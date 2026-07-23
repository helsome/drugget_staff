"""Single composition root for the price-review gate.

Runtime entry points must create review dependencies here instead of assembling
them independently.  The factory deliberately returns a *working gate* for
every mode: an unavailable or disabled reviewer is represented by a reviewer
that fails closed, never by ``None``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from .agent_review import AgentReviewService, AgentReviewer, FakeAgentReviewer
from .agent_validator import AgentProposalValidator
from .alerts import AlertDryRunService
from .codex_review import BrowserToolProvider, CodexBrowserProvider, CodexGateway, CodexReviewConfig, CodexSDKReviewer
from .decisions import PriceDecisionService
from .review_orchestrator import ReviewOrchestrator
from .review_policy import ReviewPolicy


ReviewMode = Literal["fake", "codex_shadow", "codex_active", "disabled"]
RuntimeMode = Literal["test", "replay", "production"]
_VALID_REVIEW_MODES = frozenset({"fake", "codex_shadow", "codex_active", "disabled"})


class ReviewerUnavailableError(RuntimeError):
    """Raised inside the review gate when no real Codex reviewer is installed."""


class _UnavailableReviewer:
    def __init__(self, *, mode: str, reason: str) -> None:
        self.mode = mode
        self.reason = reason

    async def review(self, request: dict) -> Any:
        del request
        raise ReviewerUnavailableError(
            f"review mode {self.mode!r} cannot review: {self.reason}"
        )


def resolve_review_mode(*, runtime_mode: RuntimeMode, review_mode: str | None = None) -> ReviewMode:
    """Resolve and validate the requested mode without silently enabling fake.

    Fake is an explicit test/replay convenience only.  Production defaults to
    ``codex_shadow`` until Stage 3A provides the SDK reviewer; that reviewer is
    currently unavailable and therefore fails closed for any mandatory review.
    """
    requested = review_mode or os.environ.get("PRICE_SPECIALIST_REVIEW_MODE")
    if requested is None:
        requested = "fake" if runtime_mode in {"test", "replay"} else "codex_shadow"
    if requested not in _VALID_REVIEW_MODES:
        raise ValueError(
            "PRICE_SPECIALIST_REVIEW_MODE must be one of "
            "fake, codex_shadow, codex_active, disabled"
        )
    if runtime_mode == "production" and requested in {"fake", "disabled"}:
        raise ValueError(f"production runtime forbids review_mode={requested!r}")
    return requested  # type: ignore[return-value]


def build_review_orchestrator(
    *,
    session: Any,
    settings: Any,
    run_id: str | None,
    event_sink: Any | None,
    review_mode: str | None = None,
    runtime_mode: RuntimeMode = "production",
    reviewer: AgentReviewer | None = None,
    browser_provider: BrowserToolProvider | None = None,
) -> ReviewOrchestrator:
    """Build the only supported runtime review composition.

    ``reviewer`` is intentionally a test seam.  Runtime callers select a
    mode; they must not instantiate ``FakeAgentReviewer`` themselves.
    """
    mode = resolve_review_mode(runtime_mode=runtime_mode, review_mode=review_mode)
    if reviewer is not None:
        if runtime_mode == "production" and isinstance(reviewer, FakeAgentReviewer):
            raise ValueError("production runtime forbids FakeAgentReviewer")
        selected_reviewer: AgentReviewer = reviewer
    elif mode == "fake":
        selected_reviewer = FakeAgentReviewer()
    elif mode == "disabled":
        selected_reviewer = _UnavailableReviewer(
            mode=mode,
            reason="disabled is reserved for explicit offline compatibility tests",
        )
    else:
        # Stage 3A uses the official AsyncCodex SDK. A browser bridge remains
        # explicitly injected: SDK availability does not grant browser or an
        # authenticated session. Missing capability returns human_review.
        config = CodexReviewConfig(model=os.environ.get("PRICE_SPECIALIST_CODEX_MODEL", "gpt-5.6-sol"))
        selected_reviewer = CodexSDKReviewer(
            CodexGateway(config), browser_provider or CodexBrowserProvider(),
        )

    orchestrator = ReviewOrchestrator(
        session=session,
        decision_service=PriceDecisionService(session),
        review_policy=ReviewPolicy(),
        review_service=AgentReviewService(selected_reviewer, evidence_root=Path(settings.evidence_dir)),
        validator=AgentProposalValidator(),
        alert_service=AlertDryRunService(),
        evidence_root=Path(settings.evidence_dir),
        emit=event_sink.emit if event_sink is not None else None,
        run_id=run_id,
    )
    # Stage 3A is shadow-only. BatchOrchestrator remains the only state-machine
    # writer, and no Codex mode may release a candidate at this stage.
    orchestrator.review_mode = mode
    orchestrator.formal_release_enabled = mode == "fake"
    return orchestrator
