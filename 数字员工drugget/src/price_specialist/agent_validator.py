"""Deterministic validator for AgentProposal (spec §8, Stage 1).

Hard rules re-check the agent's self-reported fields deterministically and
override the agent's confidence. Stage 1 uses single-unit-price equality and
flat evidence-pointer resolution; SKU enrichment lands in Stage 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .agent_review import AgentProposal

# Column-level pointers always valid (deterministic fields on the
# observation/comparison rows, not raw_evidence keys).
_KNOWN_COLUMN_POINTERS: frozenset[str] = frozenset(
    {"single_unit_price", "page_price_value", "comparison_price", "control_price"}
)

_QUANTUM = Decimal("0.0001")

# Severity ranking for picking the most-severe failing-rule decision.
_SEVERITY: dict[str, int] = {
    "accepted": 0,
    "accept": 0,
    "recapture": 1,
    "human_review": 2,
    "reject": 3,
}


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of validating an AgentProposal against the hard rules."""

    decision: str  # accepted | recapture | human_review | reject
    passed: bool
    reasons: list[str]
    recomputed_price: Decimal | None


class AgentProposalValidator:
    """Deterministic Stage-1 validator; hard rules override agent confidence."""

    def __init__(self, *, confidence_threshold: float = 0.9) -> None:
        self.confidence_threshold = confidence_threshold

    def validate(
        self,
        proposal: AgentProposal,
        observation: Any,
        comparison: Any,
        rule: Any = None,
    ) -> ValidationOutcome:
        del rule
        reasons: list[str] = []
        failing_decisions: list[str] = []

        # Rule 1: price recompute (single-unit-price equality, Stage 1).
        recomputed_price = _parse_decimal(proposal.normalized_price)
        obs_price = getattr(observation, "single_unit_price", None)
        if not _price_matches(recomputed_price, obs_price):
            reasons.append("normalized_price")
            failing_decisions.append("human_review")

        # Rule 2: evidence pointers resolve against raw_evidence or known columns.
        raw_evidence = getattr(observation, "raw_evidence", None) or {}
        if not proposal.evidence_pointers or not all(self._pointer_resolves(p, raw_evidence) for p in proposal.evidence_pointers):
            reasons.append("evidence_pointer")
            failing_decisions.append("human_review")

        # Rule 3: confidence threshold.
        if proposal.confidence < self.confidence_threshold:
            reasons.append("confidence")
            failing_decisions.append("human_review")

        # Rule 4: unresolved questions block auto-accept.
        if proposal.unresolved_questions:
            reasons.append("unresolved_questions")
            failing_decisions.append("human_review")

        if proposal.recommended_action != proposal.decision:
            reasons.append("recommended_action")
            failing_decisions.append("human_review")
        if proposal.price_verified is not True:
            reasons.append("price_verified")
            failing_decisions.append("human_review")

        # Stage-2 structured evidence activates the complete SKU/quote
        # contract. Legacy Stage-1 rows have no SKU evidence and remain
        # conservatively governed by their existing single-unit checks.
        structured = any(key in raw_evidence for key in ("sku_options", "price_quotes", "selected_sku", "product"))
        if structured:
            selected = raw_evidence.get("selected_sku") or {}
            selected_id = selected.get("sku_id") or selected.get("id")
            sku_ids = {str(item.get("sku_id")) for item in raw_evidence.get("sku_options", []) if item.get("sku_id")}
            if not proposal.target_sku_id or proposal.target_sku_id not in sku_ids or proposal.target_sku_id != selected_id:
                reasons.append("target_sku_id")
                failing_decisions.append("recapture")
            quotes = [q for q in raw_evidence.get("price_quotes", []) if q.get("sku_id") == proposal.target_sku_id]
            page_price = getattr(observation, "page_price_value", None)
            quote = next((q for q in quotes if str(q.get("amount")) in {str(proposal.normalized_price), str(page_price)}), None)
            if quote is None:
                reasons.append("price_quote")
                failing_decisions.append("human_review")
            else:
                if proposal.price_type != quote.get("price_type"):
                    reasons.append("price_type")
                    failing_decisions.append("human_review")
                if proposal.min_purchase_quantity != quote.get("min_quantity"):
                    reasons.append("min_purchase_quantity")
                    failing_decisions.append("human_review")
            if not any(p.startswith("product.") or p.startswith("manufacturer") for p in proposal.evidence_pointers):
                reasons.append("product_manufacturer_evidence")
                failing_decisions.append("reject")
            if proposal.control_price_version_id != str(getattr(comparison, "control_price_version_id", None) or ""):
                reasons.append("control_price_version_id")
                failing_decisions.append("human_review")
            if proposal.evidence_sha256 != str(getattr(observation, "evidence_sha256", None) or ""):
                reasons.append("evidence_sha256")
                failing_decisions.append("human_review")

        # Rule 5: SKU match -> recapture.
        if proposal.sku_match is False:
            reasons.append("sku_match")
            failing_decisions.append("recapture")

        # Rule 6: product / manufacturer match -> reject.
        if proposal.product_match is False:
            reasons.append("product_match")
            failing_decisions.append("reject")
        if proposal.manufacturer_match is False:
            reasons.append("manufacturer_match")
            failing_decisions.append("reject")

        # Rule 7: agent decision sanity. Auto-accept only when the agent accepts
        # AND every hard rule passes. A non-accept agent decision is honored
        # directly; otherwise the most-severe failing-rule decision wins.
        agent_decision = proposal.decision
        if agent_decision == "accept" and not failing_decisions:
            return ValidationOutcome(
                decision="accepted",
                passed=True,
                reasons=reasons,
                recomputed_price=recomputed_price,
            )
        if agent_decision in {"recapture", "human_review", "reject"}:
            decision = agent_decision
        else:
            decision = _most_severe(failing_decisions) if failing_decisions else "human_review"
        return ValidationOutcome(
            decision=decision,
            passed=False,
            reasons=reasons,
            recomputed_price=recomputed_price,
        )

    @staticmethod
    def _pointer_resolves(pointer: str, raw_evidence: dict[str, Any]) -> bool:
        """Resolve ``a.b[0].c`` against captured evidence, never agent text."""
        if pointer in _KNOWN_COLUMN_POINTERS:
            return True
        current: Any = raw_evidence
        for segment in pointer.replace("]", "").split("."):
            for part in segment.split("["):
                if part == "":
                    continue
                if isinstance(current, dict) and part in current:
                    current = current[part]
                elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                    current = current[int(part)]
                else:
                    return False
        return current is not None


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _price_matches(recomputed: Decimal | None, obs_price: Any) -> bool:
    if recomputed is None or obs_price is None:
        return False
    try:
        observed = Decimal(obs_price)
    except (InvalidOperation, ValueError, TypeError):
        return False
    return recomputed.quantize(_QUANTUM) == observed.quantize(_QUANTUM)


def _most_severe(decisions: list[str]) -> str:
    return max(decisions, key=lambda d: _SEVERITY.get(d, 0))
