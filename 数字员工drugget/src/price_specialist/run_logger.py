"""Structured run-event model, sinks, and drug-level status aggregation.

Provides the observability backbone for the drug collection system.  Every
action (run lifecycle, search, detail inspection, formal-price confirmation,
export) produces a ``RunEvent`` that is delivered to every registered
``RunEventSink``.  Sinks include JSONL files, in-process queues, and
periodic CSV/JSON snapshots.
"""
from __future__ import annotations

import csv
import json
import threading
import uuid
from typing import Any, Protocol
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

EVENT_TYPES = frozenset({
    "run_created",
    "run_started",
    "run_cancelled",
    "run_finished",
    "platform_health_started",
    "platform_health_success",
    "platform_health_failed",
    "task_planned",
    "task_enqueued",
    "task_started",
    "task_succeeded",
    "task_not_found",
    "task_failed",
    "task_blocked",
    "task_skipped",
    "task_cancelled",
    "search_started",
    "search_hits_received",
    "search_no_hits",
    "candidate_saved",
    "detail_started",
    "detail_succeeded",
    "detail_failed",
    "formal_price_confirmed",
    "export_started",
    "export_succeeded",
    "export_failed",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class RunEvent:
    """Every observable occurrence during a batch run.

    All fields are optional (except the identity fields) so that callers
    only fill what they know at the point of emission.
    """

    event_id: str = field(default_factory=_new_id)
    timestamp: str = field(default_factory=_now_iso)
    run_id: str = ""
    event_type: str = ""
    phase: str = ""
    status: str = ""
    platform: str | None = None
    task_id: str | None = None
    task_type: str | None = None
    drug_id: str | None = None
    brand_name: str | None = None
    generic_name: str | None = None
    store_id: str | None = None
    shop_name: str | None = None
    query: str | None = None
    product_id: str | None = None
    provider_id: str | None = None
    candidate_count: int | None = None
    inspected_count: int | None = None
    formal_price_count: int | None = None
    collection_status: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Sink protocol and implementations
# ---------------------------------------------------------------------------

class RunEventSink(Protocol):
    """Protocol for anything that can consume a ``RunEvent``."""

    def emit(self, event: RunEvent) -> None:
        ...


class CompositeEventSink:
    """Fan-out to multiple sinks.  One failed sink does not stop the others."""

    def __init__(self, sinks: list[RunEventSink] | None = None) -> None:
        self._sinks: list[RunEventSink] = sinks or []

    def add(self, sink: RunEventSink) -> None:
        self._sinks.append(sink)

    def emit(self, event: RunEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                import traceback
                traceback.print_exc()


class JsonlEventSink:
    """Append structured events to a JSONL file.

    The file is flushed after every write so a crash never loses the last
    event.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8")

    def emit(self, event: RunEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
        self._handle.write(line + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle and not self._handle.closed:
            self._handle.close()

    def __del__(self) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._path


class QueueEventSink:
    """Push events into a thread-safe ``Queue`` for GUI consumption."""

    def __init__(self, queue: Queue[RunEvent]) -> None:
        self._queue = queue

    def emit(self, event: RunEvent) -> None:
        self._queue.put(event)


class DatabaseEventSink:
    """Persist events to a SQL database (future use — stub)."""

    def emit(self, event: RunEvent) -> None:
        pass


class LoggingEventSink:
    """Write a human-readable one-liner to Python's ``logging`` module."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def emit(self, event: RunEvent) -> None:
        parts = [f"[{event.timestamp}]", event.event_type]
        if event.platform:
            parts.append(f"plat={event.platform}")
        if event.brand_name:
            parts.append(f"drug={event.brand_name}")
        if event.shop_name:
            parts.append(f"shop={event.shop_name}")
        if event.message:
            parts.append(f"msg={event.message}")
        self._logger.info(" ".join(parts))


# ---------------------------------------------------------------------------
# Drug-level status aggregation
# ---------------------------------------------------------------------------

@dataclass
class DrugRunStatus:
    """Aggregated status of one drug across all platforms in a single run."""

    drug_id: str | None = None
    brand_name: str = ""
    generic_name: str = ""
    platform: str = ""
    search_mode: str = ""
    current_phase: str = "pending"
    status: str = "pending"
    candidate_count: int = 0
    detail_success_count: int = 0
    formal_price_count: int = 0
    error_count: int = 0
    last_reason: str = ""
    task_count: int = 0
    completed_task_count: int = 0


class DrugStatusTracker:
    """Thread-safe tracker that aggregates per-drug status from event streams.

    The tracker can be fed events in real time and exposes a snapshot of
    every drug seen so far.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._drugs: dict[str, DrugRunStatus] = {}

    def _key(self, brand_name: str, platform: str) -> str:
        return f"{brand_name}||{platform}"

    def ingest(self, event: RunEvent) -> None:
        """Update aggregated state from one event."""
        with self._lock:
            brand = event.brand_name or ""
            platform = event.platform or ""
            key = self._key(brand, platform)
            if key not in self._drugs:
                self._drugs[key] = DrugRunStatus(
                    drug_id=event.drug_id,
                    brand_name=brand,
                    generic_name=event.generic_name or "",
                    platform=platform,
                    search_mode=event.details.get("search_mode", "") if event.details else "",
                    current_phase=event.phase,
                    status="pending",
                )
            ds = self._drugs[key]

            if event.event_type == "task_started":
                ds.current_phase = event.phase or "running"
                ds.status = "running"
            elif event.event_type in ("task_succeeded", "formal_price_confirmed"):
                ds.completed_task_count += 1
                if event.event_type == "formal_price_confirmed":
                    ds.formal_price_count = (ds.formal_price_count or 0) + 1
                if event.event_type == "task_succeeded":
                    pass  # task_succeeded doesn't imply detail_success
                ds.status = "success"
            elif event.event_type == "detail_succeeded":
                ds.detail_success_count = (ds.detail_success_count or 0) + 1
                ds.completed_task_count += 1
            elif event.event_type in ("task_failed", "task_not_found"):
                ds.error_count += 1
            elif event.event_type == "search_hits_received":
                ds.candidate_count = event.candidate_count or ds.candidate_count
            elif event.event_type == "candidate_saved":
                ds.candidate_count += 1

            if event.event_type == "task_failed":
                ds.status = "partial"
                ds.last_reason = event.error_detail or event.message or "failed"
            elif event.event_type == "formal_price_confirmed":
                ds.status = "success"
                ds.last_reason = event.message or "formal_price_confirmed"
            elif event.event_type == "task_succeeded":
                ds.status = "success"
                ds.last_reason = event.message or "success"
            elif event.event_type == "search_no_hits":
                ds.status = "partial"
                ds.last_reason = "no_hits"
            elif event.event_type == "task_blocked":
                ds.status = "failed"
                ds.last_reason = event.error_detail or event.message or "blocked"
            elif event.event_type == "task_cancelled":
                ds.status = "cancelled"
                ds.last_reason = "cancelled"

            if event.message:
                ds.last_reason = event.message

            if event.task_type:
                ds.search_mode = event.task_type

    def snapshot(self) -> list[DrugRunStatus]:
        """Return a copy of the current drug statuses (sorted by brand+platform)."""
        with self._lock:
            return sorted(
                [ds for ds in self._drugs.values()],
                key=lambda x: (x.brand_name, x.platform),
            )

    def summary_counts(self) -> dict[str, int]:
        """Return total / success / partial / failed / cancelled counts."""
        with self._lock:
            total = len(self._drugs)
            success = sum(1 for ds in self._drugs.values() if ds.status == "success")
            partial = sum(1 for ds in self._drugs.values() if ds.status == "partial")
            failed = sum(1 for ds in self._drugs.values() if ds.status == "failed")
            cancelled = sum(1 for ds in self._drugs.values() if ds.status == "cancelled")
            running = sum(1 for ds in self._drugs.values() if ds.status == "running")
            return {
                "total": total,
                "success": success,
                "partial": partial,
                "failed": failed,
                "cancelled": cancelled,
                "running": running,
            }


# ---------------------------------------------------------------------------
# Snapshot writers (atomic write to disk)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temp file + rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def write_run_status_snapshot(path: Path, tracker: DrugStatusTracker,
                               run_id: str, run_mode: str = "",
                               elapsed_seconds: float = 0) -> None:
    """Write a JSON snapshot of the full run status."""
    drug_list = [asdict(ds) for ds in tracker.snapshot()]
    counts = tracker.summary_counts()
    snapshot = {
        "run_id": run_id,
        "run_mode": run_mode,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "timestamp": _now_iso(),
        "summary_counts": counts,
        "drugs": drug_list,
    }
    _atomic_write(path, json.dumps(snapshot, ensure_ascii=False, default=str, indent=2))


def write_drug_status_csv(path: Path, tracker: DrugStatusTracker) -> None:
    """Write a CSV snapshot of per-drug status."""
    drugs = tracker.snapshot()
    _atomic_write(path, "")
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "brand_name", "generic_name", "platform", "search_mode",
            "current_phase", "status", "candidate_count",
            "detail_success_count", "formal_price_count",
            "error_count", "last_reason",
        ])
        writer.writeheader()
        for ds in drugs:
            writer.writerow({
                "brand_name": ds.brand_name,
                "generic_name": ds.generic_name,
                "platform": ds.platform,
                "search_mode": ds.search_mode,
                "current_phase": ds.current_phase,
                "status": ds.status,
                "candidate_count": ds.candidate_count,
                "detail_success_count": ds.detail_success_count,
                "formal_price_count": ds.formal_price_count,
                "error_count": ds.error_count,
                "last_reason": ds.last_reason,
            })


def write_task_status_csv(path: Path, tracker: DrugStatusTracker) -> None:
    """Write a CSV snapshot of aggregated task-level status."""
    counts = tracker.summary_counts()
    _atomic_write(path, "")
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "total", "success", "partial", "failed", "cancelled", "running",
        ])
        writer.writeheader()
        writer.writerow(counts)


# ---------------------------------------------------------------------------
# Convenience: build a composite sink with all standard sinks
# ---------------------------------------------------------------------------

def build_run_event_system(
    run_id: str,
    output_dir: Path,
    event_queue: Queue[RunEvent] | None = None,
    logger: Any = None,
) -> tuple[CompositeEventSink, DrugStatusTracker]:
    """Build a standard event pipeline: JSONL, queue, and optional logging.

    Returns
    -------
    (sink, tracker)
        The ``sink`` should be passed to the orchestrator and runner.
        The ``tracker`` can be polled for periodic snapshots.
    """
    sink = CompositeEventSink()
    tracker = DrugStatusTracker()

    jsonl_path = output_dir / "run_events.jsonl"
    sink.add(JsonlEventSink(jsonl_path))

    if event_queue is not None:
        sink.add(QueueEventSink(event_queue))

    if logger is not None:
        sink.add(LoggingEventSink(logger))

    _inner_sink = sink

    class TrackingCompositeEventSink:
        """Composite that also feeds the DrugStatusTracker."""

        def emit(self, event: RunEvent) -> None:
            tracker.ingest(event)
            _inner_sink.emit(event)

    return TrackingCompositeEventSink(), tracker


# ---------------------------------------------------------------------------
# Backward-compatible alias for BatchLogger (legacy)
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = frozenset({"password", "cookie", "cookies", "token", "pin", "secret", "authorization"})

PLATFORM_LABELS: dict[str, str] = {
    "taobao": "淘宝",
    "yaoshibang": "药师帮",
    "jd": "京东",
}


def _platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


def _now_iso_legacy() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _safe(obj: Any, depth: int = 0) -> Any:
    if depth > 5:
        return str(obj)[:200]
    if isinstance(obj, dict):
        from collections import OrderedDict
        return OrderedDict(
            (k, "[REDACTED]" if k.lower() in SENSITIVE_KEYS else _safe(v, depth + 1))
            for k, v in obj.items()
        )
    if isinstance(obj, list):
        return [_safe(item, depth + 1) for item in obj]
    if isinstance(obj, str):
        return obj[:2000] if len(obj) > 2000 else obj
    return obj


class BatchLogger:
    """Legacy batch logger, kept for backward compatibility.

    Deprecated: use ``build_run_event_system`` instead.
    """

    def __init__(self, run_id: str, output_dir: Path) -> None:
        self.run_id = run_id
        self.log_path = output_dir / "run.log.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.log_path.open("a", encoding="utf-8")
        self.task_index: int = 0
        self.total_tasks: int = 0

    def close(self) -> None:
        if self._handle and not self._handle.closed:
            self._handle.close()

    def __del__(self) -> None:
        self.close()

    def _write(self, event: dict) -> None:
        payload = {
            "run_id": self.run_id,
            "timestamp": _now_iso_legacy(),
            "task_index": self.task_index,
            "total_tasks": self.total_tasks,
            **event,
        }
        safe = _safe(payload)
        line = json.dumps(safe, ensure_ascii=False, default=str)
        self._handle.write(line + "\n")
        self._handle.flush()

    def _terminal(self, platform: str, action: str, *details: str) -> None:
        label = _platform_label(platform)
        seq = f"[{self.task_index}/{self.total_tasks}]" if self.total_tasks else ""
        parts = [f"[{_now()}][{label}]{seq}", action, *details]
        print(" ".join(parts), flush=True)

    def batch_start(self, platform: str, *, total_tasks: int) -> None:
        self.total_tasks = total_tasks
        self.task_index = 0
        self._write({"event_type": "batch_start", "platform": platform, "total_tasks": total_tasks})
        self._terminal(platform, "批次开始", f"共{total_tasks}个任务")

    def platform_check(self, platform: str, *, status: str, reason: str | None = None) -> None:
        self._write({"event_type": "platform_check", "platform": platform, "status": status, "reason": reason})
        if status != "ok":
            self._terminal(platform, f"暂停｜{reason or status}")

    def task_start(self, platform: str, *, task_id: str, **kwargs) -> None:
        self.task_index += 1
        self._write({"event_type": "task_start", "platform": platform, "task_id": task_id, **kwargs})
        route = kwargs.get("route", "")
        shop = kwargs.get("shop", "")
        drug = kwargs.get("drug", "")
        details_bits = [route or "", shop or "", drug or ""]
        detail_str = "｜".join(b for b in details_bits if b)
        self._terminal(platform, "开始", detail_str)

    def search_complete(self, platform: str, *, task_id: str, **kwargs) -> None:
        self._write({"event_type": "search_complete", "platform": platform, "task_id": task_id, **kwargs})
        valid_count = kwargs.get("valid_count", 0)
        duration = kwargs.get("duration", 0)
        if valid_count == 0:
            self._terminal(platform, "未找到", f"候选0条", f"耗时{duration:.1f}秒")
        else:
            self._terminal(platform, "搜索完成", f"候选{valid_count}条", f"耗时{duration:.1f}秒")

    def candidate_select(self, platform: str, *, task_id: str, **kwargs) -> None:
        self._write({"event_type": "candidate_select", "platform": platform, "task_id": task_id, **kwargs})
        selected = kwargs.get("selected", 0)
        self._terminal(platform, "候选选择", f"选中{selected}条进入详情")

    def detail_open(self, platform: str, *, task_id: str, **kwargs) -> None:
        self._write({"event_type": "detail_open", "platform": platform, "task_id": task_id, **kwargs})

    def price_save(self, platform: str, *, task_id: str, **kwargs) -> None:
        self._write({"event_type": "price_save", "platform": platform, "task_id": task_id, **kwargs})
        price = kwargs.get("price")
        if price:
            self._terminal(platform, "正式价格", f"{price}元", "证据已保存")
        else:
            self._terminal(platform, "价格保存", "证据已保存")

    def task_fail(self, platform: str, *, task_id: str, **kwargs) -> None:
        self._write({"event_type": "task_fail", "platform": platform, "task_id": task_id, **kwargs})
        error_type = kwargs.get("error_type", "未知错误")
        self._terminal(platform, "失败", error_type)

    def platform_pause(self, platform: str, *, reason: str) -> None:
        self._write({"event_type": "platform_pause", "platform": platform, "reason": reason})
        self._terminal(platform, "暂停", reason)

    def batch_end(self, platform: str, *, summary: dict | None = None) -> None:
        self._write({"event_type": "batch_end", "platform": platform, "summary": summary})
        self._terminal(platform, "批次结束", str(summary or ""))