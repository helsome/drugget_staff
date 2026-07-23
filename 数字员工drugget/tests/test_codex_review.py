"""Stage 3A isolated Codex shadow-runtime tests; no network or SDK login."""
from __future__ import annotations

import json

import pytest

from price_specialist.codex_review import (
    BrowserCapabilities, BrowserRequest, CodexGateway, CodexReviewConfig,
    CodexSDKReviewer, FakeBrowserProvider, ReplayBrowserProvider,
)


class _Result:
    id = "turn-1"
    token_usage = {"input_tokens": 7, "output_tokens": 11}
    tool_calls = []
    final_response = json.dumps({
        "decision": "human_review", "recommended_action": "human_review", "confidence": 0.4,
        "product_match": True, "manufacturer_match": True, "sku_match": True,
        "price_verified": False, "reasons": ["needs human"],
        "evidence_pointers": ["single_unit_price"], "unresolved_questions": ["confirm SKU"],
    })


class _Thread:
    async def run(self, prompt, **kwargs):
        assert "browser_evidence" in prompt
        assert kwargs["output_schema"]["additionalProperties"] is False
        return _Result()


class _Client:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def thread_start(self, **kwargs):
        assert kwargs["model"] == "gpt-5.6-sol"
        return _Thread()


@pytest.mark.asyncio
async def test_codex_shadow_reviewer_calls_sdk_gateway_and_records_trace() -> None:
    gateway = CodexGateway(CodexReviewConfig(), client_factory=_Client)
    browser = FakeBrowserProvider(page={"price": "16.17"}, skus=[{"sku_id": "sku-1"}])
    reviewer = CodexSDKReviewer(gateway, browser)
    proposal = await reviewer.review({"observation": {"final_url": "https://example.test", "raw_evidence": {}, "selected_sku_id": "sku-1"}})
    assert proposal.decision == "human_review"
    assert reviewer.review_metadata["model"] == "gpt-5.6-sol"
    assert reviewer.review_metadata["browser_capabilities"]["browser_available"] is True


@pytest.mark.asyncio
async def test_no_browser_capability_routes_to_human_review_without_sdk_call() -> None:
    reviewer = CodexSDKReviewer(
        CodexGateway(CodexReviewConfig(), client_factory=lambda: (_ for _ in ()).throw(AssertionError("SDK should not run"))),
        FakeBrowserProvider(capabilities=BrowserCapabilities()),
    )
    proposal = await reviewer.review({"observation": {"raw_evidence": {}}})
    assert proposal.decision == proposal.recommended_action == "human_review"
    assert proposal.confidence == 0


@pytest.mark.asyncio
async def test_replay_provider_exposes_skus_and_evidence() -> None:
    provider = ReplayBrowserProvider({"sku_options": [{"sku_id": "a"}], "page_context": {"title": "fixture"}})
    request = BrowserRequest(url=None, evidence={})
    assert (await provider.capability_probe()).network_data_available is True
    assert (await provider.list_skus(request))[0]["sku_id"] == "a"
    assert (await provider.inspect_page(request))["page_context"]["title"] == "fixture"
