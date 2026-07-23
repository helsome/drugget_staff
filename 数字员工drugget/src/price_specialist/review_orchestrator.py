"""Review gate orchestrator: decide -> policy -> agent -> validator.

Wires the deterministic :class:`PriceDecisionService`, the mandatory
:class:`ReviewPolicy`, the pluggable agent :class:`AgentReviewService`, and the
deterministic :class:`AgentProposalValidator` into one per-observation state
machine. ``BatchOrchestrator`` delegates each detail/fixed success to it so
that ``is_formal_price`` is gated on the review outcome rather than set
unconditionally.

The orchestrator is deliberately thin: it only mutates review/formal-price
columns on the persisted ``PriceComparison`` / ``PriceBreakEvent`` rows and
emits structured ``RunEvent``s. Agent failure is contained - it never raises
out of :meth:`review_observation`, so a broken agent cannot block the
collection batch.
"""
from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent_validator import AgentProposalValidator, ValidationOutcome
from .alerts import AlertDryRunService
from .decisions import NOT_BELOW_CONTROL, NOT_COMPARABLE, PriceDecisionService
from .formal_price_state import formal_price_state
from .models import PriceBreakEvent
from .review_policy import ReviewPolicy, ReviewTrigger
from .run_logger import RunEvent

logger = logging.getLogger(__name__)


# Validator decision -> (comparison.review_status, comparison.formal_price_status).
_REVIEW_STATUS: dict[str, tuple[str, str]] = {
    "accepted": ("accepted", "confirmed"),
    "recapture": ("recapture_required", "pending"),
    "human_review": ("human_review_required", "pending"),
    "reject": ("rejected", "blocked"),
}


def _event_decision(outcome_decision: str) -> str:
    """Normalize the validator verdict to the agent/proposal decision vocabulary.

    The validator returns ``accepted`` for the accept case; the
    ``PriceBreakEvent.review_decision`` column stores the proposal vocabulary
    (``accept``) so the event reads consistently with the ``AgentProposal``
    schema.
    """
    return "accept" if outcome_decision == "accepted" else outcome_decision


@dataclass
class ReviewOutcome:
    """Result of running the review gate for one observation."""

    formal_price_status: str
    comparison: Any
    event: Any = None
    proposal: Any = None
    skipped: bool = False


