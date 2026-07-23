"""L1 replay + L2 review-outcome tests for the ReviewOrchestrator state machine.

The L1 test drives the full decide -> policy -> agent -> validator chain from a
captured raw-evidence fixture (``tests/fixtures/replay/getai_below_control.json``)
so the replay corpus is established for Stage 2 parsing. The L2 cases pin the
review_status / formal_price_status mapping for every validator decision plus
the agent-failure containment path and the guidance-missing skip.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from price_specialist.agent_review import (
    AgentProposal,
    AgentReviewService,
    FakeAgentReviewer,
)
from price_specialist.agent_validator import AgentProposalValidator
from price_specialist.alerts import AlertDryRunService
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.decisions import PriceDecisionService
from price_specialist.models import (
    CollectionRun,
    CollectionTask,
    ControlPriceVersion,
    DrugProduct,
    PackageMaster,
    PriceBreakEvent,
    PriceObservation,
)
from price_specialist.review_orchestrator import ReviewOrchestrator
from price_specialist.review_policy import ReviewPolicy

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "replay" / "getai_below_control.json"


# ---------------------------------------------------------------------------
# Fixture + seed helpers
# ---------------------------------------------------------------------------

def _load_fixture() -> dict:
    """Load the captured 葛泰 raw-evidence fixture (offline, no network)."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _fixture_sha256(fixture: dict) -> str:
    """Stable evidence hash over the canonical fixture bytes."""
    raw = json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _seed_getai(
    session,
    *,
    unit_price: Decimal,
    raw_evidence: dict | None = None,
    evidence_sha256: str = "abc",
    with_control_rule: bool = True,
    page_shop: str = "药实在",
    final_url: str | None = None,
) -> PriceObservation:
    """Seed 葛泰/地奥司明片 0.45g*20片 + a detail observation.

    Mirrors the seed in tests/test_review_orchestrator_wiring.py (verified
    package, business-confirmed 1.15/片 control price) but parameterizes the
    raw-evidence payload, evidence hash, and whether a control rule exists so
    the same seed covers the replay + guidance-missing cases.
    """
    drug = DrugProduct(brand_name="葛泰", generic_name="地奥司明片")
    run = CollectionRun()
    session.add_all([drug, run])
    session.flush()
    task = CollectionTask(
        run_id=run.id, platform="yaoshibang", task_type="inspect_candidate",
        status="succeeded", session_alias="test",
        payload={"drug_name": "葛泰", "metadata": {"drug_id": drug.id}},
    )
    session.add(task)
    session.flush()
    session.add(PackageMaster(
        drug_id=drug.id, spec_raw="0.45g*20片", spec_normalized="0.45g*20片",
        units_per_box=Decimal("20"), min_unit="片", source="test", verified=True,
    ))
    observation = PriceObservation(
        run_id=run.id, task_id=task.id, channel="detail",
        captured_at=datetime(2026, 7, 20), page_shop=page_shop,
        final_url=final_url, selected_spec="0.45g*20片",
        page_price_value=Decimal("16.17"), single_box_price=Decimal("16.17"),
        single_unit_price=unit_price, min_unit="片",
        collection_status="success", calculation_status="success",
        price_status="not_evaluated", evidence_path="/evidence/detail.json",
        evidence_sha256=evidence_sha256, raw_evidence=raw_evidence or {},
    )
    session.add(observation)
    session.flush()
    if with_control_rule:
        session.add(ControlPriceVersion(
            drug_id=drug.id, spec_key="0.45g*20片", price_per_min_unit=Decimal("1.15"),
            min_unit="片", effective_from=date(2026, 4, 1), source="审批表",
            source_line="葛泰 0.45g*20片 1.15", active=True, business_confirmed=True,
            confirmed_by="业务", confirmed_at=date(2026, 7, 20), approval_reference="APP-1",
        ))
        session.flush()
    return observation


def _build_review_orchestrator(
    session, evidence_root: Path, *, reviewer=None, run_id: str = "",
) -> ReviewOrchestrator:
    return ReviewOrchestrator(
        session=session,
        decision_service=PriceDecisionService(session),
        review_policy=ReviewPolicy(),
        review_service=AgentReviewService(
            reviewer or FakeAgentReviewer(), evidence_root=evidence_root,
        ),
        validator=AgentProposalValidator(),
        alert_service=AlertDryRunService(),
        evidence_root=evidence_root,
        emit=lambda _event: None,
        run_id=run_id,
    )


