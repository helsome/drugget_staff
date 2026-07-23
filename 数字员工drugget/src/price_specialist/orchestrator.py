from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session
from sqlalchemy import func, select

from .database import configured_database, init_database
from .alerts import AlertDryRunService
from .cancellation import CancellationToken, CancelledError
from .collector import ComputerUseCollector
from .enums import CollectionStatus, TaskStatus, TaskType
from .evidence import EvidenceStore
from .errors import CollectorAccessError
from .incidents import IncidentService
from .models import CollectionRun, CollectionTask, Incident, SearchCandidate, StoreResponsibility
from .pricing import evaluate_price
from .review_orchestrator import ReviewOrchestrator
from .run_logger import RunEvent, RunEventSink
from .schemas import BrowserSession, ClassifiedCandidate, CollectionResult, CollectionTaskSpec, SearchHit
from .search import SearchClassifier, deduplicate_hits
from .services import SearchCandidateService, TaskQueueService, evaluate_fixed_result


HUMAN_OR_DEFERRED_STATUSES = {
    CollectionStatus.CHALLENGE_DETECTED,
    CollectionStatus.LOGIN_REQUIRED,
    CollectionStatus.RATE_LIMITED,
    CollectionStatus.PAGE_CHANGED,
    CollectionStatus.STORE_UNVERIFIED,
}


@dataclass(frozen=True)
class FixedWork:
    task: CollectionTaskSpec
    session: BrowserSession
    expected_box_count: Decimal | None
    units_per_box: Decimal | None
    min_unit: str | None
    control_price: Decimal | None


@dataclass(frozen=True)
class SearchWork:
    query: str
    target_brand: str
    target_spec: str | None
    session: BrowserSession
    classifier: SearchClassifier


@dataclass
class RouteOutcome:
    status: str
    results: list[Any] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DualRouteOutcome:
    fixed: RouteOutcome
    search: RouteOutcome


class DualRouteRunner:
    """Run fixed monitoring and heuristic discovery as independent state machines."""

    def __init__(self, collector: ComputerUseCollector, evidence_store: EvidenceStore, *, network_retry_limit: int = 2):
        self.collector = collector
        self.evidence_store = evidence_store
        self.network_retry_limit = network_retry_limit

    async def _collect_with_policy(self, work: FixedWork) -> CollectionResult:
        retries = 0
        while True:
            result = await self.collector.collect_fixed(work.task, work.session)
            if result.collection_status != CollectionStatus.NETWORK_ERROR:
                return result
            if retries >= self.network_retry_limit:
                return result
            retries += 1

    async def run_fixed(self, work_items: list[FixedWork]) -> RouteOutcome:
        outcome = RouteOutcome(status="completed")
        for work in work_items:
            try:
                result = await self._collect_with_policy(work)
                if result.collection_status == CollectionStatus.SUCCESS:
                    result = evaluate_price(
                        result,
                        expected_box_count=work.expected_box_count,
                        units_per_box=work.units_per_box,
                        min_unit=work.min_unit,
                        control_price=work.control_price,
                    )
                evidence_path, digest = self.evidence_store.save(work.task, result)
                result.evidence.raw_fields["evidence_sha256"] = digest
                result.evidence.raw_fields["evidence_path"] = str(evidence_path)
                outcome.results.append(result)
                if result.collection_status in HUMAN_OR_DEFERRED_STATUSES:
                    outcome.incidents.append(
                        {
                            "task_id": work.task.task_id,
                            "platform": work.task.platform,
                            "incident_type": result.collection_status.value,
                            "status": "deferred" if result.collection_status == CollectionStatus.RATE_LIMITED else "pending_human",
                            "current_url": result.final_url,
                            "page_title": result.page_title,
                            "evidence_path": str(evidence_path),
                        }
                    )
            except Exception as exc:  # keep one SKU from stopping the fixed route
                outcome.errors.append({"task_id": work.task.task_id, "error": type(exc).__name__, "detail": str(exc)})
        if outcome.errors or outcome.incidents:
            outcome.status = "partial"
        return outcome

    async def run_search(self, work_items: list[SearchWork]) -> RouteOutcome:
        outcome = RouteOutcome(status="completed")
        for work in work_items:
            try:
                hits = deduplicate_hits(await self.collector.search(work.query, work.session))
                classified: list[ClassifiedCandidate] = [
                    work.classifier.classify(
                        hit,
                        target_brand=work.target_brand,
                        target_spec=work.target_spec,
                    )
                    for hit in hits
                ]
                outcome.results.extend(classified)
            except CollectorAccessError as exc:
                outcome.errors.append(
                    {
                        "query": work.query,
                        "platform": work.session.platform,
                        "collection_status": exc.collection_status,
                        "detail": exc.message,
                    }
                )
            except Exception as exc:
                outcome.errors.append({"query": work.query, "error": type(exc).__name__, "detail": str(exc)})
        if outcome.errors:
            outcome.status = "partial"
        return outcome

    async def run(self, *, fixed: list[FixedWork], search: list[SearchWork]) -> DualRouteOutcome:
        # Deliberately do not short-circuit: either route can finish when the other fails.
        fixed_outcome = await self.run_fixed(fixed)
        search_outcome = await self.run_search(search)
        return DualRouteOutcome(fixed=fixed_outcome, search=search_outcome)


