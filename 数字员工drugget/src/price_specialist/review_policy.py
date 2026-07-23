"""ReviewPolicy - the single source of truth for mandatory agent-review triggers.

Spec §3.2: any standardized comparison price below the valid guidance price must
enter review, even by 0.01. This hard rule is uncloseable.
Spec §3.3: additional triggers (SKU ambiguity, page change, evidence incomplete, ...)
are added in Stage 2; they all live here, never in adapters, the GUI, or the
orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .decisions import BELOW_CONTROL

BELOW_CONTROL_REASON = "below_control"


@dataclass(frozen=True)
class ReviewTrigger:
    """A single reason a price observation must enter agent review."""

    reason: str
    detail: str


class ReviewPolicy:
    """Evaluate whether a price comparison must be routed to agent review.

    The policy reads only the deterministic ``PriceComparison`` verdict and
    difference; it never relaxes package or guidance rules. Stage 1 implements
    the below-control trigger only. Stage 2 extends ``requires_review`` with the
    SKU/evidence triggers - all of them defined here, nowhere else.
    """

    def requires_review(self, comparison: Any, observation: Any = None) -> ReviewTrigger | None:
        if getattr(comparison, "verdict", None) == BELOW_CONTROL:
            difference = getattr(comparison, "difference", None)
            detail = f"标准化采集价低于有效指导价 {difference}"
            return ReviewTrigger(reason=BELOW_CONTROL_REASON, detail=detail)
        return None
