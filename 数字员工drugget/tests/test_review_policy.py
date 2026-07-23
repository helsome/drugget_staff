"""L0 tests for ReviewPolicy - the single source of truth for review triggers."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from price_specialist.decisions import BELOW_CONTROL, NOT_BELOW_CONTROL, NOT_COMPARABLE
from price_specialist.review_policy import BELOW_CONTROL_REASON, ReviewPolicy


@dataclass
class _Comparison:
    """Minimal stand-in for PriceComparison: ReviewPolicy only reads verdict + difference."""
    verdict: str
    difference: Decimal | None = None


def test_below_control_by_one_cent_triggers() -> None:
    """A price below guidance by even 0.0001 must trigger review (spec §3.2 - uncloseable)."""
    policy = ReviewPolicy()
    trigger = policy.requires_review(_Comparison(BELOW_CONTROL, Decimal("0.0001")))
    assert trigger is not None
    assert trigger.reason == BELOW_CONTROL_REASON


def test_below_control_by_large_margin_triggers() -> None:
    policy = ReviewPolicy()
    trigger = policy.requires_review(_Comparison(BELOW_CONTROL, Decimal("0.3415")))
    assert trigger is not None and trigger.reason == BELOW_CONTROL_REASON


def test_not_below_control_does_not_trigger() -> None:
    """At or above guidance never triggers the below-control review."""
    assert ReviewPolicy().requires_review(_Comparison(NOT_BELOW_CONTROL, Decimal("-1"))) is None


def test_not_comparable_does_not_trigger() -> None:
    """A non-comparable verdict (e.g. guidance missing) is not a below-control review trigger."""
    assert ReviewPolicy().requires_review(_Comparison(NOT_COMPARABLE, None)) is None


def test_trigger_detail_carries_difference() -> None:
    policy = ReviewPolicy()
    trigger = policy.requires_review(_Comparison(BELOW_CONTROL, Decimal("0.3415")))
    assert trigger is not None and "0.3415" in trigger.detail
