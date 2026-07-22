"""Test execution runner — reusable module for the Tkinter workbench and programmatic use."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue

from sqlalchemy.orm import Session

from .cancellation import CancellationToken, CancelledError
from .collector import OpenCLIComputerUseCollector
from .config import Settings
from .database import configured_database, init_database
from .enums import StoreSelectionMode, TaskType
from .evidence import EvidenceStore
from .orchestrator import BatchOrchestrator, DEFAULT_RATE_POLICIES, RatePolicy
from .schemas import CollectionTaskSpec
from .services import DrugSelection, StoreTaskPlanner, TaskQueueService


@dataclass
class TestRunConfig:
    """Configuration for a single test run, submitted from the GUI."""

    drugs: list[DrugSelection] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    search_modes: list[str] = field(default_factory=lambda: ["global_search", "store_search"])
    search_limit: int = 5
    max_candidates: int = 3
    inspect_limit: int = 3
    rate_policy_overrides: dict[str, dict] = field(default_factory=dict)
    use_test_db: bool = True
    output_root: str | None = None
    store_selection_mode: StoreSelectionMode = StoreSelectionMode.RESPONSIBILITY_ONLY
    selected_store_ids: list[str] = field(default_factory=list)


@dataclass
class ProgressUpdate:
    """Fine-grained status update pushed from the worker thread to the GUI."""

    timestamp: str = ""
    run_id: str = ""
    platform: str = ""
    phase: str = ""  # init / search / inspect / store_search / export / done / error
    task_type: str | None = None  # GLOBAL_SEARCH / STORE_SEARCH / INSPECT_CANDIDATE
    drug_name: str | None = None
    shop_name: str | None = None
    query: str | None = None
    status: str = ""  # pending / running / success / failed / skipped
    message: str = ""
    detail: str | None = None
    output_path: str | None = None
    platform_total: int = 0
    platform_completed: int = 0
    platform_failed: int = 0
    elapsed_seconds: float = 0.0


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _progress(
    *, phase: str, status: str, message: str, platform: str = "",
    task_type: str | None = None, drug_name: str | None = None,
    shop_name: str | None = None, query: str | None = None,
    detail: str | None = None, run_id: str = "",
    output_path: str | None = None,
    platform_total: int = 0, platform_completed: int = 0, platform_failed: int = 0,
    elapsed: float = 0.0,
) -> ProgressUpdate:
    return ProgressUpdate(
        timestamp=_now(), run_id=run_id, platform=platform, phase=phase,
        task_type=task_type, drug_name=drug_name, shop_name=shop_name,
        query=query, status=status, message=message, detail=detail,
        output_path=output_path,
        platform_total=platform_total, platform_completed=platform_completed,
        platform_failed=platform_failed, elapsed_seconds=elapsed,
    )


class TestWorker:
    """Runs a test collection in a background thread, pushing ProgressUpdate to a queue."""

    def __init__(self, config: TestRunConfig, root: Path) -> None:
        self.config = config
        self.root = root
        self.queue: Queue[ProgressUpdate] = Queue()
        self._cancel_flag = threading.Event()
        self.cancellation_token = CancellationToken()
        self._start_time = 0.0

    def start(self) -> None:
        self._start_time = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        self._cancel_flag.set()
        self.cancellation_token.cancel()

    def _elapsed(self) -> float:
        return time.time() - self._start_time

    def _put(self, update: ProgressUpdate) -> None:
        update.elapsed_seconds = self._elapsed()
        self.queue.put(update)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_run())
        except Exception as exc:
            self._put(_progress(
                phase="error", status="failed", platform="",
                message=f"运行异常: {exc}",
            ))
        finally:
            loop.close()

    async def _async_run(self) -> None:
        cfg = self.config
        settings = Settings.load(self.root, mode="test" if cfg.use_test_db else "prod")
        # Safety: validate runtime mode before connecting to the database
        if cfg.use_test_db:
            settings.validate_runtime_mode("test")
        else:
            settings.validate_runtime_mode("prod")
        engine, factory = configured_database(settings)
        init_database(engine)

        # Sanitize fake provider_ids before any task generation
        with factory() as db:
            cleaned = StoreTaskPlanner.sanitize_fake_provider_ids(db)
            if cleaned:
                self._put(_progress(
                    phase="init", status="running", platform="",
                    run_id="",
                    message=f"已清理 {cleaned} 个历史假 provider_id",
                ))

        # Build session map
        sessions: dict[str, str] = {}
        for platform in cfg.platforms:
            sessions[platform] = f"{platform}-p0"

        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            run_id = run.id

            # Count planned tasks for preview
            global_search_count = 0
            store_search_count = 0
            skipped_store_count = 0
            need_resolution_count = 0
            total_drugs = len(cfg.drugs)

            for selection in cfg.drugs:
                drug = selection.resolve(db)
                if drug is None:
                    continue
                if "global_search" in cfg.search_modes:
                    global_search_count += 1
                if "store_search" in cfg.search_modes:
                    planner = StoreTaskPlanner(db)
                    for platform in cfg.platforms:
                        store_results = planner.eligible_stores(
                            platform=platform,
                            drug=drug,
                            selection=cfg.store_selection_mode,
                            manual_store_ids=cfg.selected_store_ids,
                        )
                        for r in store_results:
                            if r.eligible:
                                store_search_count += 1
                            else:
                                skipped_store_count += 1
                                if r.need_identity_resolution:
                                    need_resolution_count += 1

            self._put(_progress(
                phase="init", status="running", platform="", run_id=run_id,
                message=f"创建运行: {run_id[:8]}... 药品 {total_drugs} 个, 平台 {len(cfg.platforms)} 个",
                detail=(
                    f"搜索模式: {', '.join(cfg.search_modes)} | "
                    f"搜索条数: {cfg.search_limit} | "
                    f"店铺模式: {cfg.store_selection_mode.value} | "
                    f"全局搜索: {global_search_count} | "
                    f"店铺搜索: {store_search_count} | "
                    f"跳过店铺: {skipped_store_count} | "
                    f"需解析: {need_resolution_count}"
                ),
            ))

            # Enqueue tasks per platform
            for platform in cfg.platforms:
                await self._enqueue_platform_tasks(
                    db, queue, run_id, platform, cfg,
                )

            db.commit()

            self._put(_progress(
                phase="init", status="success", platform="", run_id=run_id,
                message="任务入队完成，开始采集",
            ))

            # Execute
            collector = OpenCLIComputerUseCollector(settings)
            evidence_store = EvidenceStore(settings.evidence_dir)
            rate_policies = dict(DEFAULT_RATE_POLICIES)
            for platform, override in cfg.rate_policy_overrides.items():
                current = rate_policies.get(platform)
                if current is None:
                    continue
                rate_policies[platform] = RatePolicy(
                    detail_interval_seconds=float(override.get("detail_interval", current.detail_interval_seconds)),
                    search_interval_seconds=float(override.get("search_interval", current.search_interval_seconds)),
                    batch_size=int(override.get("batch_size", current.batch_size)),
                    batch_cooldown_seconds=float(override.get("batch_cooldown", current.batch_cooldown_seconds)),
                    interval_jitter_seconds=current.interval_jitter_seconds,
                    cooldown_jitter_seconds=current.cooldown_jitter_seconds,
                )
            orchestrator = BatchOrchestrator(
                session=db, collector=collector,
                evidence_store=evidence_store, run_id=run_id,
                rate_policies=rate_policies,
                cancellation_token=self.cancellation_token,
            )
            outcomes = await orchestrator.execute_all(sessions)

            if self.cancellation_token.is_cancelled:
                # Cancel pending tasks and mark run as cancelled
                queue.cancel_pending_tasks(run_id)
                queue.mark_run_cancelled(run_id)
                db.commit()
                self._put(_progress(
                    phase="done", status="cancelled", platform="", run_id=run_id,
                    message="采集已取消",
                    detail=str(outcomes),
                ))
                return  # Skip export and success updates

            self._put(_progress(
                phase="done", status="success", platform="", run_id=run_id,
                message="采集完成",
                detail=str(outcomes),
            ))

            # Export results — only run outputs, not fixture inputs
            from export_fixture_run_csv import export_run_outputs, export_run_manifest

            csv_dir = (
                Path(cfg.output_root) / run_id
                if cfg.output_root
                else self.root / "artifacts" / "runs" / "current" / run_id
            )
            csv_dir.mkdir(parents=True, exist_ok=True)
            export_run_outputs(run_id, db, csv_dir)
            export_run_manifest(
                run_id, db, csv_dir,
                run=run,
                selected_drugs=[d.generic_name or d.brand_name for d in cfg.drugs],
                selected_platforms=cfg.platforms,
                selected_search_modes=cfg.search_modes,
                effective_parameters={
                    "search_limit": cfg.search_limit,
                    "max_candidates": cfg.max_candidates,
                    "inspect_limit": cfg.inspect_limit,
                },
                source_type="test_workbench",
                runtime_mode="test" if cfg.use_test_db else "production",
            )
            self._put(_progress(
                phase="export", status="success", platform="", run_id=run_id,
                message=f"CSV 已导出: {csv_dir}",
                output_path=str(csv_dir),
            ))

    async def _enqueue_platform_tasks(
        self,
        db: Session,
        queue: TaskQueueService,
        run_id: str,
        platform: str,
        cfg: TestRunConfig,
    ) -> None:
        """Enqueue tasks for one platform based on the selected drugs and search modes.

        Store search tasks are generated through ``StoreTaskPlanner`` to
        prevent the unbounded Cartesian product of ``all drugs x all stores``.
        """
        planner = StoreTaskPlanner(db)

        for selection in cfg.drugs:
            drug = selection.resolve(db)
            if drug is None:
                self._put(_progress(
                    phase="init", status="skipped", platform=platform,
                    drug_name=selection.generic_name or selection.brand_name, run_id=run_id,
                    message=f"跳过: 药品 {selection.generic_name or selection.brand_name} 未在目录中找到",
                ))
                continue

            common_metadata = {
                "drug_id": drug.id,
                "target_brand": drug.brand_name,
                "search_limit": cfg.search_limit,
                "candidate_limit": cfg.max_candidates,
                "inspect_limit": cfg.inspect_limit,
                "source": "test_workbench",
            }
            query = f"{drug.brand_name} {drug.generic_name}"

            # GLOBAL_SEARCH
            if "global_search" in cfg.search_modes:
                spec = CollectionTaskSpec(
                    task_id=str(uuid.uuid4()),
                    run_id=run_id,
                    platform=platform,
                    task_type=TaskType.SEARCH,
                    session_alias=f"{platform}-p0",
                    drug_name=drug.brand_name,
                    generic_name=drug.generic_name,
                    query=query,
                    metadata={
                        **common_metadata,
                        "route": "global",
                    },
                )
                queue.enqueue(spec)
                self._put(_progress(
                    phase="init", status="success", platform=platform,
                    task_type="GLOBAL_SEARCH", drug_name=drug.generic_name,
                    query=query, run_id=run_id,
                    message=f"已入队: 全局搜索 {drug.generic_name}",
                ))

            # STORE_SEARCH — use StoreTaskPlanner to avoid Cartesian product
            if "store_search" in cfg.search_modes:
                store_results = planner.eligible_stores(
                    platform=platform,
                    drug=drug,
                    selection=cfg.store_selection_mode,
                    manual_store_ids=cfg.selected_store_ids,
                )

                eligible_stores = [r for r in store_results if r.eligible]
                skipped_stores = [r for r in store_results if not r.eligible]

                # Report skipped stores
                for r in skipped_stores:
                    reason_map = {
                        "missing_shop_home_url": "缺少 shop_home_url",
                        "missing_provider_id": "缺少可信 provider_id",
                        "no_responsibility": "与药品无责任关系",
                        "not_executable": "不可执行的身份状态",
                        "not_selected": "未手工选择",
                    }
                    shop_name = r.store.shop_name if r.store else "未知"
                    reason_text = reason_map.get(r.reason, r.reason)
                    self._put(_progress(
                        phase="init", status="skipped", platform=platform,
                        task_type="STORE_SEARCH", drug_name=drug.generic_name,
                        shop_name=shop_name, run_id=run_id,
                        message=f"跳过店铺: {shop_name} — {reason_text}",
                        detail=f"原因: {r.reason}"
                        + (", 需要身份解析" if r.need_identity_resolution else ""),
                    ))

                if not eligible_stores:
                    self._put(_progress(
                        phase="init", status="skipped", platform=platform,
                        task_type="STORE_SEARCH", drug_name=drug.generic_name,
                        run_id=run_id,
                        message=f"无可用店铺: {drug.generic_name} @ {platform}",
                        detail=f"店铺模式: {cfg.store_selection_mode.value}, "
                               f"跳过 {len(skipped_stores)} 个店铺",
                    ))
                    continue

                for r in eligible_stores:
                    store = r.store
                    if store is None:
                        continue

                    # Platform-specific metadata
                    route = "shop_home"
                    shop_home_url = None
                    provider_id = None
                    if platform == "taobao":
                        route = "shop_home"
                        shop_home_url = store.shop_home_url
                    elif platform == "yaoshibang":
                        route = "provider_profile"
                        provider_id = store.platform_store_key

                    spec = CollectionTaskSpec(
                        task_id=str(uuid.uuid4()),
                        run_id=run_id,
                        platform=platform,
                        task_type=TaskType.STORE_SEARCH,
                        session_alias=f"{platform}-p0",
                        drug_name=drug.brand_name,
                        generic_name=drug.generic_name,
                        shop_name=store.shop_name,
                        query=query,
                        metadata={
                            **common_metadata,
                            "route": route,
                            "provider_id": provider_id,
                            "shop_home_url": shop_home_url,
                        },
                    )
                    queue.enqueue(spec)
                    self._put(_progress(
                        phase="init", status="success", platform=platform,
                        task_type="STORE_SEARCH", drug_name=drug.generic_name,
                        shop_name=store.shop_name, run_id=run_id,
                        message=f"已入队: 店铺搜索 {drug.generic_name} @ {store.shop_name}",
                    ))