class ReviewOrchestrator:
    """Runs the review gate for one observation: decide -> policy -> agent -> validator."""

    def __init__(
        self,
        *,
        session: Any,
        decision_service: PriceDecisionService,
        review_policy: ReviewPolicy,
        review_service: Any,
        validator: AgentProposalValidator,
        alert_service: AlertDryRunService,
        evidence_root: Path,
        emit: Callable[[RunEvent], None] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.session = session
        self.decision_service = decision_service
        self.review_policy = review_policy
        self.review_service = review_service
        self.validator = validator
        self.alert_service = alert_service
        self.evidence_root = Path(evidence_root)
        self.emit: Callable[[RunEvent], None] = emit or (lambda _event: None)
        self.run_id = run_id

    async def review_observation(self, observation: Any, *, task_type: Any = None) -> ReviewOutcome:
        del task_type  # reserved for Stage-2 task-specific triggers; unused in Stage 1.

        # 1. Decide: persist a strict, evidence-linked PriceComparison.
        comparison = self.decision_service.evaluate_observation(observation.id)

        # 2. Policy: is mandatory agent review required?
        trigger: ReviewTrigger | None = self.review_policy.requires_review(comparison, observation)

        # 3. The deterministic decision table is fail-closed.  A
        # non-comparable result never calls an agent: page anomalies cannot
        # turn missing guidance/package evidence into a reviewable formal
        # price.  Only an exact not_below_control result *without* a policy
        # trigger may be confirmed without an agent.
        state = formal_price_state(comparison.verdict, comparison.reason_code)
        if comparison.verdict == NOT_COMPARABLE or (
            (comparison.verdict != NOT_BELOW_CONTROL or trigger is None)
            and not state.dispatch_agent
        ):
            comparison.review_required = state.review_required
            comparison.review_status = state.review_status
            comparison.formal_price_status = state.formal_price_status
            self.session.flush()
            return ReviewOutcome(
                formal_price_status=state.formal_price_status,
                comparison=comparison,
                event=None,
                skipped=True,
            )

        # Stage-2 evidence triggers apply to comparable, non-below-control
        # prices too.  They use the same pending -> agent -> validator gate as
        # a below-control comparison and cannot retain direct confirmation.
        if comparison.verdict == NOT_BELOW_CONTROL and trigger is not None:
            state = type(state)(True, "pending_agent", "pending", True)

        # A below-control result must be reviewed.  If an implementation
        # regresses ReviewPolicy, preserve safety by blocking instead of
        # falling through to a formal confirmation.
        if trigger is None:
            comparison.review_required = True
            comparison.review_status = "blocked"
            comparison.formal_price_status = "blocked"
            self.session.flush()
            self._emit_review_event(
                "review_policy_violation",
                status="failed",
                observation=observation,
                comparison=comparison,
                event=None,
                message="低于控价但未获得审核触发，已阻断正式价格",
                details={"reason_code": comparison.reason_code},
            )
            return ReviewOutcome(
                formal_price_status="blocked",
                comparison=comparison,
                event=None,
                skipped=True,
            )

        # 4. Review required -> create the PriceBreakEvent, dispatch the agent, validate.
        comparison.review_required = state.review_required
        comparison.review_reason = trigger.reason
        comparison.review_status = state.review_status
        comparison.formal_price_status = state.formal_price_status
        self.session.flush()

        event = self.alert_service.ensure_event(self.session, comparison=comparison)
        if event is None:
            # Non-low comparable prices can still need an evidence review (for
            # example ambiguous SKU selection).  They are review-only events:
            # no price-break routing or notification is created, but the
            # durable event id is required to store auditable agent I/O.
            event = PriceBreakEvent(
                observation_id=observation.id,
                comparison_id=comparison.id,
                routing_status="review_only",
                event_status="review_only",
                payload={
                    "send": False,
                    "review_only": True,
                    "comparison_id": comparison.id,
                    "observation_id": observation.id,
                    "review_reason": trigger.reason,
                },
            )
            self.session.add(event)
            self.session.flush()
        if event is not None:
            event.review_status = "pending_agent"
            self.session.flush()

        self._emit_review_event(
            "review_required",
            status="running",
            observation=observation,
            comparison=comparison,
            event=event,
            message=f"价格低于控价，触发 Agent 复核: {trigger.detail}",
            details={
                "reason": trigger.reason,
                "difference": str(comparison.difference) if comparison.difference is not None else None,
                "control_price": str(comparison.control_price) if comparison.control_price is not None else None,
            },
        )
        self._emit_review_event(
            "review_started",
            status="running",
            observation=observation,
            comparison=comparison,
            event=event,
            message="开始 Agent 复核",
        )

        proposal = None
        try:
            proposal = await self.review_service.dispatch(
                self.session,
                event=event,
                comparison=comparison,
                observation=observation,
            )
            outcome: ValidationOutcome = self.validator.validate(proposal, observation, comparison)
            if hasattr(self.review_service, "write_validation"):
                self.review_service.write_validation(event=event, outcome=outcome)
            shadow_mode = getattr(self, "review_mode", None) == "codex_shadow"
            review_status, formal_price_status = _REVIEW_STATUS.get(outcome.decision, ("human_review_required", "pending"))
            if shadow_mode:
                # Shadow results are auditable advisory data only. They never
                # settle the formal status, even if a proposal validates.
                review_status = "shadow_completed" if outcome.passed else "shadow_validation_failed"
                formal_price_status = "pending"
            comparison.review_status = review_status
            comparison.formal_price_status = formal_price_status
            if event is not None:
                event.review_status = review_status
                event.review_decision = _event_decision(outcome.decision)
                event.reviewed_at = datetime.now(timezone.utc)
                event.review_evidence_path = str(self.evidence_root / str(event.id) / "agent-review")
                if shadow_mode:
                    event.review_summary = json.dumps({
                        "shadow_review_status": review_status,
                        "shadow_review_decision": outcome.decision,
                        "shadow_validation_result": {"passed": outcome.passed, "reasons": outcome.reasons},
                    }, ensure_ascii=False)
                else:
                    event.review_summary = ", ".join(outcome.reasons) if outcome.reasons else outcome.decision
            self.session.flush()

            accepted = outcome.decision == "accepted"
            self._emit_review_event(
                "review_accepted" if accepted else "review_rejected",
                status="success" if accepted else "failed",
                observation=observation,
                comparison=comparison,
                event=event,
                message=f"Agent 复核结论: {outcome.decision}",
                details={
                    "decision": outcome.decision,
                    "reasons": list(outcome.reasons),
                    "passed": outcome.passed,
                    "formal_price_status": formal_price_status,
                },
            )
        except Exception as exc:  # Agent failure must NOT block the collection batch.
            logger.exception("agent review failed for observation %s", getattr(observation, "id", None))
            comparison.review_status = "agent_failed"
            comparison.formal_price_status = "pending"
            if event is not None:
                event.review_status = "agent_failed"
                event.review_error_code = type(exc).__name__
                event.reviewed_at = datetime.now(timezone.utc)
            self.session.flush()
            self._emit_review_event(
                "review_failed",
                status="failed",
                observation=observation,
                comparison=comparison,
                event=event,
                message=f"Agent 复核异常: {type(exc).__name__}: {exc}",
                details={"error_code": type(exc).__name__, "error_detail": str(exc)[:1000]},
            )

        return ReviewOutcome(
            formal_price_status=comparison.formal_price_status,
            comparison=comparison,
            event=event,
            proposal=proposal,
            skipped=False,
        )

    def _emit_review_event(
        self,
        event_type: str,
        *,
        status: str,
        observation: Any,
        comparison: Any,
        event: Any,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "observation_id": getattr(observation, "id", None),
            "comparison_id": getattr(comparison, "id", None),
            "event_id": getattr(event, "id", None) if event is not None else None,
        }
        if details:
            payload.update(details)
        self.emit(
            RunEvent(
                run_id=self.run_id or "",
                event_type=event_type,
                phase="review",
                status=status,
                message=message,
                details=payload,
            )
        )
