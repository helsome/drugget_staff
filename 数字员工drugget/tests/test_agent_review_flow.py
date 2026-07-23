"""L0/L1/L2 tests for the agent-review state machine (Stage 1).

Stage 1 covers: model migration, ReviewPolicy, AgentProposal/FakeAgentReviewer,
the deterministic Validator, and the orchestrator review gate.
"""
from __future__ import annotations

import sqlalchemy as sa

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
