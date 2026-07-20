from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .decisions import BELOW_CONTROL
from .models import (
    CentralAssignmentQueue,
    CollectionTask,
    NotificationDelivery,
    PriceBreakEvent,
    PriceComparison,
    PriceObservation,
    StoreResponsibility,
)
from .routing import delivery_idempotency_key, decide_price_break_route


class AlertDryRunService:
    """Creates only local dry-run records. It has no notification transport."""

    def ensure_event(self, session: Session, *, comparison: PriceComparison) -> PriceBreakEvent | None:
        if comparison.verdict != BELOW_CONTROL:
            return None
        existing = session.scalar(select(PriceBreakEvent).where(PriceBreakEvent.comparison_id == comparison.id))
        if existing is not None:
            return existing
        observation = session.get(PriceObservation, comparison.observation_id)
        if observation is None:
            raise ValueError("comparison observation not found")
        task = session.get(CollectionTask, observation.task_id)
        store = self._find_store(session, observation, task)
        payload = {
            "send": False,
            "comparison_id": comparison.id,
            "observation_id": observation.id,
            "verdict": comparison.verdict,
            "difference": str(comparison.difference) if comparison.difference is not None else None,
            "control_rule": comparison.rule_snapshot,
            "detail_evidence": comparison.detail_evidence_snapshot,
        }
        event = PriceBreakEvent(observation_id=observation.id, comparison_id=comparison.id, store_id=store.id if store else None, routing_status="pending", event_status="dry_run", payload=payload)
        session.add(event)
        session.flush()
        return event

    def route_preview(self, session: Session, *, event: PriceBreakEvent) -> dict[str, Any]:
        observation = session.get(PriceObservation, event.observation_id)
        task = session.get(CollectionTask, observation.task_id) if observation else None
        store = session.get(StoreResponsibility, event.store_id) if event.store_id else self._find_store(session, observation, task) if observation else None
        if store and event.store_id is None:
            event.store_id = store.id
        decision = decide_price_break_route(responsible_person=store.responsible_person if store else None, contact=store.contact if store else None, responsible_unit=store.responsible_unit if store else None, dry_run=True)
        if decision.routing_status == "unassigned":
            event.routing_status = "unassigned"
            queue = session.scalar(select(CentralAssignmentQueue).where(CentralAssignmentQueue.event_id == event.id))
            if queue is None:
                queue = CentralAssignmentQueue(event_id=event.id, reason_code="store_or_contact_missing", payload={"reason": decision.reason, "platform": task.platform if task else None, "page_shop": observation.page_shop if observation else None})
                session.add(queue)
            session.flush()
            return {"event_id": event.id, "routing_status": "unassigned", "send": False, "reason": decision.reason}
        event.routing_status = "routed_dry_run"
        key = delivery_idempotency_key(event_id=event.id, recipient=decision.recipient, channel="company_notification_preview")
        delivery = session.scalar(select(NotificationDelivery).where(NotificationDelivery.idempotency_key == key))
        if delivery is None:
            delivery = NotificationDelivery(event_id=event.id, channel="company_notification_preview", recipient=decision.recipient, status="preview", idempotency_key=key, payload={"send": False, "responsible_person": store.responsible_person, "responsible_unit": store.responsible_unit, "event": event.payload})
            session.add(delivery)
        session.flush()
        return {"event_id": event.id, "routing_status": "routed_dry_run", "send": False, "recipient": decision.recipient, "delivery_id": delivery.id}

    def _find_store(self, session: Session, observation: PriceObservation, task: CollectionTask | None) -> StoreResponsibility | None:
        if observation.target_id:
            # A target/store lookup is intentionally left to the existing foreign-key path.
            from .models import MonitorTarget
            target = session.get(MonitorTarget, observation.target_id)
            if target and target.store_id:
                return session.get(StoreResponsibility, target.store_id)
        if task and observation.page_shop:
            return session.scalar(select(StoreResponsibility).where(StoreResponsibility.platform == task.platform, StoreResponsibility.shop_name == observation.page_shop))
        return None

    def record_event(self, session: Session, *, observation: PriceObservation) -> PriceBreakEvent | None:
        comparison = session.scalar(select(PriceComparison).where(PriceComparison.observation_id == observation.id))
        return self.ensure_event(session, comparison=comparison) if comparison else None
