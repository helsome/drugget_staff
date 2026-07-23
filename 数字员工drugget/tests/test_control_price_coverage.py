"""Coverage report tests against the real curated control-price CSV.

These tests assert the verified state of ``data/knowledge-base/control_price_rules.csv``
as of 2026-07-23. The report is strictly read-only; it must never mutate the CSV or any
``business_confirmed`` flag.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scripts.control_price_coverage import compute_coverage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = PROJECT_ROOT / "data/knowledge-base/control_price_rules.csv"
TODAY = date(2026, 7, 23)


def test_coverage_counts_against_real_csv() -> None:
    report = compute_coverage(CSV_PATH, today=TODAY)

    assert report["total_rules"] == 32
    assert report["business_confirmed"] == 1
    assert report["pending"] == 31
    assert report["active"] == 32
    assert report["comparable"] == 1
    assert report["strength_only_specs"] == 6
    assert report["spec_less"] == 25

    assert report["comparable_drugs"] == ["葛泰"]

    assert len(report["stale_conflicts"]) == 1
    conflict = report["stale_conflicts"][0]
    assert conflict["brand"] == "葛泰"
    assert conflict["generic_name"] == "地奥司明片"
    assert conflict["detail"]


def test_strength_only_and_comparable_drugs_match_audit() -> None:
    report = compute_coverage(CSV_PATH, today=TODAY)

    # 葛泰 0.45g*20片 is the single confirmed, parseable, effective rule.
    assert report["comparable_drugs"] == ["葛泰"]

    # The 6 strength-only spec_keys called out in the prior audit.
    expected_strength_only_brands = {"希诺彤", "倍利舒", "托妥", "晴瑞欣", "品定"}
    # strength_only_specs is a count; comparable/pending drug lists carry the brands.
    assert report["strength_only_specs"] == 6
    assert expected_strength_only_brands.issubset(set(report["pending_drugs"]))


def test_pending_drugs_is_sorted_unique_brand_list() -> None:
    report = compute_coverage(CSV_PATH, today=TODAY)
    pending = report["pending_drugs"]

    assert isinstance(pending, list)
    assert pending == sorted(pending)
    assert len(pending) == len(set(pending))
    assert len(pending) == 30
    # 葛泰 retains a stale spec-less pending row alongside its confirmed full-spec row,
    # so it still appears among pending drugs (and is flagged as a stale conflict).
    assert "葛泰" in pending


def test_default_today_matches_known_counts() -> None:
    # Counts that do not depend on today must hold under the default date.today().
    report = compute_coverage(CSV_PATH)

    assert report["total_rules"] == 32
    assert report["business_confirmed"] == 1
    assert report["strength_only_specs"] == 6
    assert report["spec_less"] == 25


def test_report_is_json_serializable() -> None:
    report = compute_coverage(CSV_PATH, today=TODAY)
    # Must be safe to echo from the CLI as JSON.
    serialized = json.dumps(report, ensure_ascii=False)
    assert "葛泰" in serialized
