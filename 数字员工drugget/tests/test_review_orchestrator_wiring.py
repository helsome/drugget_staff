"""Wiring tests for the ReviewOrchestrator gate and its BatchOrchestrator integration.

Covers:
  * L1: below-control 葛泰 observation -> agent accepts -> formal price confirmed.
  * L1: above-guidance observation -> no review -> skipped, formal price confirmed.
  * L1: agent failure -> contained, formal price pending, no exception escapes.
  * L2: BatchOrchestrator with a wired ReviewOrchestrator gates is_formal_price
    on the review outcome for a below-control 葛泰 detail result.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from price_specialist.agent_review import AgentProposal, AgentReviewService, FakeAgentReviewer
from price_specialist.agent_validator import AgentProposalValidator
from price_specialist.alerts import AlertDryRunService
from price_specialist.collector import ComputerUseCollector
from price_specialist.config import Settings
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.decisions import PriceDecisionService
from price_specialist.enums import CollectionStatus, TaskType
from price_specialist.evidence import EvidenceStore
from price_specialist.models import (
    CollectionRun,
    CollectionTask,
    ControlPriceVersion,
    DrugProduct,
    PackageMaster,
    PriceBreakEvent,
    PriceComparison,
    PriceObservation,
    SearchCandidate,
)
from price_specialist.orchestrator import BatchOrchestrator, RatePolicy
from price_specialist.review_orchestrator import ReviewOrchestrator
from price_specialist.review_factory import build_review_orchestrator
from price_specialist.review_policy import ReviewPolicy
from price_specialist.schemas import (
    CollectionResult,
    CollectionTaskSpec,
    EvidenceBundle,
    SearchHit,
)
from price_specialist.services import TaskQueueService


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_getai_rule(session, *, unit_price: Decimal) -> PriceObservation:
    """Seed the 葛泰/地奥司明片 control rule + a detail observation.

    Mirrors tests/test_price_decisions.py: a verified 0.45g*20片 package and a
    business-confirmed 1.15/片 control price. The observation's single unit
    price is parameterized so the same seed covers below/above guidance cases.
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
        captured_at=datetime(2026, 7, 20), page_shop="责任店",
        selected_spec="0.45g*20片", page_price_value=Decimal("16.17"),
        single_box_price=Decimal("16.17"), single_unit_price=unit_price,
        min_unit="片", collection_status="success", calculation_status="success",
        price_status="not_evaluated", evidence_path="/evidence/detail.json",
        evidence_sha256="abc",
    )
    session.add(observation)
    session.flush()
    session.add(ControlPriceVersion(
        drug_id=drug.id, spec_key="0.45g*20片", price_per_min_unit=Decimal("1.15"),
        min_unit="片", effective_from=date(2026, 4, 1), source="审批表",
        source_line="葛泰 0.45g*20片 1.15", active=True, business_confirmed=True,
        confirmed_by="业务", confirmed_at=date(2026, 7, 20), approval_reference="APP-1",
    ))
    session.flush()
    return observation


