"""Control-price coverage report generator (read-only).

Computes a coverage summary of the curated control-price rules CSV:
how many rules are business-confirmed, pending, active, comparable
(active + confirmed + effective + parseable package spec), and how many
spec_keys are strength-only (non-empty but unparseable) or spec-less.
It also flags stale conflicts where a (brand, generic_name) has both a
spec-less pending row and a confirmed full-spec row.

This module never mutates the CSV or any ``business_confirmed`` flag.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from price_specialist.catalog import (
    ControlPriceEntry,
    normalize_spec,
    parse_control_price_rules,
    parse_package_units,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = PROJECT_ROOT / "data" / "knowledge-base" / "control_price_rules.csv"


def _spec_is_parseable(spec_key: str | None) -> bool:
    """Return True when spec_key parses to a package count (mirrors resolve_control_price)."""
    normalized = (normalize_spec(spec_key) or "").replace(" ", "")
    return parse_package_units(normalized)[0] is not None


def _is_effective(entry: ControlPriceEntry, today: date) -> bool:
    return (
        entry.effective_from is not None
        and entry.effective_from <= today
        and (entry.effective_to is None or today <= entry.effective_to)
    )


def _is_comparable(entry: ControlPriceEntry, today: date) -> bool:
    """Mirror the comparability gate in pricing.resolve_control_price."""
    return (
        entry.active
        and entry.business_confirmed
        and _is_effective(entry, today)
        and _spec_is_parseable(entry.spec_key)
    )


def compute_coverage(csv_path: Path, *, today: date | None = None) -> dict:
    """Compute a read-only coverage report for the control-price rules CSV.

    Args:
        csv_path: Path to ``control_price_rules.csv``.
        today: Reference date for effectiveness; defaults to ``date.today()``.

    Returns:
        A JSON-serializable dict with keys: ``total_rules``, ``business_confirmed``,
        ``pending``, ``active``, ``comparable``, ``strength_only_specs``,
        ``spec_less``, ``stale_conflicts``, ``comparable_drugs``, ``pending_drugs``.
    """
    reference_day = today or date.today()
    entries = parse_control_price_rules(Path(csv_path))

    total_rules = len(entries)
    business_confirmed = sum(1 for entry in entries if entry.business_confirmed)
    pending = sum(1 for entry in entries if not entry.business_confirmed)
    active = sum(1 for entry in entries if entry.active)

    spec_less = 0
    strength_only = 0
    comparable_count = 0
    comparable_drugs: set[str] = set()
    pending_drugs: set[str] = set()

    for entry in entries:
        if not entry.spec_key:
            spec_less += 1
        elif not _spec_is_parseable(entry.spec_key):
            strength_only += 1

        if not entry.business_confirmed:
            pending_drugs.add(entry.brand)
        if _is_comparable(entry, reference_day):
            comparable_count += 1
            comparable_drugs.add(entry.brand)

    # Stale conflict: same (brand, generic_name) has both a spec-less pending
    # row and a confirmed full-spec row -> the spec-less row is dead data.
    groups: dict[tuple[str, str], list[ControlPriceEntry]] = defaultdict(list)
    for entry in entries:
        groups[(entry.brand, entry.generic_name)].append(entry)

    stale_conflicts: list[dict[str, str]] = []
    for (brand, generic_name), group in groups.items():
        stale_rows = [entry for entry in group if not entry.spec_key and not entry.business_confirmed]
        confirmed_rows = [
            entry for entry in group if entry.business_confirmed and _spec_is_parseable(entry.spec_key)
        ]
        if stale_rows and confirmed_rows:
            superseded = confirmed_rows[0]
            stale_conflicts.append(
                {
                    "brand": brand,
                    "generic_name": generic_name,
                    "detail": (
                        f"同时存在规格缺失的未确认行与已确认完整规格行"
                        f"({superseded.spec_key})，规格缺失行属失效数据"
                    ),
                }
            )

    stale_conflicts.sort(key=lambda item: (item["brand"], item["generic_name"]))

    return {
        "total_rules": total_rules,
        "business_confirmed": business_confirmed,
        "pending": pending,
        "active": active,
        "comparable": comparable_count,
        "strength_only_specs": strength_only,
        "spec_less": spec_less,
        "stale_conflicts": stale_conflicts,
        "comparable_drugs": sorted(comparable_drugs),
        "pending_drugs": sorted(pending_drugs),
    }


def main() -> None:
    """Print the coverage report as JSON to stdout."""
    parser = argparse.ArgumentParser(description="生成控价规则覆盖率报告（只读）")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_CSV,
        help="控价规则 CSV 路径",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="参考日期 YYYY-MM-DD，默认今天",
    )
    args = parser.parse_args()
    report = compute_coverage(args.source, today=args.today)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
