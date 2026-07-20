from decimal import Decimal

import pytest

from price_specialist.catalog import ControlPriceEntry
from price_specialist.enums import CalculationStatus, CollectionStatus, PriceStatus
from price_specialist.errors import AmbiguousControlPrice
from price_specialist.pricing import evaluate_price, parse_price, resolve_control_price
from price_specialist.schemas import CollectionResult


def entry(spec: str | None, price: str) -> ControlPriceEntry:
    return ControlPriceEntry("希诺彤", "罗沙司他胶囊", spec, Decimal(price), "粒", f"希诺彤 {spec} {price}")


def test_parse_price_rejects_ranges_and_missing_values() -> None:
    assert parse_price("¥211.89") == Decimal("211.89")
    assert parse_price("20-30") is None
    assert parse_price(None) is None


def test_control_price_requires_exact_spec_when_brand_has_multiple_prices() -> None:
    entries = [entry("20mg", "1.65"), entry("50mg", "3.30")]
    assert resolve_control_price(entries, brand="希诺彤", spec="20mg*7粒").price == Decimal("1.65")
    with pytest.raises(AmbiguousControlPrice):
        resolve_control_price(entries, brand="希诺彤", spec="30mg*7粒")
    with pytest.raises(AmbiguousControlPrice):
        resolve_control_price(entries, brand="希诺彤", spec=None)


def test_price_calculation_never_defaults_missing_box_count() -> None:
    result = CollectionResult(collection_status=CollectionStatus.SUCCESS, page_price_raw="¥79.99")
    evaluated = evaluate_price(
        result,
        expected_box_count=Decimal("1"),
        units_per_box=Decimal("28"),
        min_unit="片",
        control_price=Decimal("0.8"),
    )
    assert evaluated.calculation_status == CalculationStatus.MISSING_PACK
    assert evaluated.price_status == PriceStatus.NOT_EVALUATED


def test_price_calculation_requires_box_cross_validation() -> None:
    result = CollectionResult(
        collection_status=CollectionStatus.SUCCESS,
        page_price_raw="¥20.58",
        sale_box_count=Decimal("2"),
    )
    evaluated = evaluate_price(
        result,
        expected_box_count=Decimal("2"),
        units_per_box=Decimal("7"),
        min_unit="片",
        control_price=Decimal("1.47"),
    )
    assert evaluated.calculation_status == CalculationStatus.SUCCESS
    assert evaluated.single_unit_price == Decimal("1.4700")
    assert evaluated.price_status == PriceStatus.AT_CONTROL

