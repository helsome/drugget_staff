"""Stage 2A canonical SKU/page-price evidence regression coverage."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from price_specialist.sku_evidence import (
    load_replay_evidence,
    normalize_sku_evidence,
    quote_single_unit_price,
)


_REPLAY = Path(__file__).parent / "fixtures" / "replay"


def _fixture(name: str) -> dict:
    return load_replay_evidence(_REPLAY / name)


def test_single_sku_replay_has_a_verified_selected_quote() -> None:
    evidence = _fixture("qingnuoshu_single_sku_base.json")
    assert evidence["evidence_complete"] is True
    assert evidence["selected_sku"]["sku_id"] == "qns-50-20"
    assert evidence["sku_options"][0]["selected"] is True
    assert evidence["price_quotes"][0]["sku_id"] == evidence["selected_sku"]["sku_id"]
    assert evidence["price_quotes"][0]["evidence_pointer"] == "price_quotes[0].amount"


def test_multi_sku_and_switch_keep_the_selected_sku_bound_to_its_quote() -> None:
    initial = _fixture("youliwei_multi_sku.json")
    switched = _fixture("youliwei_sku_switch.json")
    assert initial["selected_sku"]["sku_id"] == "ylw-75-36"
    assert switched["selected_sku"]["sku_id"] == "ylw-75-48"
    selected_quote = next(
        quote for quote in switched["price_quotes"]
        if quote["sku_id"] == switched["selected_sku"]["sku_id"]
    )
    assert selected_quote["amount"] == "59.90"
    assert selected_quote["evidence_pointer"] == "price_quotes[1].amount"


def test_explicit_multi_box_evidence_is_required_for_unit_price_conversion() -> None:
    evidence = _fixture("anduolin_package_total.json")
    quote = evidence["price_quotes"][0]
    assert quote["min_quantity"] == 2  # MOQ is retained, not used as an implicit divisor.
    assert quote["price_box_count"] == 2
    assert quote["units_per_box"] == 3
    assert quote_single_unit_price(quote) == Decimal("149.3333333333333333333333333")
    assert quote_single_unit_price({"amount": "896", "min_quantity": 2}) is None


@pytest.mark.parametrize(
    ("fixture", "expected_type", "expected_moq", "membership", "promotion"),
    [
        ("changfan_member_price.json", "member_price", 1, True, False),
        ("changfan_tier_price.json", "tier_price", 10, False, False),
    ],
)
def test_conditional_price_quotes_keep_price_type_and_conditions(
    fixture: str,
    expected_type: str,
    expected_moq: int,
    membership: bool,
    promotion: bool,
) -> None:
    evidence = _fixture(fixture)
    quote = next(quote for quote in evidence["price_quotes"] if quote["price_type"] == expected_type and quote["min_quantity"] == expected_moq)
    assert quote["membership_required"] is membership
    assert quote["promotion_required"] is promotion
    assert quote["evidence_pointer"].startswith("price_quotes[")


def test_missing_selected_sku_id_stays_incomplete_instead_of_being_invented() -> None:
    evidence = normalize_sku_evidence(
        {"price": "38.00", "units_per_box": 20},
        product_id="page-product",
        title="测试药品 20mg*20片",
        selected_spec="20mg*20片",
        page_price_raw="¥38.00",
        page_price_value="38.00",
        min_purchase_quantity=1,
    )
    assert evidence["selected_sku"]["sku_id"] == ""
    assert evidence["selected_sku"]["verified"] is False
    assert evidence["evidence_complete"] is False


def test_conflicting_legacy_selected_sku_cannot_certify_a_different_option() -> None:
    evidence = normalize_sku_evidence(
        {
            "sku_options": [
                {"sku_id": "page-selected", "raw_spec": "20mg*20片", "selected": True, "available": True},
                {"sku_id": "other", "raw_spec": "20mg*10片", "selected": False, "available": True},
            ],
            "selected_sku": {"sku_id": "other", "normalized_spec": "20mg*10片", "verified": True},
            "price_quotes": [{"sku_id": "page-selected", "amount": "38", "min_quantity": 1, "price_type": "base_price"}],
        },
        product_id="page-product",
        title="测试药品",
    )
    assert evidence["selected_sku"]["sku_id"] == "page-selected"
    assert evidence["selected_sku"]["verified"] is True
