from pathlib import Path

from price_specialist.replay import audit_legacy_smoke


def test_legacy_smoke_is_rejected_under_strict_evidence_rules() -> None:
    project = Path(__file__).resolve().parents[1]
    report = audit_legacy_smoke(project / "7.14抓取结果/smoke_test_results_fixed.json")
    assert report["verdict"] == "NOT_PASSED"
    assert report["fixed_route"]["jd"]["valid_detail_pages"] == 8
    assert report["fixed_route"]["jd"]["calculation_eligible"] == 8
    assert report["fixed_route"]["taobao"]["valid_detail_pages"] == 10
    assert report["fixed_route"]["taobao"]["calculation_eligible"] == 0
    assert report["search_route"]["jd"]["raw_hits"] == 147
    assert report["search_route"]["jd"]["unique_hits"] == 106
    assert report["search_route"]["taobao"]["raw_hits"] == 43
    assert report["search_route"]["taobao"]["unique_hits"] == 37
    assert report["accuracy_metrics"]["page_price_accuracy"] is None