def _proposal(decision: str, *, obs_price, **overrides) -> AgentProposal:
    """Build a clean AgentProposal honouring ``decision`` with sensible defaults.

    normalized_price echoes the observation's deterministic unit price so the
    validator's price-recompute rule never distorts the decision under test.
    """
    fields = dict(
        decision=decision,
        product_match=True,
        manufacturer_match=True,
        sku_match=True,
        price_verified=True,
        price_type="base_price",
        normalized_price=str(obs_price),
        min_purchase_quantity=1,
        confidence=0.95,
        reasons=[],
        evidence_pointers=["single_unit_price"],
        unresolved_questions=[],
        recommended_action=decision,
    )
    fields.update(overrides)
    return AgentProposal(**fields)


# ---------------------------------------------------------------------------
# Stub reviewers for the L2 outcome matrix
# ---------------------------------------------------------------------------

class _RecaptureReviewer:
    """Agent concludes the SKU is ambiguous -> recapture."""

    async def review(self, request: dict) -> AgentProposal:
        obs = request["observation"]
        return _proposal(
            "recapture", obs_price=obs["single_unit_price"],
            sku_match=False, confidence=0.92, reasons=["sku ambiguity on page"],
        )


class _HumanReviewReviewer:
    """Agent cannot fully verify the price -> human_review with open question."""

    async def review(self, request: dict) -> AgentProposal:
        obs = request["observation"]
        return _proposal(
            "human_review", obs_price=obs["single_unit_price"],
            price_verified=False, confidence=0.85,
            reasons=["promotional price not verifiable as long-term list price"],
            unresolved_questions=["页面促销价是否为长期挂牌价"],
        )


class _RejectReviewer:
    """Agent concludes the product does not match -> reject."""

    async def review(self, request: dict) -> AgentProposal:
        obs = request["observation"]
        return _proposal(
            "reject", obs_price=obs["single_unit_price"],
            product_match=False, price_verified=False, confidence=0.9,
            reasons=["page product is a different manufacturer"],
        )


class _TimeoutReviewer:
    """Agent dispatch raises -> exercises failure containment."""

    async def review(self, request: dict) -> AgentProposal:
        raise TimeoutError("agent timed out")


# ---------------------------------------------------------------------------
# L1: full-chain replay from a captured raw-evidence fixture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_getai_below_control_accepts_and_confirms(tmp_path: Path) -> None:
    """Replay a captured 葛泰 detail fixture through the full review chain.

    The fixture's deterministic values (16.17/box, 0.45g*20片, 20 units) yield a
    single_unit_price of 0.8085/片, below the 1.15/片 confirmed control price.
    The fake agent accepts, so the formal price is confirmed end to end.
    """
    fixture = _load_fixture()
    evidence_sha = _fixture_sha256(fixture)
    final_url = fixture["page_context"]["final_url"]

    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai(
            db,
            unit_price=Decimal("0.8085"),
            raw_evidence=fixture,
            evidence_sha256=evidence_sha,
            final_url=final_url,
            page_shop=fixture["page_context"]["page_shop"],
        )
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, run_id=observation.run_id,
        )

        outcome = await review_orchestrator.review_observation(observation)

        comparison = outcome.comparison
        assert comparison.verdict == "below_control"
        assert comparison.review_required is True
        assert comparison.review_status == "accepted"
        assert comparison.formal_price_status == "confirmed"
        assert outcome.skipped is False

        event = db.scalar(
            select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id)
        )
        assert event is not None
        assert event.review_decision == "accept"
        assert event.review_status == "accepted"
        assert event.reviewed_at is not None

        # The captured raw evidence is the auditable basis for the replay.
        assert observation.evidence_sha256 == evidence_sha
        assert observation.raw_evidence["product"]["title"] == "葛泰 地奥司明片 0.45g*20片"

        # Agent I/O persisted under evidence_root/<event>/agent-review/.
        agent_dir = tmp_path / event.id / "agent-review"
        assert agent_dir.is_dir()
        assert (agent_dir / "request.json").is_file()
        assert (agent_dir / "final.json").is_file()
        assert (agent_dir / "attempt-1-result.json").is_file()


