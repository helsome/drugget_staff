from __future__ import annotations

import hashlib

from .schemas import RouteDecision


def decide_price_break_route(
    *,
    responsible_person: str | None,
    contact: str | None,
    responsible_unit: str | None,
    dry_run: bool = True,
) -> RouteDecision:
    if not responsible_person:
        return RouteDecision(
            routing_status="unassigned",
            dry_run=True,
            reason=f"缺少责任人；转中央待分配队列（责任单位：{responsible_unit or '未知'}）",
        )
    if not contact:
        return RouteDecision(
            routing_status="unassigned",
            dry_run=True,
            reason=f"责任人{responsible_person}缺少联系方式；不发送",
        )
    return RouteDecision(
        routing_status="ready" if not dry_run else "dry_run",
        recipient=contact,
        channel="company_notification",
        dry_run=dry_run,
        reason="责任店档案命中；当前仅生成通知预览" if dry_run else "责任店档案命中",
    )


def delivery_idempotency_key(*, event_id: str, recipient: str | None, channel: str | None) -> str:
    value = f"{event_id}|{recipient or ''}|{channel or ''}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

