from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .enums import FixedTier, PriceStatus
from .models import MonitorTarget, PriceBreakEvent, PriceObservation, StoreResponsibility


class AlertDryRunService:
    """Produces preview payloads only. No transport is configured in P0."""

    def build_payload(
        self,
        *,
        observation: dict[str, Any],
        store: dict[str, Any] | None,
        fixed_tier: FixedTier,
    ) -> dict[str, Any]:
        if observation.get("price_status") != PriceStatus.BELOW_CONTROL.value:
            return {"routing_status": "not_a_break", "send": False}
        if fixed_tier != FixedTier.RESPONSIBILITY_CORE:
            return {"routing_status": "unassigned", "send": False, "reason": "observation_only"}
        if not store or not store.get("responsible_person"):
            return {"routing_status": "unassigned", "send": False, "reason": "missing_responsibility"}
        return {
            "routing_status": "routed_dry_run",
            "send": False,
            "recipient": store["responsible_person"],
            "responsible_unit": store.get("responsible_unit"),
            "platform": observation.get("platform"),
            "drug": observation.get("drug"),
            "spec": observation.get("spec"),
            "price": observation.get("single_unit_price"),
            "control_price": observation.get("control_price"),
            "note": "P0 dry-run；未发送真实消息",
        }

    def record_event(
        self,
        session: Session,
        *,
        observation: PriceObservation,
    ) -> PriceBreakEvent | None:
        if observation.price_status != PriceStatus.BELOW_CONTROL.value or not observation.target_id:
            return None
        target = session.get(MonitorTarget, observation.target_id)
        if target is None:
            return None
        store = session.get(StoreResponsibility, target.store_id) if target.store_id else None
        store_data = None if store is None else {
            "responsible_person": store.responsible_person,
            "responsible_unit": store.responsible_unit,
        }
        payload = self.build_payload(
            observation={
                "single_unit_price": str(observation.single_unit_price),
                "control_price": str(observation.control_price),
                "spec": target.spec_normalized,
                "platform": target.platform,
            },
            store=store_data,
            fixed_tier=FixedTier(target.fixed_tier),
        )
        event = PriceBreakEvent(
            observation_id=observation.id,
            store_id=target.store_id,
            routing_status=payload["routing_status"],
            event_status="dry_run",
            payload=payload,
        )
        session.add(event)
        session.flush()
        return event