# ---------------------------------------------------------------------------
# L2: review-outcome matrix (review_status, formal_price_status)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_l2_agent_recapture_blocks_formal_price(tmp_path: Path) -> None:
    """Agent recapture -> recapture_required / pending (formal price blocked)."""
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai(db, unit_price=Decimal("0.8085"))
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, reviewer=_RecaptureReviewer(), run_id=observation.run_id,
        )

        outcome = await review_orchestrator.review_observation(observation)

        assert outcome.comparison.verdict == "below_control"
        assert outcome.comparison.review_status == "recapture_required"
        # formal_price_status "pending" is the signal that gates is_formal_price
        # to False in BatchOrchestrator (see test_batch_orchestrator_gates_formal_price_on_review).
        assert outcome.comparison.formal_price_status == "pending"

        event = db.scalar(
            select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id)
        )
        assert event is not None
        assert event.review_status == "recapture_required"
        assert event.review_decision == "recapture"


@pytest.mark.asyncio
async def test_l2_agent_human_review_blocks_formal_price(tmp_path: Path) -> None:
    """Agent human_review + open question -> human_review_required / pending."""
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai(db, unit_price=Decimal("0.8085"))
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, reviewer=_HumanReviewReviewer(), run_id=observation.run_id,
        )

        outcome = await review_orchestrator.review_observation(observation)

        assert outcome.comparison.review_status == "human_review_required"
        assert outcome.comparison.formal_price_status == "pending"

        event = db.scalar(
            select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id)
        )
        assert event is not None
        assert event.review_status == "human_review_required"
        assert event.review_decision == "human_review"


@pytest.mark.asyncio
async def test_l2_agent_reject_blocks_formal_price(tmp_path: Path) -> None:
    """Agent reject + product_match False -> rejected / blocked."""
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai(db, unit_price=Decimal("0.8085"))
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, reviewer=_RejectReviewer(), run_id=observation.run_id,
        )

        outcome = await review_orchestrator.review_observation(observation)

        assert outcome.comparison.review_status == "rejected"
        assert outcome.comparison.formal_price_status == "blocked"

        event = db.scalar(
            select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id)
        )
        assert event is not None
        assert event.review_status == "rejected"
        assert event.review_decision == "reject"


@pytest.mark.asyncio
async def test_l2_agent_timeout_is_contained_and_marks_pending(tmp_path: Path) -> None:
    """Agent TimeoutError is contained -> agent_failed / pending, no raise."""
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai(db, unit_price=Decimal("0.8085"))
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, reviewer=_TimeoutReviewer(), run_id=observation.run_id,
        )

        # Must not raise - failure is contained so other tasks in the batch continue.
        outcome = await review_orchestrator.review_observation(observation)

        assert outcome.comparison.verdict == "below_control"
        assert outcome.comparison.review_status == "agent_failed"
        assert outcome.comparison.formal_price_status == "pending"

        event = db.scalar(
            select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id)
        )
        assert event is not None
        assert event.review_status == "agent_failed"
        assert event.review_error_code == "TimeoutError"


@pytest.mark.asyncio
async def test_l2_guidance_missing_skips_review_and_confirms_formal_price(tmp_path: Path) -> None:
    """No confirmed control rule -> not_comparable, no review, formal price confirmed.

    Guidance-missing does NOT block the formal price: ReviewPolicy only triggers
    on below_control, so a not_comparable observation is skipped and its
    formal_price_status stays "confirmed".
    """
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        # Seed the drug + verified package but NO ControlPriceVersion.
        observation = _seed_getai(db, unit_price=Decimal("0.8085"), with_control_rule=False)
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, run_id=observation.run_id,
        )

        outcome = await review_orchestrator.review_observation(observation)

        comparison = outcome.comparison
        assert comparison.verdict == "not_comparable"
        assert comparison.reason_code == "exact_confirmed_control_rule_missing"

        # ReviewPolicy only triggers on below_control; not_comparable -> None.
        assert ReviewPolicy().requires_review(comparison, observation) is None

        assert outcome.skipped is True
        assert comparison.review_required is False
        assert comparison.formal_price_status == "confirmed"
        assert outcome.formal_price_status == "confirmed"
        assert outcome.event is None
        assert db.scalar(select(PriceBreakEvent)) is None
