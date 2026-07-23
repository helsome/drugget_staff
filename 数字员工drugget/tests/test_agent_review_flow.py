"""L0/L1/L2 tests for the agent-review state machine (Stage 1).

Stage 1 covers: model migration, ReviewPolicy, AgentProposal/FakeAgentReviewer,
the deterministic Validator, and the orchestrator review gate.
"""
from __future__ import annotations

import sqlalchemy as sa
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from price_specialist.database import create_db_engine, init_database


def test_review_columns_exist_on_fresh_sqlite(tmp_path) -> None:
    """A freshly initialised database carries the review/formal-price columns."""
    engine = create_db_engine(f"sqlite:///{tmp_path}/fresh.db")
    init_database(engine)
    with engine.connect() as conn:
        cmp_cols = {row["name"] for row in sa.inspect(conn).get_columns("price_comparisons")}
        ev_cols = {row["name"] for row in sa.inspect(conn).get_columns("price_break_events")}
    assert {"review_required", "review_reason", "review_status", "formal_price_status"} <= cmp_cols
    assert {
        "review_status",
        "review_decision",
        "review_attempts",
        "reviewed_at",
        "review_evidence_path",
        "review_error_code",
        "review_summary",
    } <= ev_cols


def test_existing_db_gets_review_columns_via_alter(tmp_path) -> None:
    """An old-shape database (pre-review columns) is upgraded by init_database."""
    engine = create_db_engine(f"sqlite:///{tmp_path}/legacy.db")
    # Create the two tables with their pre-review shape only.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE price_observations (id VARCHAR(36) PRIMARY KEY)"
        ))
        conn.execute(sa.text(
            "CREATE TABLE price_comparisons ("
            "id VARCHAR(36) PRIMARY KEY,"
            "observation_id VARCHAR(36),"
            "verdict VARCHAR(40) NOT NULL,"
            "reason_code VARCHAR(100) NOT NULL"
            ")"
        ))
        conn.execute(sa.text(
            "CREATE TABLE price_break_events ("
            "id VARCHAR(36) PRIMARY KEY,"
            "observation_id VARCHAR(36) NOT NULL,"
            "routing_status VARCHAR(40) NOT NULL,"
            "event_status VARCHAR(40) NOT NULL DEFAULT 'dry_run',"
            "payload JSON"
            ")"
        ))
    init_database(engine)
    with engine.connect() as conn:
        cmp_cols = {row["name"] for row in sa.inspect(conn).get_columns("price_comparisons")}
        ev_cols = {row["name"] for row in sa.inspect(conn).get_columns("price_break_events")}
    assert {"review_required", "review_reason", "review_status", "formal_price_status"} <= cmp_cols
    assert {"review_status", "review_decision", "review_attempts"} <= ev_cols


# ---------------------------------------------------------------------------
# Task 1.3: AgentProposal schema + FakeAgentReviewer + AgentReviewService
# ---------------------------------------------------------------------------

def _below_control_observation() -> SimpleNamespace:
    """A 葛泰 below-control observation stub (page unit price 0.8085 < guidance 1.15)."""
    return SimpleNamespace(
        id="obs-1",
        single_unit_price=Decimal("0.8085"),
        page_price_value=Decimal("16.1700"),
        selected_spec="0.45g*20片",
        final_url="https://dian.ysbang.cn/druginfo?wholesaleId=1&providerId=2",
        evidence_sha256="abc123",
        min_unit="片",
        raw_evidence={"price_type": "base_price", "min_purchase_quantity": 10},
    )


def _below_control_comparison() -> SimpleNamespace:
    return SimpleNamespace(
        id="cmp-1",
        verdict="below_control",
        difference=Decimal("0.3415"),
        control_price=Decimal("1.1500"),
        control_price_version_id="cpv-1",
        rule_snapshot={"spec_key": "0.45g*20片", "price_per_min_unit": "1.1500"},
        detail_evidence_snapshot={"brand": "葛泰"},
    )


