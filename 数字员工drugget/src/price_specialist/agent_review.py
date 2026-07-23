"""Agent-review state machine (Stage 1).

Spec §7.2: a fixed JSON schema (``AgentProposal``) travels between the
orchestrator and a pluggable reviewer. Stage 1 ships a ``FakeAgentReviewer``
that echoes the deterministic page unit price, plus ``AgentReviewService``
which makes dispatch idempotent on ``(event, evidence_sha256,
control_price_version_id)``. Persistence is filesystem-only at this stage.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

AGENT_SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "1.0"


class AgentProposal(BaseModel):
    """Spec §7.2 fixed JSON schema for an agent review decision."""

    decision: str  # accept | recapture | human_review | reject
    product_match: bool
    manufacturer_match: bool
    sku_match: bool
    target_sku_id: str | None = None
    price_verified: bool
    price_type: str | None = None
    normalized_price: str | None = None
    min_purchase_quantity: int | None = None
    confidence: float
    reasons: list[str] = Field(default_factory=list)
    evidence_pointers: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_action: str


class AgentReviewer(Protocol):
    async def review(self, request: dict) -> AgentProposal: ...


class FakeAgentReviewer:
    """Stage-1 stub: echoes the observation's deterministic price as an accept."""

    async def review(self, request: dict) -> AgentProposal:
        obs = request["observation"]
        obs_price = obs["single_unit_price"]
        return AgentProposal(
            decision="accept",
            product_match=True,
            manufacturer_match=True,
            sku_match=True,
            target_sku_id=obs.get("selected_sku_id"),
            price_verified=True,
            price_type=obs.get("price_type", "base_price"),
            normalized_price=str(obs_price),
            min_purchase_quantity=obs.get("min_purchase_quantity", 1),
            confidence=0.99,
            reasons=["fake-agent: echoed page unit price"],
            evidence_pointers=["single_unit_price"],
            unresolved_questions=[],
            recommended_action="accept",
        )


def idempotency_key(
    *,
    event_id: str,
    evidence_sha256: str,
    control_price_version_id: str,
) -> str:
    """Stable hash of the (event, evidence, control-price) triple + schema/prompt."""
    raw = f"{event_id}|{evidence_sha256}|{control_price_version_id}|{AGENT_SCHEMA_VERSION}|{PROMPT_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
    )


class AgentReviewService:
    """Idempotent dispatcher between the orchestrator and an ``AgentReviewer``."""

    def __init__(self, reviewer: AgentReviewer, evidence_root: Path) -> None:
        self.reviewer = reviewer
        self.evidence_root = Path(evidence_root)

    def build_request(
        self,
        *,
        event: Any,
        comparison: Any,
        observation: Any,
    ) -> dict:
        """Assemble the review request dict consumed by ``reviewer.review``."""
        raw_evidence = getattr(observation, "raw_evidence", None) or {}
        rule_snapshot = getattr(comparison, "rule_snapshot", None) or {}
        detail_snapshot = getattr(comparison, "detail_evidence_snapshot", None) or {}
        obs = {
            "id": getattr(observation, "id", None),
            "single_unit_price": getattr(observation, "single_unit_price", None),
            "page_price_value": getattr(observation, "page_price_value", None),
            "selected_spec": getattr(observation, "selected_spec", None),
            "selected_sku_id": getattr(observation, "selected_sku_id", None),
            "min_unit": getattr(observation, "min_unit", None),
            "final_url": getattr(observation, "final_url", None),
            "evidence_sha256": getattr(observation, "evidence_sha256", None),
            "price_type": raw_evidence.get("price_type", "base_price"),
            "min_purchase_quantity": raw_evidence.get("min_purchase_quantity", 1),
            "raw_evidence": raw_evidence,
        }
        return {
            "schema_version": AGENT_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "event": {
                "id": getattr(event, "id", None),
                "observation_id": getattr(event, "observation_id", None),
            },
            "comparison": {
                "id": getattr(comparison, "id", None),
                "verdict": getattr(comparison, "verdict", None),
                "control_price": getattr(comparison, "control_price", None),
                "control_price_version_id": getattr(comparison, "control_price_version_id", None),
                "difference": getattr(comparison, "difference", None),
                "rule_snapshot": rule_snapshot,
                "detail_evidence_snapshot": detail_snapshot,
            },
            "observation": obs,
            "guidance": {"brand": detail_snapshot.get("brand"), "rule_snapshot": rule_snapshot},
        }

    async def dispatch(
        self,
        session: Any,
        *,
        event: Any,
        comparison: Any,
        observation: Any,
    ) -> AgentProposal:
        """Dispatch a review request idempotently; short-circuit on prior ``final.json``."""
        del session  # DB persistence lands in Stage 2; Stage 1 is filesystem-only.
        request = self.build_request(event=event, comparison=comparison, observation=observation)
        review_dir = self.evidence_root / str(event.id) / "agent-review"
        request_path = review_dir / "request.json"
        final_path = review_dir / "final.json"

        _atomic_write_json(request_path, request)

        if final_path.is_file():
            stored = json.loads(final_path.read_text(encoding="utf-8"))
            stored.pop("idempotency_key", None)
            return AgentProposal.model_validate(stored)

        proposal = await self.reviewer.review(request)
        proposal_dict = proposal.model_dump(mode="json")

        _atomic_write_json(review_dir / "attempt-1-result.json", proposal_dict)
        final_payload = {
            **proposal_dict,
            "idempotency_key": idempotency_key(
                event_id=str(event.id),
                evidence_sha256=str(observation.evidence_sha256),
                control_price_version_id=str(comparison.control_price_version_id),
            ),
        }
        _atomic_write_json(final_path, final_payload)

        try:
            event.review_attempts = int(getattr(event, "review_attempts", 0)) + 1
        except Exception:  # pragma: no cover - ORM models may reject direct setattr
            logger.debug("could not increment review_attempts on event %s", event.id)

        return proposal