@dataclass(frozen=True)
class RatePolicy:
    detail_interval_seconds: float
    search_interval_seconds: float
    batch_size: int
    batch_cooldown_seconds: float
    interval_jitter_seconds: float = 0
    cooldown_jitter_seconds: float = 0

    def delay_for(self, task_type: str, *, batch_complete: bool) -> float:
        """Return a bounded, non-bursting interval for normal browser pacing."""
        if batch_complete:
            return max(0, self.batch_cooldown_seconds + random.uniform(-self.cooldown_jitter_seconds, self.cooldown_jitter_seconds))
        base = self.search_interval_seconds if task_type in {TaskType.SEARCH, TaskType.STORE_SEARCH} else self.detail_interval_seconds
        return max(0, base + random.uniform(-self.interval_jitter_seconds, self.interval_jitter_seconds))


def _resolve_config_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "config"


def _load_rate_policies() -> dict[str, RatePolicy]:
    """Load rate policies from JSON config, falling back to hardcoded defaults."""
    path = _resolve_config_dir() / "rate_policies.json"
    if not path.is_file():
        return _HARDCODED_RATE_POLICIES
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            platform: RatePolicy(
                detail_interval_seconds=cfg.get("detail_interval_seconds", 30),
                search_interval_seconds=cfg.get("search_interval_seconds", 45),
                batch_size=cfg.get("batch_size", 3),
                batch_cooldown_seconds=cfg.get("batch_cooldown_seconds", 300),
                interval_jitter_seconds=cfg.get("interval_jitter_seconds", 0),
                cooldown_jitter_seconds=cfg.get("cooldown_jitter_seconds", 0),
            )
            for platform, cfg in raw.items()
            if isinstance(cfg, dict)
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return _HARDCODED_RATE_POLICIES


_HARDCODED_RATE_POLICIES: dict[str, RatePolicy] = {
    "jd": RatePolicy(30, 45, 3, 300),
    "taobao": RatePolicy(25, 35, 5, 180),
    "yaoshibang": RatePolicy(32, 45, 4, 240, interval_jitter_seconds=8, cooldown_jitter_seconds=45),
}

DEFAULT_RATE_POLICIES = _load_rate_policies()


