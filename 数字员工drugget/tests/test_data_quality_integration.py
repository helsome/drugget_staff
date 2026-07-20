import json
from pathlib import Path

from price_specialist.catalog import BRAND_TO_GENERIC, parse_package_units
from price_specialist.data_quality import audit_sources


PROJECT = Path(__file__).resolve().parents[1]


def test_official_catalog_and_package_parser() -> None:
    assert len(BRAND_TO_GENERIC) == 30
    assert parse_package_units("80mg*2粒+125mg*1粒") == (3, "粒")
    assert parse_package_units("20mg*7片") == (7, "片")
    assert parse_package_units(None) == (None, None)


def test_historical_data_quality_contract() -> None:
    report = audit_sources(PROJECT / "过往抓取数据")
    assert report.source_rows["安托监控数据2026年4-6月.xlsx"] == 168_797
    assert report.source_rows["趣维1-3月总数据.xlsx"] == 18_176
    assert report.source_rows["7.14抓取结果/store_archive_full.json"] == 10_507
    assert report.recognized_rows == 186_619
    assert report.unrecognized_rows == 354
    assert len(report.covered_brands) == 14
    assert len(report.uncovered_brands) == 16
    assert report.quwei_exact_duplicates == 176
    assert report.quwei_business_key_duplicates == 301
    assert report.quwei_price_formula_mismatches == 172
    mismatch_dates = {
        issue.details["captured_date"]
        for issue in report.issues
        if issue.issue_type == "single_box_formula_mismatch"
    }
    assert mismatch_dates == {"2026-03-10"}


def test_generated_p0_artifacts_obey_discovery_and_smoke_rules() -> None:
    smoke = json.loads((PROJECT / "outputs/smoke/smoke_plan.json").read_text(encoding="utf-8"))
    search = json.loads((PROJECT / "outputs/search/offline_candidates.json").read_text(encoding="utf-8"))
    assert smoke["jd_unique_stores"] == 10
    assert smoke["taobao_unique_stores"] == 10
    assert len({item["shop_name"] for item in smoke["jd_targets"]}) == 10
    assert len({item["shop_name"] for item in smoke["taobao_targets"]}) == 10
    assert search["raw_item_count"] == 190
    assert search["classification_rate"] == 1.0
    assert search["formal_price_count"] == 0