def _below_control_event() -> SimpleNamespace:
    return SimpleNamespace(id="evt-1", observation_id="obs-1", review_attempts=0)


def test_agent_proposal_round_trips_schema() -> None:
    """AgentProposal must carry every field of the §7.2 fixed JSON schema."""
    import json

    from price_specialist.agent_review import AgentProposal

    proposal = AgentProposal(
        decision="accept",
        product_match=True,
        manufacturer_match=True,
        sku_match=True,
        target_sku_id="SKU-001",
        price_verified=True,
        price_type="tier_price",
        normalized_price="28.00",
        min_purchase_quantity=10,
        confidence=0.94,
        reasons=["页面价格为10盒起批价"],
        evidence_pointers=["price_quotes[1].amount", "price_quotes[1].min_quantity"],
        unresolved_questions=[],
        recommended_action="accept",
    )
    restored = AgentProposal.model_validate_json(proposal.model_dump_json())
    assert restored.decision == "accept"
    assert restored.confidence == 0.94
    assert restored.evidence_pointers == ["price_quotes[1].amount", "price_quotes[1].min_quantity"]
    # round-trips through plain JSON
    as_dict = json.loads(proposal.model_dump_json())
    assert as_dict["target_sku_id"] == "SKU-001"


@pytest.mark.asyncio
async def test_fake_agent_reviewer_echoes_page_unit_price() -> None:
    """The Fake agent accepts and echoes the observation's deterministic unit price."""
    from price_specialist.agent_review import AgentReviewService, FakeAgentReviewer

    reviewer = FakeAgentReviewer()
    service = AgentReviewService(reviewer=reviewer, evidence_root=Path("/tmp/unused"))
    request = service.build_request(
        event=_below_control_event(),
        comparison=_below_control_comparison(),
        observation=_below_control_observation(),
    )
    proposal = await reviewer.review(request)
    assert proposal.decision == "accept"
    assert proposal.normalized_price == "0.8085"
    assert proposal.confidence >= 0.9


@pytest.mark.asyncio
async def test_dispatch_is_idempotent_for_same_input(tmp_path) -> None:
    """Same (event, evidence_sha256, control_price_version_id) must not re-dispatch."""
    from price_specialist.agent_review import AgentReviewService

    calls = {"count": 0}

    class _CountingReviewer:
        async def review(self, request: dict):
            calls["count"] += 1
            from price_specialist.agent_review import AgentProposal

            return AgentProposal(
                decision="accept", product_match=True, manufacturer_match=True,
                sku_match=True, price_verified=True, confidence=0.99,
                reasons=["ok"], evidence_pointers=["single_unit_price"],
                recommended_action="accept", normalized_price="0.8085",
            )

    service = AgentReviewService(reviewer=_CountingReviewer(), evidence_root=tmp_path)
    event = _below_control_event()
    comparison = _below_control_comparison()
    observation = _below_control_observation()
    first = await service.dispatch(session=None, event=event, comparison=comparison, observation=observation)
    second = await service.dispatch(session=None, event=event, comparison=comparison, observation=observation)
    assert calls["count"] == 1, "idempotent dispatch must not call the reviewer twice"
    assert first.normalized_price == second.normalized_price
    # I/O persisted to disk
    assert (tmp_path / event.id / "agent-review" / "request.json").is_file()
    assert (tmp_path / event.id / "agent-review" / "final.json").is_file()


@pytest.mark.asyncio
async def test_dispatch_records_attempt_files(tmp_path) -> None:
    from price_specialist.agent_review import AgentReviewService, FakeAgentReviewer

    service = AgentReviewService(reviewer=FakeAgentReviewer(), evidence_root=tmp_path)
    await service.dispatch(
        session=None,
        event=_below_control_event(),
        comparison=_below_control_comparison(),
        observation=_below_control_observation(),
    )
    attempt = tmp_path / "evt-1" / "agent-review" / "attempt-1-result.json"
    assert attempt.is_file()
