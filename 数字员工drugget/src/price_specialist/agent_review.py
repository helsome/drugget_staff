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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

try:  # POSIX deployment target (macOS/Linux); Windows remains single-process safe.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger(__name__)

AGENT_SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "1.0"


class AgentProposal(BaseModel):
    """Spec §7.2 fixed JSON schema for an agent review decision."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "recapture", "human_review", "reject"]
    product_match: bool
    manufacturer_match: bool
    sku_match: bool
    target_sku_id: str | None = None
    price_verified: bool
    price_type: Literal["base_price", "promotion_price", "member_price", "coupon_price", "tier_price", "range_price", "package_total_price", "unknown"] | None = None
    normalized_price: str | None = None
    min_purchase_quantity: int | None = None
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    evidence_pointers: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_action: Literal["accept", "recapture", "human_review", "reject"]
    control_price_version_id: str | None = None
    evidence_sha256: str | None = None


class AgentReviewer(Protocol):
    async def review(self, request: dict) -> AgentProposal: ...


class FakeAgentReviewer:
    """Stage-1 stub: echoes the observation's deterministic price as an accept."""

    async def review(self, request: dict) -> AgentProposal:
        obs = request["observation"]
        raw = obs.get("raw_evidence") or {}
        selected = raw.get("selected_sku") or {}
        structured = bool(raw.get("sku_options") or raw.get("price_quotes"))
        obs_price = obs["single_unit_price"]
        return AgentProposal(
            decision="accept",
            product_match=True,
            manufacturer_match=True,
            sku_match=True,
            target_sku_id=obs.get("selected_sku_id") or selected.get("sku_id"),
            price_verified=True,
            price_type=obs.get("price_type", "base_price"),
            normalized_price=str(obs_price),
            min_purchase_quantity=obs.get("min_purchase_quantity", 1),
            confidence=0.99,
            reasons=["fake-agent: echoed page unit price"],
            evidence_pointers=(
                ["single_unit_price", "price_quotes[0].amount", "product.title", "product.manufacturer"]
                if structured else ["single_unit_price"]
            ),
            unresolved_questions=[],
            recommended_action="accept",
            control_price_version_id=str(request.get("comparison", {}).get("control_price_version_id") or ""),
            evidence_sha256=str(obs.get("evidence_sha256") or ""),
        )


def idempotency_key(
    *,
    event_id: str,
    evidence_sha256: str,
    control_price_version_id: str,
    model: str = "fake",
) -> str:
    """Stable key including evidence, rule version, schema, prompt, and model."""
    raw = f"{event_id}|{evidence_sha256}|{control_price_version_id}|{PROMPT_VERSION}|{AGENT_SCHEMA_VERSION}|{model}"
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


@contextmanager
def _review_lock(path: Path):
    """Advisory cross-process lease for an event's artifact directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        review_dir = self.evidence_root / str(event.id) / "agent-review"
        # Keep final.json read/write and the external reviewer call in one
        # advisory lease so a second worker cannot duplicate the same review.
        with _review_lock(review_dir / ".dispatch.lock"):
            return await self._dispatch_unlocked(session, event=event, comparison=comparison, observation=observation)

    async def _dispatch_unlocked(
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
        model = str(getattr(getattr(self.reviewer, "gateway", None), "config", None).model) if getattr(getattr(self.reviewer, "gateway", None), "config", None) else "fake"
        key = idempotency_key(
            event_id=str(event.id), evidence_sha256=str(observation.evidence_sha256),
            control_price_version_id=str(comparison.control_price_version_id), model=model,
        )
        request["idempotency_key"] = key
        review_dir = self.evidence_root / str(event.id) / "agent-review"
        request_path = review_dir / "request.json"
        final_path = review_dir / "final.json"

        _atomic_write_json(request_path, request)

        if final_path.is_file():
            stored = json.loads(final_path.read_text(encoding="utf-8"))
            if stored.pop("idempotency_key", None) == key:
                stored.pop("review_metadata", None)
                return AgentProposal.model_validate(stored)

        attempt = int(getattr(event, "review_attempts", 0) or 0) + 1
        _atomic_write_json(review_dir / f"attempt-{attempt}-request.json", request)
        proposal = await self.reviewer.review(request)
        proposal_dict = proposal.model_dump(mode="json")

        _atomic_write_json(review_dir / f"attempt-{attempt}-result.json", proposal_dict)
        final_payload = {
            **proposal_dict,
            "idempotency_key": key,
            "review_metadata": getattr(self.reviewer, "review_metadata", None),
        }
        _atomic_write_json(final_path, final_payload)

        try:
            event.review_attempts = attempt
        except Exception:  # pragma: no cover - ORM models may reject direct setattr
            logger.debug("could not increment review_attempts on event %s", event.id)

        return proposal

    def write_validation(self, *, event: Any, outcome: Any) -> None:
        """Persist the deterministic validation result beside its proposal."""
        attempt = int(getattr(event, "review_attempts", 1) or 1)
        _atomic_write_json(
            self.evidence_root / str(event.id) / "agent-review" / f"attempt-{attempt}-validation.json",
            {"decision": outcome.decision, "passed": outcome.passed, "reasons": outcome.reasons,
             "recomputed_price": str(outcome.recomputed_price) if outcome.recomputed_price is not None else None},
        )