class BatchOrchestrator:
    """Persistent DB queue runner with one isolated session per platform."""

    def __init__(
        self,
        *,
        session: Session,
        collector: ComputerUseCollector,
        evidence_store: EvidenceStore,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rate_policies: dict[str, RatePolicy] | None = None,
        run_id: str | None = None,
        logger: Any | None = None,
        event_sink: Any | None = None,
        cancellation_token: CancellationToken | None = None,
        review_orchestrator: ReviewOrchestrator | None = None,
    ):
        self.session = session
        self.collector = collector
        self.evidence_store = evidence_store
        self.sleep = sleep
        self.policies = rate_policies or DEFAULT_RATE_POLICIES
        self.queue = TaskQueueService(session)
        self.incidents = IncidentService(session)
        self.candidates = SearchCandidateService(session)
        self.alerts = AlertDryRunService()
        self.run_id = run_id
        self.logger = logger
        self.event_sink = event_sink
        self.cancellation_token = cancellation_token
        # A missing gate is intentionally fail-closed.  Legacy callers may
        # still construct BatchOrchestrator directly, but they cannot turn an
        # inspected candidate into a formal price without a confirmed review.
        self.review_orchestrator = review_orchestrator

    def _is_cancelled(self) -> bool:
        return self.cancellation_token is not None and self.cancellation_token.is_cancelled

    def _emit(self, event: RunEvent) -> None:
        """Emit a structured event if the event_sink is configured."""
        if self.event_sink is not None:
            self.event_sink.emit(event)

    async def _execute(self, spec: CollectionTaskSpec, browser: BrowserSession) -> CollectionResult:
        if spec.task_type in {TaskType.SEARCH, TaskType.STORE_SEARCH}:
            if spec.metadata.get("fallback_only") and self.candidates.has_valid_candidate(
                run_id=spec.run_id,
                drug_id=str(spec.metadata.get("drug_id") or ""),
                platform=spec.platform,
            ):
                return CollectionResult(
                    collection_status=CollectionStatus.SUCCESS,
                    evidence={"raw_fields": {"hits": [], "fallback_skipped": True}},
                )
            try:
                hits = deduplicate_hits(
                    await (
                        self.collector.search_store(spec, browser)
                        if spec.task_type == TaskType.STORE_SEARCH
                        else self.collector.search(spec.query or "", browser, limit=int(spec.metadata.get("search_limit", 20)))
                    )
                )
            except CollectorAccessError as exc:
                try:
                    status = CollectionStatus(exc.collection_status)
                except ValueError:
                    status = CollectionStatus.PARSE_ERROR
                return CollectionResult(
                    collection_status=status,
                    error_code=exc.code,
                    error_detail=exc.message,
                    evidence={
                        "raw_fields": {
                            key: value for key, value in exc.details.items()
                            if key != "screenshot_bytes_b64"
                        },
                        "screenshot_bytes_b64": exc.details.get("screenshot_bytes_b64"),
                    },
                )
            except Exception as exc:
                return CollectionResult(
                    collection_status=CollectionStatus.UNKNOWN_ERROR,
                    error_code=type(exc).__name__,
                    error_detail=str(exc)[:1000],
                    evidence={"raw_fields": {"query": spec.query, "platform": spec.platform}},
                )
            search_evidence = self.collector.last_search_evidence(browser)
            search_evidence.raw_fields["hits"] = [hit.model_dump(mode="json") for hit in hits]
            return CollectionResult(
                collection_status=CollectionStatus.SUCCESS if hits else CollectionStatus.NOT_FOUND,
                error_code=None if hits else "no_valid_candidate",
                error_detail=None if hits else "搜索未返回有效候选商品",
                evidence=search_evidence,
            )

        retries = 0
        while True:
            result = (
                await self.collector.inspect_candidate(spec, browser)
                if spec.task_type == TaskType.INSPECT_CANDIDATE
                else await self.collector.collect_fixed(spec, browser)
            )
            if result.collection_status != CollectionStatus.NETWORK_ERROR or retries >= 2:
                return result
            retries += 1

    async def execute_platform(
        self,
        platform: str,
        session_alias: str,
        *,
        max_tasks: int | None = None,
        task_types: set[str] | None = None,
        skip_health_check: bool = False,
    ) -> dict[str, int | str]:
        blocking_filters = [
            Incident.platform == platform,
            Incident.incident_type.in_(
                (
                    CollectionStatus.CHALLENGE_DETECTED.value,
                    CollectionStatus.LOGIN_REQUIRED.value,
                    CollectionStatus.RATE_LIMITED.value,
                )
            ),
            Incident.status.in_(("pending_human", "in_progress", "deferred", "session_disabled")),
        ]
        if self.run_id:
            blocking_filters.append(CollectionTask.run_id == self.run_id)
        blocking = self.session.scalar(
            select(Incident.id)
            .join(CollectionTask, Incident.task_id == CollectionTask.id)
            .where(*blocking_filters)
            .limit(1)
        )
        if blocking:
            self._emit(RunEvent(
                run_id=self.run_id or "", event_type="task_blocked", phase="execute",
                status="blocked", platform=platform,
                message=f"平台 {platform} 被阻塞: 未解决的事故",
            ))
            return {"platform": platform, "completed": 0, "paused": 1, "reason": "unresolved_incident",
                "execution_status": "blocked", "business_status": "blocked"}
        browser = BrowserSession(platform=platform, alias=session_alias)
        if not skip_health_check:
            self._emit(RunEvent(
                run_id=self.run_id or "", event_type="platform_health_started",
                phase="health", status="running", platform=platform,
                message=f"检查 {platform} 平台健康状态",
            ))
            health = await self.collector.health_check(browser)
            if health.collection_status != CollectionStatus.SUCCESS:
                self._emit(RunEvent(
                    run_id=self.run_id or "", event_type="platform_health_failed",
                    phase="health", status="failed", platform=platform,
                    message=f"平台 {platform} 健康检查失败: {health.collection_status.value}",
                    collection_status=health.collection_status.value,
                ))
                return {"platform": platform, "completed": 0, "paused": 1, "reason": health.collection_status.value,
                    "execution_status": "blocked", "business_status": "blocked"}
            self._emit(RunEvent(
                run_id=self.run_id or "", event_type="platform_health_success",
                phase="health", status="success", platform=platform,
                message=f"平台 {platform} 健康检查通过",
            ))

        completed = 0
        batch_count = 0
        while (max_tasks is None or completed < max_tasks):
            if self._is_cancelled():
                break
            task = self.queue.lease(
                platform=platform,
                session_alias=session_alias,
                run_id=self.run_id,
                task_types=task_types,
            )
            if task is None:
                break
            spec = CollectionTaskSpec.model_validate(task.payload)
            task.status = TaskStatus.RUNNING.value
            self.session.commit()

            # Emit task_started
            self._emit(RunEvent(
                run_id=self.run_id or "", event_type="task_started",
                phase="execute", status="running", platform=platform,
                task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                brand_name=spec.drug_name, generic_name=spec.generic_name,
                shop_name=spec.shop_name, query=spec.query,
                message=f"开始 {spec.task_type.value} {spec.drug_name or ''} {spec.shop_name or ''}",
                details={"drug_id": str(spec.metadata.get("drug_id", "")),
                         "search_mode": spec.task_type.value if spec.task_type else ""},
            ))

            result = await self._execute(spec, browser)
            # Store-resolution may enrich metadata (for example a verified
            # 药师帮 provider_id). Persist it with the queued task for audit and
            # future replay before writing the observation.
            task.payload = spec.model_dump(mode="json")
            if spec.platform == "yaoshibang" and spec.shop_name and spec.metadata.get("provider_id"):
                store = self.session.scalar(
                    select(StoreResponsibility).where(
                        StoreResponsibility.platform == "yaoshibang",
                        StoreResponsibility.shop_name == spec.shop_name,
                    )
                )
                if store and not store.platform_store_key:
                    store.platform_store_key = str(spec.metadata["provider_id"])
            if spec.platform == "taobao" and spec.shop_name and spec.metadata.get("shop_home_url"):
                store = self.session.scalar(
                    select(StoreResponsibility).where(
                        StoreResponsibility.platform == "taobao",
                        StoreResponsibility.shop_name == spec.shop_name,
                    )
                )
                if store:
                    store.shop_home_url = str(spec.metadata["shop_home_url"])
                    if spec.metadata.get("platform_store_key"):
                        store.platform_store_key = str(spec.metadata["platform_store_key"])
            if spec.task_type not in {TaskType.SEARCH, TaskType.STORE_SEARCH, TaskType.INSPECT_CANDIDATE}:
                result = evaluate_fixed_result(self.session, spec, result)
            evidence_path, digest = self.evidence_store.save(spec, result)

            if result.collection_status in {
                CollectionStatus.CHALLENGE_DETECTED,
                CollectionStatus.LOGIN_REQUIRED,
                CollectionStatus.RATE_LIMITED,
            }:
                screenshot = evidence_path / "screenshot.png"
                self.incidents.create(task, result, str(screenshot) if screenshot.exists() else None)
                self.session.commit()
                return {
                    "platform": platform,
                    "completed": completed,
                    "paused": 1,
                    "reason": result.collection_status.value,
                }

            if result.collection_status in {CollectionStatus.PAGE_CHANGED, CollectionStatus.STORE_UNVERIFIED}:
                screenshot = evidence_path / "screenshot.png"
                self.incidents.create(task, result, str(screenshot) if screenshot.exists() else None)

            if spec.task_type in {TaskType.SEARCH, TaskType.STORE_SEARCH} and result.collection_status == CollectionStatus.SUCCESS:
                hits = [SearchHit.model_validate(item) for item in result.evidence.raw_fields.get("hits", [])]
                candidates = self.candidates.classify_and_save(task=task, spec=spec, hits=hits)

                # Emit search events
                if hits:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="search_hits_received",
                        phase="search", status="success", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        query=spec.query, candidate_count=len(hits),
                        message=f"搜索到 {len(hits)} 个候选",
                    ))
                else:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="search_no_hits",
                        phase="search", status="partial", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        query=spec.query, candidate_count=0,
                        message="搜索无结果",
                    ))

                configured_inspection_limit = int(spec.metadata.get("inspect_limit", 1) or 1)
                inspection_limit = 0 if configured_inspection_limit < 0 else max(1, configured_inspection_limit)
                inspections_enqueued = 0
                for candidate in candidates:
                    if candidate.candidate_type.value not in SearchCandidateService.VALID_TYPES:
                        continue
                    if inspections_enqueued >= inspection_limit:
                        break
                    metadata = {
                        **spec.metadata,
                        "source_task_id": task.id,
                        "candidate_type": candidate.candidate_type.value,
                        "candidate_product_id": candidate.product_id,
                    }
                    if spec.platform == "yaoshibang":
                        provider_id = str(candidate.raw.get("provider_id") or metadata.get("provider_id") or "")
                        if not provider_id:
                            continue
                        metadata["provider_id"] = provider_id
                    self.queue.enqueue(CollectionTaskSpec(
                        task_id=str(uuid.uuid4()), run_id=spec.run_id, platform=spec.platform,
                        task_type=TaskType.INSPECT_CANDIDATE, session_alias=spec.session_alias,
                        priority=spec.priority + 100, drug_name=spec.drug_name,
                        generic_name=spec.generic_name, spec=spec.spec,
                        shop_name=spec.shop_name or candidate.shop_name,
                        product_id=candidate.product_id, url=candidate.url,
                        query=spec.query, metadata=metadata,
                    ))
                    inspections_enqueued += 1
                for candidate in candidates:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="candidate_saved",
                        phase="search", status="success", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=candidate.shop_name, product_id=candidate.product_id,
                        query=spec.query, candidate_count=1,
                        message=f"保存候选: {candidate.shop_name or ''} {candidate.product_id or ''}",
                    ))
            observation = self.queue.record_result(task, result, str(evidence_path))
            observation.evidence_sha256 = digest
            observation.collector_version = result.evidence.collector_version
            observation.raw_evidence = result.evidence.raw_fields

            # Emit detail / search / fixed task result events
            if spec.task_type == TaskType.INSPECT_CANDIDATE:
                if result.collection_status == CollectionStatus.SUCCESS:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="detail_succeeded",
                        phase="inspect", status="success", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=spec.shop_name, product_id=spec.product_id,
                        collection_status=result.collection_status.value,
                        message=f"详情成功: {spec.drug_name or ''} {spec.shop_name or ''}",
                    ))
                else:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="detail_failed",
                        phase="inspect", status="failed", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=spec.shop_name, product_id=spec.product_id,
                        collection_status=result.collection_status.value,
                        error_code=result.error_code, error_detail=result.error_detail,
                        message=f"详情失败: {result.collection_status.value}",
                    ))
            elif spec.task_type in {TaskType.SEARCH, TaskType.STORE_SEARCH}:
                if result.collection_status == CollectionStatus.SUCCESS:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="task_succeeded",
                        phase="search", status="success", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=spec.shop_name, query=spec.query,
                        collection_status=result.collection_status.value,
                        message=f"搜索完成: {spec.drug_name or ''}",
                    ))
                elif result.collection_status == CollectionStatus.NOT_FOUND:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="task_not_found",
                        phase="search", status="partial", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=spec.shop_name, query=spec.query,
                        collection_status=result.collection_status.value,
                        error_code=result.error_code, error_detail=result.error_detail,
                        message="搜索未找到结果",
                    ))
                else:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="task_failed",
                        phase="search", status="failed", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=spec.shop_name, query=spec.query,
                        collection_status=result.collection_status.value,
                        error_code=result.error_code, error_detail=result.error_detail,
                        message=f"搜索失败: {result.collection_status.value}",
                    ))
            else:
                # Fixed tasks
                if result.collection_status == CollectionStatus.SUCCESS:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="task_succeeded",
                        phase="fixed", status="success", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        collection_status=result.collection_status.value,
                        message=f"固定任务完成: {spec.drug_name or ''}",
                    ))
                else:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="task_failed",
                        phase="fixed", status="failed", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        collection_status=result.collection_status.value,
                        error_code=result.error_code, error_detail=result.error_detail,
                        message=f"固定任务失败: {result.collection_status.value}",
                    ))

            # Run the review gate for detail/fixed successes. The gate persists
            # a strict PriceComparison, creates a PriceBreakEvent for below-control
            # prices, dispatches the agent, and returns the formal-price status
            # that gates ``is_formal_price``.  An absent gate is fail-closed:
            # collection evidence is retained, but no candidate is released.
            review_outcome = None
            if (
                self.review_orchestrator is not None
                and result.collection_status == CollectionStatus.SUCCESS
                and spec.task_type
                in {TaskType.INSPECT_CANDIDATE, TaskType.FIXED_CORE, TaskType.FIXED_OBSERVATION}
            ):
                review_outcome = await self.review_orchestrator.review_observation(
                    observation, task_type=spec.task_type,
                )

            if spec.task_type == TaskType.INSPECT_CANDIDATE and result.collection_status == CollectionStatus.SUCCESS:
                candidate = self.session.scalar(
                    select(SearchCandidate).where(
                        SearchCandidate.run_id == spec.run_id,
                        SearchCandidate.platform == spec.platform,
                        SearchCandidate.drug_id == str(spec.metadata.get("drug_id") or ""),
                        SearchCandidate.product_id == str(spec.metadata.get("candidate_product_id") or spec.product_id or ""),
                    )
                )
                formal_price_status = (
                    review_outcome.formal_price_status
                    if review_outcome is not None
                    else "blocked"
                )
                formal_confirmed = (
                    formal_price_status == "confirmed"
                    and self.review_orchestrator is not None
                    and bool(getattr(self.review_orchestrator, "formal_release_enabled", True))
                )
                if candidate is not None and formal_confirmed:
                    candidate.is_formal_price = True
                    candidate.sku_verification_status = "verified_detail"
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="formal_price_confirmed",
                        phase="inspect", status="success", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=spec.shop_name, product_id=spec.product_id or candidate.product_id,
                        formal_price_count=1,
                        message=f"正式价格确认: {spec.drug_name or ''} {spec.shop_name or ''}",
                    ))
                elif candidate is not None and not formal_confirmed:
                    self._emit(RunEvent(
                        run_id=self.run_id or "", event_type="formal_price_blocked",
                        phase="inspect", status="partial", platform=platform,
                        task_id=spec.task_id, task_type=spec.task_type.value if spec.task_type else None,
                        brand_name=spec.drug_name, generic_name=spec.generic_name,
                        shop_name=spec.shop_name, product_id=spec.product_id or candidate.product_id,
                        message=f"正式价格未确认（待复核）: {spec.drug_name or ''} {spec.shop_name or ''}",
                        details={"formal_price_status": formal_price_status},
                    ))
            elif (
                spec.task_type == TaskType.INSPECT_CANDIDATE
                and result.collection_status == CollectionStatus.PARSE_ERROR
            ):
                self._enqueue_detail_fallback(spec)
            if result.collection_status in {CollectionStatus.PAGE_CHANGED, CollectionStatus.STORE_UNVERIFIED}:
                task.status = TaskStatus.HUMAN_REQUIRED.value
            self.session.commit()
            completed += 1
            batch_count += 1

            policy = self.policies[platform]
            if batch_count >= policy.batch_size:
                await self.sleep(policy.delay_for(spec.task_type, batch_complete=True))
                batch_count = 0
            else:
                await self.sleep(policy.delay_for(spec.task_type, batch_complete=False))
        return {"platform": platform, "completed": completed, "paused": 0, "reason": "queue_empty",
                "execution_status": "finished" if not self._is_cancelled() else "cancelled",
                "business_status": "success" if not self._is_cancelled() else "cancelled"}

    def _enqueue_detail_fallback(self, spec: CollectionTaskSpec) -> CollectionTask | None:
        """Try the next persisted candidate after a detail parse failure.

        A single search chain gets at most two fallbacks (three detail attempts
        including the initial candidate), and already-enqueued products are
        never scheduled twice.
        """
        fallback_attempt = int(spec.metadata.get("fallback_attempt", 0) or 0)
        if fallback_attempt >= 2:
            return None
        existing_product_ids = {
            str((item.payload.get("metadata") or {}).get("candidate_product_id") or item.payload.get("product_id") or "")
            for item in self.session.scalars(
                select(CollectionTask).where(
                    CollectionTask.run_id == spec.run_id,
                    CollectionTask.platform == spec.platform,
                    CollectionTask.task_type == TaskType.INSPECT_CANDIDATE.value,
                )
            )
        }
        candidates = self.session.scalars(
            select(SearchCandidate)
            .where(
                SearchCandidate.run_id == spec.run_id,
                SearchCandidate.platform == spec.platform,
                SearchCandidate.drug_id == str(spec.metadata.get("drug_id") or ""),
                SearchCandidate.candidate_type.in_(SearchCandidateService.VALID_TYPES),
            )
            .order_by(SearchCandidate.search_rank, SearchCandidate.id)
        )
        candidate = next(
            (item for item in candidates if str(item.product_id or "") not in existing_product_ids),
            None,
        )
        if candidate is None:
            return None
        metadata = {
            **spec.metadata,
            "candidate_type": candidate.candidate_type,
            "candidate_product_id": candidate.product_id,
            "fallback_attempt": fallback_attempt + 1,
        }
        if spec.platform == "yaoshibang":
            provider_id = str((candidate.raw or {}).get("provider_id") or "")
            if not provider_id:
                return None
            metadata["provider_id"] = provider_id
        return self.queue.enqueue(CollectionTaskSpec(
            task_id=str(uuid.uuid4()), run_id=spec.run_id, platform=spec.platform,
            task_type=TaskType.INSPECT_CANDIDATE, session_alias=spec.session_alias,
            priority=spec.priority + 1, drug_name=spec.drug_name,
            generic_name=spec.generic_name, spec=spec.spec,
            shop_name=candidate.shop_name, product_id=candidate.product_id,
            url=candidate.url, query=spec.query, metadata=metadata,
        ))

    async def execute_all(
        self,
        sessions: dict[str, str],
        *,
        max_tasks_per_platform: int | None = None,
    ) -> list[dict[str, int | str]]:
        if self.run_id:
            run = self.session.get(CollectionRun, self.run_id)
            if run:
                run.status = "running"
                run.started_at = run.started_at or datetime.now()
                task_types = set(
                    self.session.scalars(
                        select(CollectionTask.task_type).where(CollectionTask.run_id == self.run_id)
                    )
                )
                if task_types & {TaskType.SEARCH.value, TaskType.STORE_SEARCH.value, TaskType.INSPECT_CANDIDATE.value}:
                    run.search_status = "running"
                if task_types & {TaskType.FIXED_CORE.value, TaskType.FIXED_OBSERVATION.value}:
                    run.fixed_status = "running"
                self.session.commit()

        # Global stage order: every healthy platform completes fixed work before
        # any Search work begins. A challenge removes only that platform.
        summary = {
            platform: {"platform": platform, "completed": 0, "paused": 0, "reason": "queue_empty",
                       "execution_status": "finished", "business_status": "success"}
            for platform in sessions
        }
        paused: set[str] = set()
        for platform, alias in sessions.items():
            health = await self.collector.health_check(BrowserSession(platform=platform, alias=alias))
            if self.logger:
                self.logger.platform_check(
                    platform,
                    status="ok" if health.collection_status == CollectionStatus.SUCCESS else health.collection_status.value,
                    reason=None if health.collection_status == CollectionStatus.SUCCESS else health.collection_status.value,
                )
            if health.collection_status != CollectionStatus.SUCCESS:
                summary[platform].update(paused=1, reason=health.collection_status.value)
                paused.add(platform)
        stages = [
            {TaskType.FIXED_CORE.value, TaskType.FIXED_OBSERVATION.value},
            {TaskType.STORE_SEARCH.value},
            {TaskType.SEARCH.value},
            {TaskType.INSPECT_CANDIDATE.value},
        ]
        for task_types in stages:
            if self._is_cancelled():
                break
            for platform, alias in sessions.items():
                if platform in paused:
                    continue
                remaining = (
                    None
                    if max_tasks_per_platform is None
                    else max_tasks_per_platform - int(summary[platform]["completed"])
                )
                if remaining is not None and remaining <= 0:
                    continue
                outcome = await self.execute_platform(
                    platform,
                    alias,
                    max_tasks=remaining,
                    task_types=task_types,
                    skip_health_check=True,
                )
                summary[platform]["completed"] = int(summary[platform]["completed"]) + int(outcome["completed"])
                summary[platform]["paused"] = outcome["paused"]
                summary[platform]["reason"] = outcome["reason"]
                summary[platform]["execution_status"] = outcome.get("execution_status", "finished")
                summary[platform]["business_status"] = outcome.get("business_status", "success")
                if outcome["paused"]:
                    paused.add(platform)

        if self.run_id:
            if self._is_cancelled():
                self.queue.cancel_pending_tasks(self.run_id)
                self.queue.mark_run_cancelled(self.run_id)
                self._emit(RunEvent(
                    run_id=self.run_id, event_type="run_cancelled",
                    phase="done", status="cancelled",
                    message="运行已取消",
                ))
                for s in summary.values():
                    s["execution_status"] = "cancelled"
                    s["business_status"] = "cancelled"
            else:
                run = self.session.get(CollectionRun, self.run_id)
                if run:
                    counts = dict(
                    self.session.execute(
                        select(CollectionTask.status, func.count())
                        .where(CollectionTask.run_id == self.run_id)
                        .group_by(CollectionTask.status)
                    ).all()
                )
                run.summary = {**(run.summary or {}), "task_status_counts": counts, "platform_outcomes": summary}
                unfinished = sum(counts.get(status, 0) for status in ("pending", "leased", "running", "human_required"))
                run.status = (
                    "completed"
                    if unfinished == 0
                    else "human_required"
                    if paused or counts.get(TaskStatus.HUMAN_REQUIRED.value, 0)
                    else "pending"
                )
                task_status_rows = self.session.execute(
                    select(CollectionTask.task_type, CollectionTask.status)
                    .where(CollectionTask.run_id == self.run_id)
                ).all()
                fixed_statuses = [
                    status for task_type, status in task_status_rows
                    if task_type in {TaskType.FIXED_CORE.value, TaskType.FIXED_OBSERVATION.value}
                ]
                search_statuses = [
                    status for task_type, status in task_status_rows
                    if task_type in {TaskType.SEARCH.value, TaskType.STORE_SEARCH.value, TaskType.INSPECT_CANDIDATE.value}
                ]
                unfinished_statuses = {
                    TaskStatus.PENDING.value,
                    TaskStatus.LEASED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.HUMAN_REQUIRED.value,
                }
                if fixed_statuses:
                    run.fixed_status = "completed" if not unfinished_statuses.intersection(fixed_statuses) else "pending"
                if search_statuses:
                    run.search_status = "completed" if not unfinished_statuses.intersection(search_statuses) else "pending"
                if run.status == "completed":
                    run.finished_at = datetime.now()
                self.session.commit()
        return list(summary.values())
