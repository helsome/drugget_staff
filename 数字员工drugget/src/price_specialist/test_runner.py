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

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import DRUG_MAP
from .collector import OpenCLIComputerUseCollector
from .config import Settings
from .database import configured_database, init_database
from .enums import TaskType
from .evidence import EvidenceStore
from .models import DrugProduct, StoreResponsibility
from .orchestrator import BatchOrchestrator, DEFAULT_RATE_POLICIES, RatePolicy
from .schemas import CollectionTaskSpec
from .services import TaskQueueService


@dataclass
class TestRunConfig:
    """Configuration for a single test run, submitted from the GUI."""

    drugs: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    search_modes: list[str] = field(default_factory=lambda: ["global_search", "store_search"])
    search_limit: int = 5
    max_candidates: int = 3
    inspect_limit: int = 3
    rate_policy_overrides: dict[str, dict] = field(default_factory=dict)
    use_test_db: bool = True
    output_root: str | None = None


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
        self._start_time = 0.0

    def start(self) -> None:
        self._start_time = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        self._cancel_flag.set()

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
        settings = Settings.from_env(self.root, test_mode=cfg.use_test_db)
        engine, factory = configured_database(settings)
        init_database(engine)

        # Build session map
        sessions: dict[str, str] = {}
        for platform in cfg.platforms:
            sessions[platform] = f"{platform}-p0"

        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            run_id = run.id

            self._put(_progress(
                phase="init", status="running", platform="", run_id=run_id,
                message=f"创建运行: {run_id[:8]}... 药品 {len(cfg.drugs)} 个, 平台 {len(cfg.platforms)} 个",
                detail=f"搜索模式: {', '.join(cfg.search_modes)} | 搜索条数: {cfg.search_limit}",
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
            )
            outcomes = await orchestrator.execute_all(sessions)

            self._put(_progress(
                phase="done", status="success", platform="", run_id=run_id,
                message="采集完成",
                detail=str(outcomes),
            ))

            # Export results
            from export_fixture_run_csv import export_run

            csv_dir = (
                Path(cfg.output_root) / run_id
                if cfg.output_root
                else self.root / "artifacts" / "runs" / "current" / run_id
            )
            export_run(run_id, csv_dir, test_mode=cfg.use_test_db)
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
        """Enqueue tasks for one platform based on the selected drugs and search modes."""
        for generic_name in cfg.drugs:
            brand_name = DRUG_MAP.get(generic_name, "")
            if not brand_name:
                self._put(_progress(
                    phase="init", status="skipped", platform=platform,
                    drug_name=generic_name, run_id=run_id,
                    message=f"跳过: 药品 {generic_name} 未在目录中找到",
                ))
                continue

            drug = db.scalar(select(DrugProduct).where(DrugProduct.brand_name == brand_name))
            if drug is None:
                drug = DrugProduct(brand_name=brand_name, generic_name=generic_name)
                db.add(drug)
                db.flush()
            common_metadata = {
                "drug_id": drug.id,
                "target_brand": brand_name,
                "search_limit": cfg.search_limit,
                "candidate_limit": cfg.max_candidates,
                "inspect_limit": cfg.inspect_limit,
                "source": "test_workbench",
            }
            query = f"{brand_name} {generic_name}"

            # GLOBAL_SEARCH
            if "global_search" in cfg.search_modes:
                spec = CollectionTaskSpec(
                    task_id=str(uuid.uuid4()),
                    run_id=run_id,
                    platform=platform,
                    task_type=TaskType.SEARCH,
                    session_alias=f"{platform}-p0",
                    drug_name=brand_name,
                    generic_name=generic_name,
                    query=query,
                    metadata={
                        **common_metadata,
                        "route": "global",
                    },
                )
                queue.enqueue(spec)
                self._put(_progress(
                    phase="init", status="success", platform=platform,
                    task_type="GLOBAL_SEARCH", drug_name=generic_name,
                    query=query, run_id=run_id,
                    message=f"已入队: 全局搜索 {generic_name}",
                ))

            # STORE_SEARCH — enqueue for each known store on this platform
            if "store_search" in cfg.search_modes:
                stores = db.execute(
                    select(StoreResponsibility).where(
                        StoreResponsibility.platform == platform,
                    )
                ).scalars().all()
                for store in stores:
                    spec = CollectionTaskSpec(
                        task_id=str(uuid.uuid4()),
                        run_id=run_id,
                        platform=platform,
                        task_type=TaskType.STORE_SEARCH,
                        session_alias=f"{platform}-p0",
                        drug_name=brand_name,
                        generic_name=generic_name,
                        shop_name=store.shop_name,
                        query=query,
                        metadata={
                            **common_metadata,
                            "route": "provider_profile" if platform == "yaoshibang" else "shop_home",
                            "provider_id": store.platform_store_key if platform == "yaoshibang" else None,
                            "shop_home_url": store.shop_home_url if platform == "taobao" else None,
                        },
                    )
                    queue.enqueue(spec)
                    self._put(_progress(
                        phase="init", status="success", platform=platform,
                        task_type="STORE_SEARCH", drug_name=generic_name,
                        shop_name=store.shop_name, run_id=run_id,
                        message=f"已入队: 店铺搜索 {generic_name} @ {store.shop_name}",
                    ))