def _build_review_orchestrator(session, evidence_root: Path, *, reviewer=None, run_id: str = "") -> ReviewOrchestrator:
    return ReviewOrchestrator(
        session=session,
        decision_service=PriceDecisionService(session),
        review_policy=ReviewPolicy(),
        review_service=AgentReviewService(reviewer or FakeAgentReviewer(), evidence_root=evidence_root),
        validator=AgentProposalValidator(),
        alert_service=AlertDryRunService(),
        evidence_root=evidence_root,
        emit=lambda _event: None,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# L1: below-control -> agent accepts -> formal price confirmed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_below_control_acceptance_confirms_formal_price(tmp_path: Path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai_rule(db, unit_price=Decimal("0.8085"))
        review_orchestrator = _build_review_orchestrator(db, tmp_path, run_id=observation.run_id)

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

        # Agent I/O persisted under evidence_root/<event>/agent-review/.
        agent_dir = tmp_path / event.id / "agent-review"
        assert (agent_dir / "request.json").is_file()
        assert (agent_dir / "final.json").is_file()
        assert (agent_dir / "attempt-1-result.json").is_file()


# ---------------------------------------------------------------------------
# L1: above guidance -> no review -> skipped, formal price confirmed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_above_guidance_skips_review_and_confirms(tmp_path: Path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        # 1.50/片 > 1.15/片 guidance -> not_below_control, no review required.
        observation = _seed_getai_rule(db, unit_price=Decimal("1.50"))
        review_orchestrator = _build_review_orchestrator(db, tmp_path, run_id=observation.run_id)

        outcome = await review_orchestrator.review_observation(observation)

        assert outcome.skipped is True
        assert outcome.formal_price_status == "confirmed"
        assert outcome.event is None
        assert outcome.comparison.verdict == "not_below_control"
        assert outcome.comparison.review_required is False
        assert outcome.comparison.formal_price_status == "confirmed"
        assert db.scalar(select(PriceBreakEvent)) is None


@pytest.mark.asyncio
async def test_above_guidance_with_sku_ambiguity_dispatches_and_cannot_direct_confirm(tmp_path: Path) -> None:
    """A comparable non-low price with explicit evidence risk enters review."""
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai_rule(db, unit_price=Decimal("1.50"))
        observation.raw_evidence = {"sku_ambiguous": True}
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, reviewer=_RaisingReviewer(), run_id=observation.run_id,
        )

        outcome = await review_orchestrator.review_observation(observation)

        assert outcome.skipped is False
        assert outcome.comparison.verdict == "not_below_control"
        assert outcome.comparison.review_required is True
        assert outcome.comparison.review_reason == "sku_ambiguous"
        assert outcome.comparison.review_status == "agent_failed"
        assert outcome.comparison.formal_price_status == "pending"
        event = db.scalar(select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id))
        assert event is not None
        assert (tmp_path / event.id / "agent-review" / "request.json").is_file()


# ---------------------------------------------------------------------------
# L1: agent failure -> contained, formal price pending, no exception escapes
# ---------------------------------------------------------------------------

class _RaisingReviewer:
    """A reviewer that always raises, to exercise the failure containment path."""

    async def review(self, request: dict) -> AgentProposal:
        raise RuntimeError("agent is down")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_control_rule", "make_detail_price_missing", "expected_status"),
    [
        (False, False, "captured_uncompared"),
        (True, True, "blocked"),
    ],
)
async def test_not_comparable_ignores_auxiliary_evidence_triggers(
    tmp_path: Path,
    with_control_rule: bool,
    make_detail_price_missing: bool,
    expected_status: str,
) -> None:
    """Auxiliary page evidence never dispatches an otherwise non-comparable row."""
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai_rule(db, unit_price=Decimal("0.8085"))
        if not with_control_rule:
            rule = db.scalar(select(ControlPriceVersion))
            assert rule is not None
            db.delete(rule)
            db.flush()
        observation.raw_evidence = {"sku_ambiguous": True, "page_changed": True}
        if make_detail_price_missing:
            observation.single_unit_price = None
        review_orchestrator = _build_review_orchestrator(db, tmp_path, run_id=observation.run_id)

        outcome = await review_orchestrator.review_observation(observation)

        assert outcome.comparison.verdict == "not_comparable"
        assert outcome.skipped is True
        assert outcome.comparison.formal_price_status == expected_status
        assert db.scalar(select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id)) is None


