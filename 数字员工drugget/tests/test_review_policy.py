"""L0 tests for ReviewPolicy - the single source of truth for review triggers."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from price_specialist.decisions import BELOW_CONTROL, NOT_BELOW_CONTROL, NOT_COMPARABLE
from price_specialist.review_policy import BELOW_CONTROL_REASON, ReviewPolicy
from price_specialist.formal_price_state import formal_price_state


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


@pytest.mark.parametrize(
    ("raw_evidence", "expected_reason"),
    [
        ({"sku_ambiguous": True}, "sku_ambiguous"),
        ({"price_type_ambiguous": True}, "price_type_ambiguous"),
        ({"package_conversion_status": "failed"}, "package_conversion_failed"),
        ({"page_changed": True}, "page_changed"),
        ({"evidence_complete": False}, "evidence_incomplete"),
        ({"manufacturer_match": False}, "manufacturer_mismatch"),
        ({"price_type": "range_price"}, "price_range_detected"),
        ({"price_quotes": [{"price_type": "tier_price"}]}, "tier_price_detected"),
        ({"selected_sku": {"verified": False}}, "selected_sku_unverified"),
        ({"sku_options": [{"available": True}, {"available": True}]}, "sku_ambiguous"),
    ],
)
def test_explicit_evidence_conditions_trigger_review(raw_evidence, expected_reason) -> None:
    observation = SimpleNamespace(raw_evidence=raw_evidence, collection_status="success", error_code=None)
    trigger = ReviewPolicy().requires_review(_Comparison(NOT_BELOW_CONTROL), observation)
    assert trigger is not None
    assert trigger.reason == expected_reason


def test_page_changed_status_triggers_review() -> None:
    observation = SimpleNamespace(raw_evidence={}, collection_status="page_changed", error_code=None)
    trigger = ReviewPolicy().requires_review(_Comparison(NOT_BELOW_CONTROL), observation)
    assert trigger is not None and trigger.reason == "page_changed"


def test_missing_or_legacy_evidence_does_not_create_new_trigger() -> None:
    """Missing Stage-2 fields must not make old, valid fixtures review-required."""
    for raw_evidence in ({}, {"price_type": "base_price"}, {"sku_options": []}):
        observation = SimpleNamespace(raw_evidence=raw_evidence, collection_status="success", error_code=None)
        assert ReviewPolicy().requires_review(_Comparison(NOT_BELOW_CONTROL), observation) is None


def test_below_control_hard_rule_has_priority_over_evidence_trigger() -> None:
    observation = SimpleNamespace(raw_evidence={"page_changed": True}, collection_status="page_changed", error_code=None)
    trigger = ReviewPolicy().requires_review(_Comparison(BELOW_CONTROL, Decimal("0.0001")), observation)
    assert trigger is not None
    assert trigger.reason == BELOW_CONTROL_REASON


def test_trigger_detail_carries_difference() -> None:
    policy = ReviewPolicy()
    trigger = policy.requires_review(_Comparison(BELOW_CONTROL, Decimal("0.3415")))
    assert trigger is not None and "0.3415" in trigger.detail


def test_formal_price_decision_table_is_fail_closed() -> None:
    """Only exact not_below_control may bypass the agent and confirm."""
    expected = {
        ("not_below_control", "exact_rule_matched"): ("not_required", "confirmed"),
        ("below_control", "exact_rule_matched"): ("pending_agent", "pending"),
        ("not_comparable", "exact_confirmed_control_rule_missing"): ("guidance_missing", "captured_uncompared"),
        ("not_comparable", "formal_detail_price_missing"): ("blocked", "blocked"),
        ("not_comparable", "drug_identity_missing"): ("blocked", "blocked"),
        ("not_comparable", "detail_spec_missing"): ("blocked", "blocked"),
        ("not_comparable", "package_unverified"): ("blocked", "blocked"),
        ("not_comparable", "package_unit_mismatch"): ("blocked", "blocked"),
        ("not_comparable", "control_rule_ambiguous"): ("human_review_required", "human_review_required"),
        ("not_comparable", "control_rule_unit_mismatch"): ("human_review_required", "human_review_required"),
    }
    for (verdict, reason), (review_status, formal_status) in expected.items():
        actual = formal_price_state(verdict, reason)
        assert (actual.review_status, actual.formal_price_status) == (review_status, formal_status)

    unknown = formal_price_state("not_comparable", "new_reason")
    assert (unknown.review_status, unknown.formal_price_status) == ("blocked", "blocked")
