"""Tests for the deterministic AgentProposalValidator (spec §8, Stage 1).

The validator re-checks the agent's self-reported fields deterministically;
hard rules override the agent's confidence. Stage 1 uses single-unit-price
equality and flat evidence-pointer resolution (SKU enrichment lands in Stage 2).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from price_specialist.agent_review import AgentProposal
from price_specialist.agent_validator import AgentProposalValidator, ValidationOutcome

_OBS_PRICE = Decimal("0.8085")


def _observation(
    *,
    single_unit_price: Decimal | None = _OBS_PRICE,
    raw_evidence: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="obs-1",
        single_unit_price=single_unit_price,
        page_price_value=Decimal("16.1700"),
        selected_spec="0.45g*20片",
        raw_evidence=raw_evidence
        if raw_evidence is not None
        else {"price_type": "base_price", "min_purchase_quantity": 10},
    )


def _comparison() -> SimpleNamespace:
    return SimpleNamespace(
        id="cmp-1",
        verdict="below_control",
        difference=Decimal("0.3415"),
        control_price=Decimal("1.1500"),
    )


def _proposal(**overrides) -> AgentProposal:
    """A default-valid AgentProposal; override fields per test."""
    defaults = dict(
        decision="accept",
        product_match=True,
        manufacturer_match=True,
        sku_match=True,
        target_sku_id="SKU-001",
        price_verified=True,
        price_type="base_price",
        normalized_price=str(_OBS_PRICE),
        min_purchase_quantity=10,
        confidence=0.95,
        reasons=["页面价格为10盒起批价"],
        evidence_pointers=["single_unit_price"],
        unresolved_questions=[],
        recommended_action="accept",
    )
    defaults.update(overrides)
    return AgentProposal(**defaults)


def test_hallucinated_price_routes_to_human_review() -> None:
    """(a) normalized_price != single_unit_price -> human_review, passed False."""
    validator = AgentProposalValidator()
    proposal = _proposal(normalized_price="0.9999")
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "human_review"
    assert outcome.passed is False
    assert "normalized_price" in outcome.reasons
    assert outcome.recomputed_price == Decimal("0.9999")


def test_all_pass_accepts() -> None:
    """(b) all rules pass + decision accept -> accepted, passed True."""
    validator = AgentProposalValidator()
    outcome = validator.validate(_proposal(), _observation(), _comparison())
    assert outcome.decision == "accepted"
    assert outcome.passed is True
    assert outcome.reasons == []
    assert outcome.recomputed_price == _OBS_PRICE


def test_low_confidence_routes_to_human_review() -> None:
    """(c) confidence below threshold -> human_review, reason 'confidence'."""
    validator = AgentProposalValidator()
    proposal = _proposal(confidence=0.8)
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "human_review"
    assert outcome.passed is False
    assert "confidence" in outcome.reasons


def test_unresolved_questions_route_to_human_review() -> None:
    """(d) non-empty unresolved_questions -> human_review."""
    validator = AgentProposalValidator()
    proposal = _proposal(unresolved_questions=["spec ambiguous: 0.45g vs 0.5g"])
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "human_review"
    assert outcome.passed is False
    assert "unresolved_questions" in outcome.reasons


def test_invalid_evidence_pointer_routes_to_human_review() -> None:
    """(e) evidence pointer not in raw_evidence or known set -> human_review."""
    validator = AgentProposalValidator()
    proposal = _proposal(evidence_pointers=["price_quotes[9].amount"])
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "human_review"
    assert outcome.passed is False
    assert "evidence_pointer" in outcome.reasons


def test_sku_match_false_routes_to_recapture() -> None:
    """(f) sku_match False -> recapture."""
    validator = AgentProposalValidator()
    proposal = _proposal(sku_match=False)
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "recapture"
    assert outcome.passed is False
    assert "sku_match" in outcome.reasons


def test_product_match_false_routes_to_reject() -> None:
    """(g) product_match False -> reject."""
    validator = AgentProposalValidator()
    proposal = _proposal(product_match=False)
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "reject"
    assert outcome.passed is False
    assert "product_match" in outcome.reasons


def test_agent_decision_recapture_honored() -> None:
    """(h) agent decision 'recapture' honored even when rules pass."""
    validator = AgentProposalValidator()
    proposal = _proposal(decision="recapture", recommended_action="recapture")
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "recapture"
    assert outcome.passed is False


def test_agent_decision_reject_honored() -> None:
    """(i) agent decision 'reject' honored even when rules pass."""
    validator = AgentProposalValidator()
    proposal = _proposal(decision="reject", recommended_action="reject")
    outcome = validator.validate(proposal, _observation(), _comparison())
    assert outcome.decision == "reject"
    assert outcome.passed is False
