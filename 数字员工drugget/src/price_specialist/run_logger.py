"""Batch-run logger: real-time terminal progress + structured JSONL audit log.

Terminal output uses a compact format that is easy to scan at a glance:

    [16:20:11][药师帮][3/16] 开始｜店内搜索｜云天下｜葛泰
    [16:20:19][药师帮][3/16] 未找到｜候选0条｜耗时8.2秒
    [16:20:23][淘宝][4/16] 搜索完成｜候选6条｜选中1条进入详情

The JSONL file (``artifacts/runs/current/<run_id>/run.log.jsonl``) contains
every structured event and is suitable for machine analysis.  Sensitive fields
(passwords, cookies, tokens, full page source) are never written.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Fields that must never appear in any log output.
SENSITIVE_KEYS = frozenset({"password", "cookie", "cookies", "token", "pin", "secret", "authorization"})

PLATFORM_LABELS: dict[str, str] = {
    "taobao": "淘宝",
    "yaoshibang": "药师帮",
    "jd": "京东",
}


def _safe(obj: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive fields from a JSON-serialisable object."""
    if depth > 5:
        return str(obj)[:200]
    if isinstance(obj, dict):
        return OrderedDict(
            (k, "[REDACTED]" if k.lower() in SENSITIVE_KEYS else _safe(v, depth + 1))
            for k, v in obj.items()
        )
    if isinstance(obj, list):
        return [_safe(item, depth + 1) for item in obj]
    if isinstance(obj, str):
        # Truncate very long strings that could contain full page source.
        return obj[:2000] if len(obj) > 2000 else obj
    return obj


def _platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class BatchLogger:
    """Write structured run logs to both terminal and a JSONL file.

    Usage
    -----
    The logger is created once per batch and passed to
    ``BatchOrchestrator``.  Calling code does not need to call
    ``flush()`` — the file is flushed after every write.
    """

    def __init__(self, run_id: str, output_dir: Path) -> None:
        self.run_id = run_id
        self._path = output_dir / "run.log.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")
        # Task-level counters for terminal display.
        self.task_index: int = 0
        self.total_tasks: int = 0

    def close(self) -> None:
        if self._handle and not self._handle.closed:
            self._handle.close()

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, event: dict[str, Any]) -> None:
        """Write one JSONL line and flush."""
        line = json.dumps(event, ensure_ascii=False, default=str)
        self._handle.write(line + "\n")
        self._handle.flush()

    def _terminal(self, platform: str, action: str, *details: str) -> None:
        """Print one terminal line."""
        label = _platform_label(platform)
        seq = f"[{self.task_index}/{self.total_tasks}]" if self.total_tasks else ""
        parts = [f"[{_now()}][{label}]{seq}", action, *details]
        print(" ".join(parts), flush=True)

    def _event(self, event_type: str, platform: str, **kwargs: Any) -> dict[str, Any]:
        """Build a structured event dict with shared fields."""
        payload = {
            "run_id": self.run_id,
            "event_type": event_type,
            "platform": platform,
            "timestamp": _now_iso(),
            "task_index": self.task_index,
            "total_tasks": self.total_tasks,
        }
        payload.update(kwargs)
        return _safe(payload)

    # ------------------------------------------------------------------
    # Public log methods – one per log node
    # ------------------------------------------------------------------

    def batch_start(self, platform: str, *, total_tasks: int) -> None:
        """Log the beginning of a batch run."""
        self.total_tasks = total_tasks
        self.task_index = 0
        event = self._event("batch_start", platform, total_tasks=total_tasks)
        self._write(event)
        self._terminal(platform, "批次开始", f"共{total_tasks}个任务")

    def platform_check(self, platform: str, *, status: str, reason: str | None = None) -> None:
        """Log the health-check / platform gate."""
        event = self._event("platform_check", platform, status=status, reason=reason)
        self._write(event)
        if status != "ok":
            self._terminal(platform, f"暂停｜{reason or status}")

    def task_start(self, platform: str, *, task_id: str, route: str | None = None,
                   shop: str | None = None, drug: str | None = None,
                   query: str | None = None, session_alias: str | None = None) -> None:
        """Log that a task has been picked up."""
        self.task_index += 1
        details_bits = [route or "", shop or "", drug or ""]
        detail_str = "｜".join(b for b in details_bits if b)
        self._terminal(platform, "开始", detail_str)
        self._write(self._event(
            "task_start", platform,
            task_id=task_id, route=route, shop=shop, drug=drug,
            query=query, session_alias=session_alias,
        ))

    def search_complete(self, platform: str, *, task_id: str,
                        hit_count: int, valid_count: int,
                        duration: float, query: str | None = None) -> None:
        """Log search/store-search results."""
        event = self._event(
            "search_complete", platform,
            task_id=task_id, hit_count=hit_count, valid_count=valid_count,
            duration_seconds=round(duration, 1), query=query,
        )
        self._write(event)
        if valid_count == 0:
            self._terminal(platform, "未找到", f"候选0条", f"耗时{duration:.1f}秒")
        else:
            self._terminal(platform, "搜索完成", f"候选{valid_count}条", f"耗时{duration:.1f}秒")

    def candidate_select(self, platform: str, *, task_id: str,
                         selected: int, total_valid: int) -> None:
        """Log how many candidates were selected for detail inspection."""
        event = self._event(
            "candidate_select", platform,
            task_id=task_id, selected=selected, total_valid=total_valid,
        )
        self._write(event)
        self._terminal(platform, "候选选择", f"选中{selected}条进入详情")

    def detail_open(self, platform: str, *, task_id: str,
                    product_id: str | None = None, shop: str | None = None) -> None:
        """Log that a detail page is being opened."""
        event = self._event(
            "detail_open", platform,
            task_id=task_id, product_id=product_id, shop=shop,
        )
        self._write(event)
        self._terminal(platform, "详情打开", shop or "", product_id or "")

    def price_save(self, platform: str, *, task_id: str,
                   price: str | None = None, spec: str | None = None,
                   evidence_dir: str | None = None) -> None:
        """Log a successful detail price save."""
        event = self._event(
            "price_save", platform,
            task_id=task_id, price=price, spec=spec, evidence_dir=evidence_dir,
        )
        self._write(event)
        if price:
            self._terminal(platform, "正式价格", f"{price}元", "证据已保存")
        else:
            self._terminal(platform, "价格保存", "证据已保存")

    def task_fail(self, platform: str, *, task_id: str,
                  error_type: str | None = None,
                  error_detail: str | None = None,
                  duration: float | None = None,
                  evidence_dir: str | None = None) -> None:
        """Log a task failure with error details."""
        event = self._event(
            "task_fail", platform,
            task_id=task_id, error_type=error_type,
            error_detail=error_detail, duration_seconds=round(duration, 1) if duration else None,
            evidence_dir=evidence_dir,
        )
        self._write(event)
        reason = error_type or "未知错误"
        self._terminal(platform, "失败", reason)

    def platform_pause(self, platform: str, *, reason: str) -> None:
        """Log that a platform has been paused (login, CAPTCHA, rate-limit)."""
        event = self._event("platform_pause", platform, reason=reason)
        self._write(event)
        self._terminal(platform, "暂停", reason)

    def batch_end(self, platform: str, *, summary: dict[str, Any] | None = None) -> None:
        """Log the end of a batch run."""
        event = self._event("batch_end", platform, summary=summary)
        self._write(event)
        self._terminal(platform, "批次结束", str(summary or ""))

    @property
    def log_path(self) -> Path:
        return self._path