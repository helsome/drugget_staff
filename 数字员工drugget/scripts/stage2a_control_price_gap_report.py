"""Read-only Stage 2A control-price and package-evidence gap report.

This report is deliberately descriptive: it never updates the curated rules,
does not set ``business_confirmed``, and does not write to SQLite.  The
designated price table is valid guidance; complete package information is a
page-evidence concern, not a precondition for guidance eligibility.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from price_specialist.catalog import parse_control_price_rules


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = PROJECT_ROOT / "data/knowledge-base/control_price_rules.csv"
DEFAULT_DRUGS = PROJECT_ROOT / "data/knowledge-base/drug_master.csv"
DEFAULT_PACKAGES = PROJECT_ROOT / "data/knowledge-base/drug_package_master.csv"


def _is_effective(*, effective_from: date | None, effective_to: date | None, today: date) -> bool:
    return (
        effective_from is not None
        and effective_from <= today
        and (effective_to is None or today <= effective_to)
    )


def build_gap_report(
    *,
    rules_path: Path = DEFAULT_RULES,
    drug_master_path: Path = DEFAULT_DRUGS,
    package_master_path: Path = DEFAULT_PACKAGES,
    today: date | None = None,
) -> dict[str, object]:
    """Return a JSON-serializable report for every monitored drug.

    ``drug_master.csv`` is the authoritative denominator.  Historical package
    rows indicate candidate SKU evidence only; they do not affect human
    confirmation status.
    """
    reference_day = today or date.today()
    rules = parse_control_price_rules(rules_path)

    rules_by_key: dict[tuple[str, str], list] = defaultdict(list)
    for rule in rules:
        rules_by_key[(rule.brand, rule.generic_name)].append(rule)

    packages_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with package_master_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            packages_by_key[(row["brand"], row["generic_name"])].append(row)

    drugs: list[dict[str, object]] = []
    with drug_master_path.open(encoding="utf-8-sig", newline="") as handle:
        for master in csv.DictReader(handle):
            key = (master["brand"], master["generic_name"])
            drug_rules = rules_by_key[key]
            package_rows = packages_by_key[key]
            effective_guidance = [
                rule for rule in drug_rules
                if rule.active
                and (rule.business_confirmed or rule.authority_basis == "designated_source")
                and _is_effective(
                    effective_from=rule.effective_from,
                    effective_to=rule.effective_to,
                    today=reference_day,
                )
            ]
            pending = [rule for rule in drug_rules if not rule.business_confirmed]
            gaps: list[str] = []
            if not package_rows:
                gaps.append("package_master_missing")
            if not effective_guidance:
                gaps.append("effective_guidance_missing")
            if pending:
                gaps.append("pending_business_confirmation")
            comparable = bool(effective_guidance)
            drugs.append(
                {
                    "brand": master["brand"],
                    "generic_name": master["generic_name"],
                    "category": master["category"],
                    "historical_coverage_status": master["coverage_status"],
                    "rule_count": len(drug_rules),
                    "business_confirmed_rule_count": sum(rule.business_confirmed for rule in drug_rules),
                    "pending_rule_count": len(pending),
                    "historical_package_candidate_count": len(package_rows),
                    "historical_package_specs": [row["spec_normalized"] for row in package_rows],
                    "guidance_eligible_specs": [rule.spec_key for rule in effective_guidance],
                    "guidance_eligible_now": comparable,
                    "guidance_gaps": gaps,
                }
            )

    comparable_drugs = [row["brand"] for row in drugs if row["guidance_eligible_now"]]
    return {
        "reference_date": reference_day.isoformat(),
        "sources": {
            "control_price_rules": str(rules_path),
            "drug_master": str(drug_master_path),
            "drug_package_master": str(package_master_path),
        },
        "summary": {
            "monitored_drug_count": len(drugs),
            "control_rule_count": len(rules),
            "guidance_eligible_drug_count": len(comparable_drugs),
            "guidance_missing_drug_count": len(drugs) - len(comparable_drugs),
            "guidance_eligible_drugs": comparable_drugs,
        },
        "drugs": drugs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Stage 2A 控价缺口报告（只读）")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    print(json.dumps(build_gap_report(today=args.today), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
