"""Fail-closed formal-price state decisions.

This module is the single deterministic decision table for a comparison before
an agent is dispatched.  A comparison that cannot be made is a captured piece
of evidence, never a confirmation of a formal price.
"""
from __future__ import annotations

from dataclasses import dataclass

from .decisions import BELOW_CONTROL, NOT_BELOW_CONTROL, NOT_COMPARABLE


@dataclass(frozen=True)
class FormalPriceState:
    """The deterministic pre-review state for a price comparison."""

    review_required: bool
    review_status: str
    formal_price_status: str
    dispatch_agent: bool


# ``reason_code`` values produced by PriceDecisionService that are not
# comparable.  Every known outcome has an explicit fail-closed state.
NOT_COMPARABLE_STATES: dict[str, FormalPriceState] = {
    "exact_confirmed_control_rule_missing": FormalPriceState(
        False, "guidance_missing", "captured_uncompared", False,
    ),
    "formal_detail_price_missing": FormalPriceState(False, "blocked", "blocked", False),
    "drug_identity_missing": FormalPriceState(False, "blocked", "blocked", False),
    "detail_spec_missing": FormalPriceState(False, "blocked", "blocked", False),
    "package_unverified": FormalPriceState(False, "blocked", "blocked", False),
    "package_unit_mismatch": FormalPriceState(False, "blocked", "blocked", False),
    "control_rule_ambiguous": FormalPriceState(
        False, "human_review_required", "human_review_required", False,
    ),
    "control_rule_unit_mismatch": FormalPriceState(
        False, "human_review_required", "human_review_required", False,
    ),
}


# Export action types deliberately mirror the decision reason for all blocking
# cases.  This prevents a newly captured non-comparable result from silently
# disappearing from action_queue.csv.
NOT_COMPARABLE_ACTIONS: dict[str, tuple[str, str]] = {
    "exact_confirmed_control_rule_missing": ("guidance_missing", "缺少已确认的完整规格控价规则"),
    "formal_detail_price_missing": ("formal_detail_price_missing", "详情正式价格或最小单位价格缺失"),
    "drug_identity_missing": ("drug_identity_missing", "详情任务缺少可审计药品身份"),
    "detail_spec_missing": ("detail_spec_missing", "详情未确认完整规格"),
    "package_unverified": ("package_unverified", "包装规格尚未验证"),
    "package_unit_mismatch": ("package_unit_mismatch", "详情最小单位与包装主数据不一致"),
    "control_rule_ambiguous": ("control_rule_ambiguous", "控价规则存在歧义，需人工复核"),
    "control_rule_unit_mismatch": ("control_rule_unit_mismatch", "控价最小单位与包装主数据不一致，需人工复核"),
}


def formal_price_state(verdict: str | None, reason_code: str | None) -> FormalPriceState:
    """Resolve a comparison into its only allowed pre-review state.

    Unknown verdicts/reasons are blocked by default.  The low-price path is
    deliberately dispatchable even when the recorded difference is only one
    cent; ReviewPolicy independently enforces that invariant before dispatch.
    """
    if verdict == NOT_BELOW_CONTROL:
        return FormalPriceState(False, "not_required", "confirmed", False)
    if verdict == BELOW_CONTROL:
        return FormalPriceState(True, "pending_agent", "pending", True)
    if verdict == NOT_COMPARABLE:
        return NOT_COMPARABLE_STATES.get(
            reason_code or "",
            FormalPriceState(False, "blocked", "blocked", False),
        )
    return FormalPriceState(False, "blocked", "blocked", False)


def not_comparable_action(reason_code: str | None, reason_detail: str | None) -> tuple[str, str]:
    """Return a visible action-queue category for every non-comparable reason."""
    action_type, default_detail = NOT_COMPARABLE_ACTIONS.get(
        reason_code or "",
        ("not_comparable_blocked", "不可比较结果已阻断正式价格，需人工排查"),
    )
    return action_type, reason_detail or default_detail
