"""Tests for the business-facing CSV export (price_results, action_queue).

Spec §6: export_run_outputs always emits the business CSVs plus manifest.json;
the five technical audit CSVs are emitted only when debug_export=True.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

# Add collectors directory to path for export_fixture_run_csv import
_collectors_dir = Path(__file__).resolve().parent.parent / "collectors"
sys.path.insert(0, str(_collectors_dir))

from export_fixture_run_csv import export_run_outputs  # noqa: E402
from price_specialist.database import create_db_engine, init_database, make_session_factory  # noqa: E402
from price_specialist.models import (  # noqa: E402
    CollectionRun,
    CollectionTask,
    PriceBreakEvent,
    PriceComparison,
    PriceObservation,
)


PRICE_RESULTS_COLUMNS = [
    "run_id", "drug", "generic_name", "platform", "shop", "product_id", "sku_id",
    "selected_spec", "price_type", "page_price", "comparison_price", "guidance_price",
    "difference", "comparison_status", "review_status", "review_decision",
    "formal_price_status", "error_code", "evidence_path",
]

ACTION_QUEUE_COLUMNS = [
    "run_id", "drug", "platform", "shop", "action_type", "reason_code",
    "reason_detail", "review_status", "evidence_path", "recommended_action",
]

TECHNICAL_CSVS = [
    "collection_runs.csv",
    "collection_tasks.csv",
    "price_observations.csv",
    "search_candidates.csv",
    "incidents.csv",
]


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = [dict(zip(header, row)) for row in reader]
    return header, rows


@pytest.fixture()
def seeded_session(tmp_path):
    """Seed a run with one clean accepted observation and one guidance_missing observation."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'business.sqlite3'}")
    init_database(engine)
    factory = make_session_factory(engine)
    session = factory()

    run = CollectionRun(id="run-biz", status="succeeded")
    session.add(run)
    session.flush()

    task = CollectionTask(
        id="task-1",
        run_id=run.id,
        platform="yaoshibang",
        task_type="inspect_candidate",
        status="succeeded",
        session_alias="test",
        payload={
            "drug_name": "葛泰",
            "generic_name": "地奥司明片",
            "product_id": "P-001",
        },
    )
    session.add(task)
    session.flush()

    # Observation 1: clean accepted below_control result (no follow-up).
    obs1 = PriceObservation(
        id="obs-1",
        run_id=run.id,
        task_id=task.id,
        channel="detail",
        captured_at=datetime(2026, 7, 20),
        page_shop="责任店",
        selected_spec="0.45g*20片",
        page_price_value=Decimal("16.17"),
        comparison_price=Decimal("0.8085"),
        control_price=Decimal("1.15"),
        collection_status="success",
        calculation_status="success",
        price_status="not_evaluated",
        evidence_path="/evidence/obs1",
        raw_evidence={"selected_sku_id": "SKU-1", "price_type": "promotion"},
    )
    session.add(obs1)
    session.flush()

    comp1 = PriceComparison(
        id="comp-1",
        observation_id=obs1.id,
        verdict="below_control",
        reason_code="below_control_price",
        comparison_unit_price=Decimal("0.8085"),
        control_price=Decimal("1.15"),
        difference=Decimal("0.3415"),
        review_status="accepted",
        formal_price_status="confirmed",
    )
    session.add(comp1)
    session.flush()

    event1 = PriceBreakEvent(
        id="event-1",
        observation_id=obs1.id,
        comparison_id=comp1.id,
        routing_status="routed_dry_run",
        review_status="accepted",
        review_decision="accept",
    )
    session.add(event1)

    # Observation 2: not_comparable because the confirmed control rule is missing.
    obs2 = PriceObservation(
        id="obs-2",
        run_id=run.id,
        task_id=task.id,
        channel="detail",
        captured_at=datetime(2026, 7, 20),
        page_shop="药实在",
        selected_spec="0.45g*20片",
        page_price_value=Decimal("16.17"),
        collection_status="success",
        calculation_status="success",
        price_status="not_evaluated",
        evidence_path="/evidence/obs2",
        raw_evidence={},
    )
    session.add(obs2)
    session.flush()

    comp2 = PriceComparison(
        id="comp-2",
        observation_id=obs2.id,
        verdict="not_comparable",
        reason_code="exact_confirmed_control_rule_missing",
        comparison_unit_price=Decimal("0.8085"),
        control_price=None,
        difference=None,
        review_status=None,
        formal_price_status="pending",
    )
    session.add(comp2)

    session.commit()
    yield session
    session.close()


def test_business_csvs_emitted_without_debug(seeded_session, tmp_path):
    """debug_export=False writes business CSVs + manifest, skips technical CSVs."""
    output = tmp_path / "out"
    export_run_outputs("run-biz", seeded_session, output, debug_export=False)

    price_results_path = output / "price_results.csv"
    action_queue_path = output / "action_queue.csv"
    assert price_results_path.exists()
    assert action_queue_path.exists()
    assert (output / "manifest.json").exists()

    for name in TECHNICAL_CSVS:
        assert not (output / name).exists(), f"{name} should not be written when debug_export=False"

    header, rows = _read_csv(price_results_path)
    assert header == PRICE_RESULTS_COLUMNS
    assert len(rows) == 2
    by_shop = {row["shop"]: row for row in rows}

    r1 = by_shop["责任店"]
    assert r1["run_id"] == "run-biz"
    assert r1["drug"] == "葛泰"
    assert r1["generic_name"] == "地奥司明片"
    assert r1["platform"] == "yaoshibang"
    assert r1["product_id"] == "P-001"
    assert r1["sku_id"] == "SKU-1"
    assert r1["price_type"] == "promotion"
    assert r1["selected_spec"] == "0.45g*20片"
    assert r1["page_price"] == "16.17"
    assert r1["comparison_price"] == "0.8085"
    assert r1["guidance_price"] == "1.15"
    assert r1["difference"] == "0.3415"
    assert r1["comparison_status"] == "below_control"
    assert r1["review_status"] == "accepted"
    assert r1["review_decision"] == "accept"
    assert r1["formal_price_status"] == "confirmed"
    assert r1["evidence_path"] == "/evidence/obs1"

    r2 = by_shop["药实在"]
    assert r2["comparison_status"] == "not_comparable"
    assert r2["sku_id"] == ""
    assert r2["price_type"] == ""
    assert r2["guidance_price"] == ""
    # comparison_price falls back to comparison.comparison_unit_price.
    assert r2["comparison_price"] == "0.8085"

    header, rows = _read_csv(action_queue_path)
    assert header == ACTION_QUEUE_COLUMNS
    assert len(rows) == 1
    row = rows[0]
    assert row["action_type"] == "guidance_missing"
    assert row["reason_code"] == "exact_confirmed_control_rule_missing"
    assert row["drug"] == "葛泰"
    assert row["platform"] == "yaoshibang"
    assert row["shop"] == "药实在"
    assert row["recommended_action"]

    # Manifest is valid JSON and references the business files.
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-biz"
    assert "price_results.csv" in manifest["files"]


def test_technical_csvs_emitted_with_debug(seeded_session, tmp_path):
    """debug_export=True writes the five technical CSVs in addition to business CSVs."""
    output = tmp_path / "out"
    export_run_outputs("run-biz", seeded_session, output, debug_export=True)

    for name in TECHNICAL_CSVS:
        assert (output / name).exists(), f"{name} should be written when debug_export=True"
    assert (output / "price_results.csv").exists()
    assert (output / "action_queue.csv").exists()
    assert (output / "manifest.json").exists()
