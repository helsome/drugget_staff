"""Composition-root safety checks for the mandatory review gate."""
from __future__ import annotations

from pathlib import Path

import pytest

from price_specialist.config import Settings
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.review_factory import build_review_orchestrator, resolve_review_mode


def _settings(tmp_path: Path) -> Settings:
    return Settings.load(
        tmp_path,
        overrides={
            "PRICE_SPECIALIST_DATABASE_URL": "sqlite:///:memory:",
            "PRICE_SPECIALIST_EVIDENCE_DIR": "evidence",
            "PRICE_SPECIALIST_OUTPUT_DIR": "outputs",
        },
    )


def test_composition_root_uses_fake_only_for_test_runtime(tmp_path: Path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    with make_session_factory(engine)() as session:
        review = build_review_orchestrator(
            session=session, settings=_settings(tmp_path), run_id="run-1",
            event_sink=None, runtime_mode="test",
        )
    assert review.review_mode == "fake"
    assert review.formal_release_enabled is True


def test_production_forbids_fake_and_defaults_to_fail_closed_shadow(tmp_path: Path) -> None:
    assert resolve_review_mode(runtime_mode="production", review_mode=None) == "codex_shadow"
    with pytest.raises(ValueError, match="forbids review_mode='fake'"):
        resolve_review_mode(runtime_mode="production", review_mode="fake")
    with pytest.raises(ValueError, match="forbids review_mode='disabled'"):
        resolve_review_mode(runtime_mode="production", review_mode="disabled")

    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    with make_session_factory(engine)() as session:
        review = build_review_orchestrator(
            session=session, settings=_settings(tmp_path), run_id="run-2",
            event_sink=None, runtime_mode="production",
        )
    assert review.review_mode == "codex_shadow"
    assert review.formal_release_enabled is False


def test_production_rejects_disabled_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_SPECIALIST_REVIEW_MODE", "disabled")
    with pytest.raises(ValueError, match="forbids review_mode='disabled'"):
        resolve_review_mode(runtime_mode="production")
