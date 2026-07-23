"""Canonical SKU and price evidence for live collection and offline replay."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .catalog import normalize_spec
from .schemas import SKUEvidence


PRICE_TYPES = frozenset({
    "base_price", "promotion_price", "member_price", "coupon_price",
    "tier_price", "range_price", "package_total_price", "unknown",
})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "是", "需要"}:
            return True
        if lowered in {"false", "no", "0", "否", "不需要"}:
            return False
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    parsed = int(match.group())
    return parsed if parsed > 0 else None


def _quote_package_count(item: dict[str, Any], raw_fields: dict[str, Any]) -> int | None:
    """Read an explicit page package count without treating MOQ as package size."""
    return _positive_int(
        item.get("price_box_count")
        or item.get("package_box_count")
        or item.get("sale_box_count")
        or raw_fields.get("price_box_count")
        or raw_fields.get("package_box_count")
        or raw_fields.get("sale_box_count")
    )


def _quote_units_per_box(item: dict[str, Any], raw_fields: dict[str, Any]) -> int | None:
    return _positive_int(item.get("units_per_box") or raw_fields.get("units_per_box"))


def _amount(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return ""
    try:
        return format(Decimal(match.group()), "f")
    except InvalidOperation:
        return ""


def _price_type(value: Any, *, raw_text: str, base_field_present: bool) -> str:
    explicit = _text(value)
    if explicit in PRICE_TYPES:
        return explicit
    text = raw_text.lower()
    if "会员" in text or "member" in text:
        return "member_price"
    if "优惠券" in text or "coupon" in text:
        return "coupon_price"
    if "阶梯" in text or "起批" in text or "tier" in text:
        return "tier_price"
    if "促销" in text or "活动价" in text or "promotion" in text:
        return "promotion_price"
    if re.search(r"\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?", text):
        return "range_price"
    if "盒装" in text or "多盒" in text or "套装" in text:
        return "package_total_price"
    return "base_price" if base_field_present else "unknown"


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_sku_evidence(
    raw_fields: dict[str, Any] | None,
    *,
    product_id: str | None = None,
    title: str | None = None,
    manufacturer: str | None = None,
    provider_id: str | None = None,
    selected_spec: str | None = None,
    selected_sku_id: str | None = None,
    page_price_raw: str | None = None,
    page_price_value: Any = None,
    min_purchase_quantity: Any = None,
    final_url: str | None = None,
    page_shop: str | None = None,
    platform: str | None = None,
    parser_name: str = "",
    parser_version: str = "",
    source: str = "live",
) -> dict[str, Any]:
    """Return canonical evidence while retaining all legacy adapter fields.

    Values are copied only from explicit page/adapter inputs. Missing SKU IDs,
    manufacturers, quantities, and price conditions remain empty/unknown and
    make ``evidence_complete`` false.
    """
    legacy = dict(raw_fields or {})
    existing_product = legacy.get("product") if isinstance(legacy.get("product"), dict) else {}
    product = {
        **existing_product,
        "product_id": _text(existing_product.get("product_id") or product_id),
        "title": _text(existing_product.get("title") or title),
        "manufacturer": _text(existing_product.get("manufacturer") or manufacturer),
        "provider_id": _text(existing_product.get("provider_id") or provider_id),
    }

    option_source = (
        legacy.get("sku_options")
        or legacy.get("规格选项")
        or legacy.get("可选规格")
        or []
    )
    normalized_selected_spec = normalize_spec(selected_spec)
    options: list[dict[str, Any]] = []
    for item in _list(option_source):
        if not isinstance(item, dict):
            continue
        raw_spec = _text(item.get("raw_spec") or item.get("spec") or item.get("规格"))
        normalized = _text(item.get("normalized_spec")) or normalize_spec(raw_spec) or ""
        sku_id = _text(item.get("sku_id") or item.get("id"))
        explicit_selected = _bool_or_none(item.get("selected"))
        selected = (
            explicit_selected
            if explicit_selected is not None
            else bool(normalized_selected_spec and normalized == normalized_selected_spec)
        )
        options.append({
            **item,
            "sku_id": sku_id,
            "raw_spec": raw_spec,
            "normalized_spec": normalized,
            "selected": selected,
            "available": _bool_or_none(item.get("available")),
        })
    if not options and selected_spec:
        options.append({
            "sku_id": _text(selected_sku_id),
            "raw_spec": _text(selected_spec),
            "normalized_spec": normalized_selected_spec or "",
            "selected": True,
            "available": None,
        })

    existing_selected = legacy.get("selected_sku") if isinstance(legacy.get("selected_sku"), dict) else {}
    selected_option = next((item for item in options if item.get("selected") is True), None)
    selected = dict(existing_selected)
    if selected_option is not None:
        # Page selection is authoritative.  Preserve diagnostic extras from a
        # legacy selected_sku object, but never allow it to contradict the
        # actual selected option and accidentally certify the wrong SKU.
        selected_extras = {
            key: value for key, value in selected.items()
            if key not in {"sku_id", "raw_spec", "normalized_spec", "selected", "available", "verified"}
        }
        selected = {
            **selected_option,
            **selected_extras,
            "sku_id": _text(selected_option.get("sku_id")),
            "raw_spec": _text(selected_option.get("raw_spec")),
            "normalized_spec": _text(selected_option.get("normalized_spec")),
        }
    selected.setdefault("sku_id", _text(selected_sku_id))
    selected.setdefault("raw_spec", _text(selected_spec))
    selected.setdefault("normalized_spec", normalized_selected_spec or "")
    selected["verified"] = bool(
        selected.get("sku_id")
        and selected.get("normalized_spec")
        and sum(1 for item in options if item.get("selected") is True) == 1
    )

    quote_source = legacy.get("price_quotes") or legacy.get("报价") or []
    quotes: list[dict[str, Any]] = []
    for index, item in enumerate(_list(quote_source)):
        if not isinstance(item, dict):
            continue
        raw_text = _text(item.get("raw_text") or item.get("text") or item.get("价格原文"))
        amount = _amount(item.get("amount") or item.get("price"))
        quote_type = _price_type(
            item.get("price_type"),
            raw_text=raw_text,
            base_field_present=bool(amount and not raw_text),
        )
        membership = _bool_or_none(item.get("membership_required"))
        promotion = _bool_or_none(item.get("promotion_required"))
        if quote_type == "member_price":
            membership = True
        if quote_type in {"promotion_price", "coupon_price"}:
            promotion = True
        quotes.append({
            **item,
            "sku_id": _text(item.get("sku_id") or selected.get("sku_id")),
            "price_type": quote_type,
            "amount": amount,
            "min_quantity": _positive_int(
                item.get("min_quantity") or item.get("minimum_purchase")
            ),
            "membership_required": membership,
            "promotion_required": promotion,
            "price_box_count": _quote_package_count(item, legacy),
            "units_per_box": _quote_units_per_box(item, legacy),
            "raw_text": raw_text,
            "evidence_pointer": _text(
                item.get("evidence_pointer") or f"price_quotes[{index}].amount"
            ),
        })
    if not quotes and (page_price_raw is not None or page_price_value is not None):
        raw_text = _text(page_price_raw)
        base_present = any(key in legacy for key in ("价格", "price", "page_price"))
        quote_type = _price_type(
            legacy.get("price_type"),
            raw_text=raw_text,
            base_field_present=base_present,
        )
        quotes.append({
            "sku_id": _text(selected.get("sku_id")),
            "price_type": quote_type,
            "amount": _amount(page_price_value if page_price_value is not None else page_price_raw),
            "min_quantity": _positive_int(min_purchase_quantity),
            "membership_required": False if quote_type == "base_price" else None,
            "promotion_required": False if quote_type == "base_price" else None,
            "price_box_count": _positive_int(legacy.get("price_box_count") or legacy.get("sale_box_count")),
            "units_per_box": _positive_int(legacy.get("units_per_box")),
            "raw_text": raw_text,
            "evidence_pointer": "price_quotes[0].amount",
        })

    existing_context = legacy.get("page_context") if isinstance(legacy.get("page_context"), dict) else {}
    page_context = {
        **existing_context,
        "final_url": _text(existing_context.get("final_url") or final_url),
        "page_shop": _text(existing_context.get("page_shop") or page_shop),
        "platform": _text(existing_context.get("platform") or platform),
        "source": _text(existing_context.get("source") or source),
    }
    existing_parser = legacy.get("parser") if isinstance(legacy.get("parser"), dict) else {}
    parser = {
        **existing_parser,
        "name": _text(existing_parser.get("name") or parser_name),
        "version": _text(existing_parser.get("version") or parser_version),
    }

    selected_count = sum(1 for item in options if item.get("selected") is True)
    complete = bool(
        product["product_id"]
        and product["title"]
        and options
        and selected_count == 1
        and selected.get("verified") is True
        and quotes
        and any(quote.get("sku_id") == selected.get("sku_id") for quote in quotes)
        and all(
            quote.get("sku_id")
            and quote.get("amount")
            and quote.get("price_type") != "unknown"
            and quote.get("min_quantity") is not None
            and quote.get("evidence_pointer")
            for quote in quotes
        )
    )
    canonical = {
        **legacy,
        "product": product,
        "sku_options": options,
        "price_quotes": quotes,
        "selected_sku": selected,
        "page_context": page_context,
        "parser": parser,
        "price_type": quotes[0]["price_type"] if len(quotes) == 1 else "unknown",
        "min_purchase_quantity": quotes[0]["min_quantity"] if len(quotes) == 1 else None,
        "evidence_complete": complete,
    }
    # Validate the canonical contract while preserving compatible extra keys.
    return SKUEvidence.model_validate(canonical).model_dump(mode="json")


def quote_single_unit_price(quote: dict[str, Any]) -> Decimal | None:
    """Deterministically convert one page quote to a minimum-unit price.

    Both package dimensions must be page evidence.  This intentionally does
    not use MOQ as a divisor and never falls back to a control price.
    """
    amount = _amount(quote.get("amount"))
    boxes = _positive_int(quote.get("price_box_count"))
    units = _positive_int(quote.get("units_per_box"))
    if not amount or boxes is None or units is None:
        return None
    try:
        return Decimal(amount) / Decimal(boxes) / Decimal(units)
    except (InvalidOperation, ZeroDivisionError):
        return None


def load_replay_evidence(path: Path) -> dict[str, Any]:
    """Load a replay fixture and enforce its non-production provenance."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("replay fixture must be a JSON object")
    context = payload.get("page_context")
    if not isinstance(context, dict) or context.get("source") != "replay":
        raise ValueError("replay fixture must declare page_context.source='replay'")
    return normalize_sku_evidence(payload, source="replay")
