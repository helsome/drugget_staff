"""ReviewPolicy - the single source of truth for mandatory agent-review triggers.

Spec §3.2: any standardized comparison price below the valid guidance price must
enter review, even by 0.01. This hard rule is uncloseable.
Spec §3.3: additional triggers (SKU ambiguity, page change, evidence incomplete, ...)
are added in Stage 2; they all live here, never in adapters, the GUI, or the
orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .decisions import BELOW_CONTROL

BELOW_CONTROL_REASON = "below_control"

SKU_AMBIGUOUS_REASON = "sku_ambiguous"
PRICE_TYPE_AMBIGUOUS_REASON = "price_type_ambiguous"
PACKAGE_CONVERSION_FAILED_REASON = "package_conversion_failed"
PAGE_CHANGED_REASON = "page_changed"
EVIDENCE_INCOMPLETE_REASON = "evidence_incomplete"
MANUFACTURER_MISMATCH_REASON = "manufacturer_mismatch"
PRICE_RANGE_DETECTED_REASON = "price_range_detected"
TIER_PRICE_DETECTED_REASON = "tier_price_detected"
SELECTED_SKU_UNVERIFIED_REASON = "selected_sku_unverified"


@dataclass(frozen=True)
class ReviewTrigger:
    """A single reason a price observation must enter agent review."""

    reason: str
    detail: str


class ReviewPolicy:
    """Evaluate whether a price comparison must be routed to agent review.

    The policy reads the deterministic ``PriceComparison`` plus explicit page
    evidence from ``PriceObservation``.  Absence of a newer evidence key is
    never treated as a failure: legacy fixtures remain deterministic and only
    an explicit flag or declared structured evidence can trigger Stage-2
    review.  The below-control hard rule is evaluated first and cannot be
    disabled by any evidence state.
    """

    def requires_review(self, comparison: Any, observation: Any = None) -> ReviewTrigger | None:
        if getattr(comparison, "verdict", None) == BELOW_CONTROL:
            difference = getattr(comparison, "difference", None)
            detail = f"标准化采集价低于有效指导价 {difference}"
            return ReviewTrigger(reason=BELOW_CONTROL_REASON, detail=detail)

        raw = getattr(observation, "raw_evidence", None)
        raw = raw if isinstance(raw, dict) else {}

        for reason, detail, predicate in _EVIDENCE_TRIGGERS:
            if predicate(observation, raw):
                return ReviewTrigger(reason=reason, detail=detail)
        return None


def _explicit_true(raw: dict[str, Any], *keys: str) -> bool:
    """Accept only a literal true flag; missing/false legacy data is neutral."""
    return any(raw.get(key) is True for key in keys)


def _structured_price_types(raw: dict[str, Any]) -> set[str]:
    quotes = raw.get("price_quotes")
    if not isinstance(quotes, list):
        return set()
    return {
        quote.get("price_type")
        for quote in quotes
        if isinstance(quote, dict) and isinstance(quote.get("price_type"), str)
    }


def _sku_ambiguous(_observation: Any, raw: dict[str, Any]) -> bool:
    if _explicit_true(raw, SKU_AMBIGUOUS_REASON):
        return True
    options = raw.get("sku_options")
    if not isinstance(options, list):
        return False
    available = [option for option in options if isinstance(option, dict) and option.get("available") is True]
    selected = [option for option in available if option.get("selected") is True]
    # A declared multi-SKU page without exactly one selected, available SKU is
    # ambiguous.  Empty/unstructured old evidence never enters this branch.
    return len(available) > 1 and len(selected) != 1


def _price_type_ambiguous(_observation: Any, raw: dict[str, Any]) -> bool:
    return _explicit_true(raw, PRICE_TYPE_AMBIGUOUS_REASON) or raw.get("price_type") == "unknown"


def _package_conversion_failed(_observation: Any, raw: dict[str, Any]) -> bool:
    return _explicit_true(raw, PACKAGE_CONVERSION_FAILED_REASON) or raw.get("package_conversion_status") == "failed"


def _page_changed(observation: Any, raw: dict[str, Any]) -> bool:
    return (
        _explicit_true(raw, PAGE_CHANGED_REASON)
        or getattr(observation, "collection_status", None) == "page_changed"
        or getattr(observation, "error_code", None) == "page_changed"
    )


def _evidence_incomplete(_observation: Any, raw: dict[str, Any]) -> bool:
    return _explicit_true(raw, EVIDENCE_INCOMPLETE_REASON) or raw.get("evidence_complete") is False


def _manufacturer_mismatch(_observation: Any, raw: dict[str, Any]) -> bool:
    return _explicit_true(raw, MANUFACTURER_MISMATCH_REASON) or raw.get("manufacturer_match") is False


def _price_range_detected(_observation: Any, raw: dict[str, Any]) -> bool:
    return _explicit_true(raw, PRICE_RANGE_DETECTED_REASON) or raw.get("price_type") == "range_price" or "range_price" in _structured_price_types(raw)


def _tier_price_detected(_observation: Any, raw: dict[str, Any]) -> bool:
    return _explicit_true(raw, TIER_PRICE_DETECTED_REASON) or raw.get("price_type") == "tier_price" or "tier_price" in _structured_price_types(raw)


def _selected_sku_unverified(_observation: Any, raw: dict[str, Any]) -> bool:
    if _explicit_true(raw, SELECTED_SKU_UNVERIFIED_REASON):
        return True
    selected_sku = raw.get("selected_sku")
    return isinstance(selected_sku, dict) and (
        selected_sku.get("verified") is False or selected_sku.get("available") is False
    )


_EVIDENCE_TRIGGERS: tuple[tuple[str, str, Callable[[Any, dict[str, Any]], bool]], ...] = (
    (SKU_AMBIGUOUS_REASON, "页面存在未消歧的 SKU 选择", _sku_ambiguous),
    (PRICE_TYPE_AMBIGUOUS_REASON, "页面价格类型未明确", _price_type_ambiguous),
    (PACKAGE_CONVERSION_FAILED_REASON, "包装换算失败", _package_conversion_failed),
    (PAGE_CHANGED_REASON, "页面结构已变化", _page_changed),
    (EVIDENCE_INCOMPLETE_REASON, "页面证据不完整", _evidence_incomplete),
    (MANUFACTURER_MISMATCH_REASON, "页面生产厂家与目标不一致", _manufacturer_mismatch),
    (PRICE_RANGE_DETECTED_REASON, "页面显示价格区间", _price_range_detected),
    (TIER_PRICE_DETECTED_REASON, "页面显示阶梯价", _tier_price_detected),
    (SELECTED_SKU_UNVERIFIED_REASON, "所选 SKU 未经验证", _selected_sku_unverified),
)
