"""Stage 2A candidate governance is explicit and never fabricates approval."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from scripts.stage2a_control_price_gap_report import build_gap_report


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "data/knowledge-base/control_rule_candidates/stage2a_5_drugs.csv"

EXPECTED_FIELDS = [
    "brand", "generic_name", "manufacturer", "spec_key", "control_price_value",
    "control_price_basis", "control_price_per_min_unit", "min_unit", "effective_from",
    "effective_to", "source_file", "source_line", "business_confirmed", "confirmed_by",
    "confirmed_at", "approval_reference",
]


def test_stage2a_gap_report_preserves_current_governance_truth() -> None:
    report = build_gap_report(today=date(2026, 7, 23))

    assert report["summary"] == {
        "monitored_drug_count": 30,
        "control_rule_count": 32,
        "guidance_eligible_drug_count": 30,
        "guidance_missing_drug_count": 0,
        "guidance_eligible_drugs": sorted(row["brand"] for row in report["drugs"]),
    }
    getai = next(row for row in report["drugs"] if row["brand"] == "葛泰")
    assert getai["guidance_eligible_now"] is True
    assert None in getai["guidance_eligible_specs"]
    assert "pending_business_confirmation" in getai["guidance_gaps"]


def test_stage2a_candidate_template_has_five_representative_drugs_without_fake_approval() -> None:
    with CANDIDATES.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == EXPECTED_FIELDS
        rows = list(reader)

    assert [row["brand"] for row in rows] == ["葛泰", "晴诺舒", "优立维", "安多林", "畅凡"]
    confirmed = [row for row in rows if row["business_confirmed"] == "True"]
    assert len(confirmed) == 1
    assert confirmed[0]["brand"] == "葛泰"
    assert confirmed[0]["spec_key"] == "0.45g*20片"
    assert all(confirmed[0][field] for field in ("confirmed_by", "confirmed_at", "approval_reference"))

    pending = [row for row in rows if row["business_confirmed"] == "False"]
    assert len(pending) == 4
    for row in pending:
        assert row["confirmed_by"] == ""
        assert row["confirmed_at"] == ""
        assert row["approval_reference"] == ""