@pytest.mark.asyncio
async def test_agent_failure_does_not_block_and_marks_pending(tmp_path: Path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        observation = _seed_getai_rule(db, unit_price=Decimal("0.8085"))
        review_orchestrator = _build_review_orchestrator(
            db, tmp_path, reviewer=_RaisingReviewer(), run_id=observation.run_id,
        )

        # Must not raise - agent failure is contained inside review_observation.
        outcome = await review_orchestrator.review_observation(observation)

        comparison = outcome.comparison
        assert comparison.verdict == "below_control"
        assert comparison.review_status == "agent_failed"
        assert comparison.formal_price_status == "pending"

        event = db.scalar(
            select(PriceBreakEvent).where(PriceBreakEvent.observation_id == observation.id)
        )
        assert event is not None
        assert event.review_status == "agent_failed"
        assert event.review_error_code == "RuntimeError"
        # request.json is written before the reviewer is called; final.json is not.
        agent_dir = tmp_path / event.id / "agent-review"
        assert (agent_dir / "request.json").is_file()
        assert not (agent_dir / "final.json").is_file()


# ---------------------------------------------------------------------------
# L2: BatchOrchestrator gates is_formal_price on the review outcome
# ---------------------------------------------------------------------------

async def _no_sleep() -> None:
    return None


class _GetaiDetailCollector(ComputerUseCollector):
    """Returns a 葛泰 search hit + a below-control detail success (0.8085/片)."""

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        return [SearchHit(
            platform=session.platform, query=query, rank=1,
            title="葛泰 0.45g*20片 地奥司明片", product_id="getai-prod-1",
            url="https://item.taobao.com/item.htm?id=getai-1", shop_name="葛泰大药房",
        )]

    async def inspect_candidate(self, task, session):
        return CollectionResult(
            collection_status=CollectionStatus.SUCCESS,
            page_shop="葛泰大药房", selected_spec="0.45g*20片",
            page_price_value=Decimal("16.17"), single_box_price=Decimal("16.17"),
            single_unit_price=Decimal("0.8085"), min_unit="片", units_per_box=Decimal("20"),
            evidence=EvidenceBundle(raw_fields={"price": "16.17"}),
        )

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


@pytest.mark.asyncio
async def test_batch_orchestrator_gates_formal_price_on_review(tmp_path: Path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        drug = DrugProduct(brand_name="葛泰", generic_name="地奥司明片")
        run = CollectionRun(id="getai-run")
        db.add_all([drug, run])
        db.flush()
        db.add(PackageMaster(
            drug_id=drug.id, spec_raw="0.45g*20片", spec_normalized="0.45g*20片",
            units_per_box=Decimal("20"), min_unit="片", source="test", verified=True,
        ))
        db.add(ControlPriceVersion(
            drug_id=drug.id, spec_key="0.45g*20片", price_per_min_unit=Decimal("1.15"),
            min_unit="片", effective_from=date(2026, 4, 1), source="审批表",
            source_line="葛泰 0.45g*20片 1.15", active=True, business_confirmed=True,
            confirmed_by="业务", confirmed_at=date(2026, 7, 20), approval_reference="APP-1",
        ))
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="getai-search", run_id=run.id, platform="taobao",
            task_type=TaskType.SEARCH, session_alias="taobao-p0",
            query="葛泰 地奥司明片", drug_name="葛泰", generic_name="地奥司明片",
            metadata={"drug_id": drug.id, "target_brand": "葛泰", "inspect_limit": 1},
        ))
        db.commit()

        settings = Settings.load(
            tmp_path,
            overrides={
                "PRICE_SPECIALIST_DATABASE_URL": "sqlite:///:memory:",
                "PRICE_SPECIALIST_EVIDENCE_DIR": "evidence",
                "PRICE_SPECIALIST_OUTPUT_DIR": "outputs",
            },
        )
        review_orchestrator = build_review_orchestrator(
            session=db, settings=settings, run_id=run.id, event_sink=None,
            runtime_mode="test",
        )
        runner = BatchOrchestrator(
            session=db, collector=_GetaiDetailCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
            review_orchestrator=review_orchestrator,
        )
        outcomes = await runner.execute_all({"taobao": "taobao-p0"})

        assert outcomes[0]["completed"] >= 2, "search + inspect tasks should both complete"

        # The detail observation was judged below_control and the agent accepted.
        comparison = db.scalar(select(PriceComparison))
        assert comparison is not None
        assert comparison.verdict == "below_control"
        assert comparison.review_status == "accepted"
        assert comparison.formal_price_status == "confirmed"

        # A PriceBreakEvent was created with an accept decision.
        event = db.scalar(select(PriceBreakEvent))
        assert event is not None
        assert event.review_decision == "accept"

        # The composition root supplies the test-only fake reviewer, so the
        # candidate is released only after the review outcome is confirmed.
        candidate = db.scalar(select(SearchCandidate).where(SearchCandidate.run_id == run.id))
        assert candidate is not None
        assert candidate.is_formal_price is True

        # Agent I/O persisted under evidence_root/<event>/agent-review/.
        agent_dir = tmp_path / "evidence" / event.id / "agent-review"
        assert (agent_dir / "request.json").is_file()
        assert (agent_dir / "final.json").is_file()
