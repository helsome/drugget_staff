"""Stage 3A Codex SDK reviewer and browser-evidence providers.

The module deliberately keeps Codex on the advisory side of the review gate.
It returns an ``AgentProposal`` for the deterministic validator; it never
writes observations, control rules, comparisons, or candidate formal status.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from .agent_review import AGENT_SCHEMA_VERSION, PROMPT_VERSION, AgentProposal


@dataclass(frozen=True)
class BrowserCapabilities:
    browser_available: bool = False
    computer_use_available: bool = False
    screenshot_available: bool = False
    network_data_available: bool = False
    authenticated_session_available: bool = False


@dataclass(frozen=True)
class BrowserRequest:
    url: str | None
    evidence: dict[str, Any]
    target_sku_id: str | None = None


class BrowserToolProvider(Protocol):
    async def capability_probe(self) -> BrowserCapabilities: ...
    async def inspect_page(self, request: BrowserRequest) -> dict[str, Any]: ...
    async def list_skus(self, request: BrowserRequest) -> list[dict[str, Any]]: ...
    async def select_sku(self, request: BrowserRequest) -> dict[str, Any]: ...
    async def capture_state(self, request: BrowserRequest) -> dict[str, Any]: ...


class FakeBrowserProvider:
    """Deterministic test provider; callers can supply explicit fixture replies."""

    def __init__(self, *, capabilities: BrowserCapabilities | None = None,
                 page: dict[str, Any] | None = None, skus: list[dict[str, Any]] | None = None) -> None:
        self.capabilities = capabilities or BrowserCapabilities(browser_available=True)
        self.page = page or {}
        self.skus = skus or []

    async def capability_probe(self) -> BrowserCapabilities:
        return self.capabilities

    async def inspect_page(self, request: BrowserRequest) -> dict[str, Any]:
        del request
        return dict(self.page)

    async def list_skus(self, request: BrowserRequest) -> list[dict[str, Any]]:
        del request
        return list(self.skus)

    async def select_sku(self, request: BrowserRequest) -> dict[str, Any]:
        return {"selected_sku_id": request.target_sku_id, "selected": True}

    async def capture_state(self, request: BrowserRequest) -> dict[str, Any]:
        return {"url": request.url, "page": dict(self.page), "sku_options": list(self.skus)}


class ReplayBrowserProvider(FakeBrowserProvider):
    """Reads Stage-2 raw-evidence replay fixtures without network/browser use."""

    def __init__(self, replay: dict[str, Any]) -> None:
        super().__init__(
            capabilities=BrowserCapabilities(browser_available=True, network_data_available=True),
            page=dict(replay.get("page_context") or replay.get("product") or {}),
            skus=list(replay.get("sku_options") or []),
        )
        self.replay = replay

    async def inspect_page(self, request: BrowserRequest) -> dict[str, Any]:
        del request
        return dict(self.replay)

    async def capture_state(self, request: BrowserRequest) -> dict[str, Any]:
        state = await super().capture_state(request)
        state["replay"] = True
        return state


class CodexBrowserProvider:
    """Adapter for a separately authorized browser bridge.

    Codex SDK does not imply an authenticated Chrome session.  An application
    must pass explicit bridge callables; otherwise the provider reports no
    capability and the reviewer produces a human-review proposal.
    """

    def __init__(self, *, capabilities: BrowserCapabilities | None = None,
                 inspect: Callable[[BrowserRequest], Awaitable[dict[str, Any]]] | None = None,
                 list_skus: Callable[[BrowserRequest], Awaitable[list[dict[str, Any]]]] | None = None,
                 select: Callable[[BrowserRequest], Awaitable[dict[str, Any]]] | None = None,
                 capture: Callable[[BrowserRequest], Awaitable[dict[str, Any]]] | None = None) -> None:
        self._capabilities = capabilities or BrowserCapabilities()
        self._inspect, self._list, self._select, self._capture = inspect, list_skus, select, capture

    async def capability_probe(self) -> BrowserCapabilities:
        return self._capabilities

    async def inspect_page(self, request: BrowserRequest) -> dict[str, Any]:
        return await self._require(self._inspect, request, "inspect_page")

    async def list_skus(self, request: BrowserRequest) -> list[dict[str, Any]]:
        return await self._require(self._list, request, "list_skus")

    async def select_sku(self, request: BrowserRequest) -> dict[str, Any]:
        return await self._require(self._select, request, "select_sku")

    async def capture_state(self, request: BrowserRequest) -> dict[str, Any]:
        return await self._require(self._capture, request, "capture_state")

    @staticmethod
    async def _require(call: Any, request: BrowserRequest, operation: str) -> Any:
        if call is None:
            raise RuntimeError(f"browser bridge unavailable for {operation}")
        return await call(request)


@dataclass(frozen=True)
class CodexReviewConfig:
    model: str = "gpt-5.6-sol"
    prompt_version: str = PROMPT_VERSION
    schema_version: str = AGENT_SCHEMA_VERSION
    reasoning_effort: str = "low"


class CodexGateway:
    """Thin, injectable wrapper around the official ``openai-codex`` AsyncCodex SDK."""

    def __init__(self, config: CodexReviewConfig, *, client_factory: Callable[[], Any] | None = None) -> None:
        self.config = config
        self._client_factory = client_factory

    async def run(self, *, prompt: str, output_schema: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if self._client_factory is None:
            try:
                from openai_codex import ApprovalMode, AsyncCodex, Sandbox  # type: ignore[import-not-found]
            except ImportError as exc:  # configuration is deliberately fail-closed
                raise RuntimeError("openai-codex SDK is not installed") from exc
            client: Any = AsyncCodex()
            approval_mode: Any = ApprovalMode.never
            sandbox: Any = Sandbox.read_only
        else:
            client = self._client_factory()
            approval_mode = "never"
            sandbox = "read_only"

        started = datetime.now(timezone.utc)
        async with client:
            thread = await client.thread_start(
                approval_mode=approval_mode, sandbox=sandbox, model=self.config.model, ephemeral=True,
            )
            result = await thread.run(
                prompt, model=self.config.model, effort=self.config.reasoning_effort,
                output_schema=output_schema,
            )
        completed = datetime.now(timezone.utc)
        final_response = getattr(result, "final_response", None) or getattr(result, "text", None)
        if not isinstance(final_response, str):
            raise RuntimeError("Codex SDK returned no final response")
        metadata = {
            "model": self.config.model,
            "request_id": getattr(result, "id", None) or getattr(result, "turn_id", None),
            "prompt_version": self.config.prompt_version,
            "schema_version": self.config.schema_version,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "token_usage": getattr(result, "token_usage", None) or getattr(result, "usage", None),
            "tool_calls": getattr(result, "tool_calls", None) or [],
            "error_code": None,
        }
        return final_response, metadata


class CodexSDKReviewer:
    """Real Codex SDK reviewer used only in ``codex_shadow`` during Stage 3A."""

    def __init__(self, gateway: CodexGateway, browser: BrowserToolProvider) -> None:
        self.gateway, self.browser = gateway, browser
        self.review_metadata: dict[str, Any] = {}

    async def review(self, request: dict[str, Any]) -> AgentProposal:
        observation = request["observation"]
        browser_request = BrowserRequest(
            url=observation.get("final_url"), evidence=observation.get("raw_evidence") or {},
            target_sku_id=observation.get("selected_sku_id"),
        )
        capabilities = await self.browser.capability_probe()
        self.review_metadata = {"browser_capabilities": asdict(capabilities)}
        if not capabilities.browser_available:
            return self._human_review("browser capability unavailable")

        page = await self.browser.inspect_page(browser_request)
        skus = await self.browser.list_skus(browser_request)
        state = await self.browser.capture_state(browser_request)
        evidence = {"capabilities": asdict(capabilities), "page": page, "sku_options": skus, "captured_state": state}
        prompt = (
            "You are an evidence-only pharmaceutical price reviewer. Return only JSON matching the supplied schema. "
            "Do not modify any business fact, guidance price, comparison verdict, or formal price status. "
            "If browser evidence is incomplete, select human_review.\nREQUEST:\n"
            + json.dumps({"review": request, "browser_evidence": evidence}, ensure_ascii=False, default=str)
        )
        try:
            raw, metadata = await self.gateway.run(prompt=prompt, output_schema=AgentProposal.model_json_schema())
            self.review_metadata.update(metadata)
            self.review_metadata["browser_evidence"] = evidence
            return AgentProposal.model_validate(json.loads(raw))
        except Exception as exc:
            self.review_metadata["error_code"] = type(exc).__name__
            self.review_metadata["error_detail"] = str(exc)[:1000]
            return self._human_review(f"Codex shadow review unavailable: {type(exc).__name__}")

    @staticmethod
    def _human_review(reason: str) -> AgentProposal:
        return AgentProposal(
            decision="human_review", recommended_action="human_review", confidence=0.0,
            product_match=False, manufacturer_match=False, sku_match=False,
            price_verified=False, reasons=[reason], evidence_pointers=[],
            unresolved_questions=[reason],
        )
