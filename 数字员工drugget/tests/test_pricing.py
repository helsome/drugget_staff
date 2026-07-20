from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from price_specialist.catalog import ControlPriceEntry, parse_control_price_rules
from price_specialist.enums import CalculationStatus, CollectionStatus, PriceStatus
from price_specialist.errors import AmbiguousControlPrice
from price_specialist.pricing import evaluate_price, parse_price, resolve_control_price
from price_specialist.schemas import CollectionResult


def entry(spec: str | None, price: str, *, confirmed: bool = True) -> ControlPriceEntry:
    return ControlPriceEntry(
        "希诺彤",
        "罗沙司他胶囊",
        spec,
        Decimal(price),
        "粒",
        f"希诺彤 {spec} {price}",
        effective_from=date(2026, 4, 1),
        business_confirmed=confirmed,
    )


def test_parse_price_rejects_ranges_and_missing_values() -> None:
    assert parse_price("¥211.89") == Decimal("211.89")
    assert parse_price("20-30") is None
    assert parse_price(None) is None


def test_control_price_requires_full_exact_spec() -> None:
    entries = [entry("20mg*7粒", "1.65"), entry("50mg*7粒", "3.30")]
    assert resolve_control_price(entries, brand="希诺彤", spec="20mg*14粒") is None
    assert resolve_control_price(entries, brand="希诺彤", spec="20mg*7粒", on_date=date(2026, 7, 20)).price == Decimal("1.65")
    assert resolve_control_price(entries, brand="希诺彤", spec="20mg", on_date=date(2026, 7, 20)) is None


def test_unconfirmed_or_incomplete_rules_are_ineligible() -> None:
    assert resolve_control_price(
        [entry("20mg*7粒", "1.65", confirmed=False)],
        brand="希诺彤",
        spec="20mg*7粒",
        on_date=date(2026, 7, 20),
    ) is None
    assert resolve_control_price(
        [entry("20mg", "1.65")],
        brand="希诺彤",
        spec="20mg*7粒",
        on_date=date(2026, 7, 20),
    ) is None


def test_existing_getai_and_tuotuo_rules_are_pending_business_confirmation() -> None:
    path = Path(__file__).parents[1] / "data/knowledge-base/control_price_rules.csv"
    entries = {entry.brand: entry for entry in parse_control_price_rules(path) if entry.brand in {"葛泰", "托妥"}}
    assert entries["葛泰"].spec_key is None
    assert entries["托妥"].spec_key == "10mg"
    assert not entries["葛泰"].business_confirmed
    assert not entries["托妥"].business_confirmed
    assert resolve_control_price(entries.values(), brand="葛泰", spec="0.45g*20片", on_date=date(2026, 7, 20)) is None
    assert resolve_control_price(entries.values(), brand="托妥", spec="10mg*48片", on_date=date(2026, 7, 20)) is None


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
