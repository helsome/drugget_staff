from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .catalog import BRAND_TO_GENERIC
from .schemas import SearchHit
from .search import SearchClassifier


def classify_existing_search(
    *,
    result_path: Path,
    target_path: Path,
    store_matching_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    results = json.loads(result_path.read_text(encoding="utf-8"))
    targets = json.loads(target_path.read_text(encoding="utf-8"))
    matching = json.loads(store_matching_path.read_text(encoding="utf-8"))
    target_rows = targets["jd_smoke_targets"] + targets["tmall_smoke_targets"]
    fixed_ids = {
        str(item.get("sku_id") or item.get("item_id"))
        for item in target_rows
        if item.get("sku_id") or item.get("item_id")
    }
    fixed_urls = {str(item["url"]) for item in target_rows if item.get("url")}
    fixed_stores = {str(item["shop_name"]): str(item["internal_store_id"]) for item in target_rows}
    known_stores = {
        str(item["shop_name_raw"]): str(item["internal_store_id"])
        for item in matching["matched_stores"]
    }
    classifier = SearchClassifier(
        fixed_product_ids=fixed_ids,
        fixed_urls=fixed_urls,
        fixed_stores=fixed_stores,
        known_stores=known_stores,
    )
    brand_spec = {}
    for item in target_rows:
        brand_spec.setdefault(item["brand"], item.get("spec_normalized") or item.get("spec_raw"))

    candidates = []
    for platform_key, platform in (("jd_search", "jd"), ("tmall_search", "taobao")):
        for search_observation in results.get(platform_key, []):
            brand = search_observation["brand"]
            raw_items = search_observation.get("opencli_data") or []
            if isinstance(raw_items, dict):
                raw_items = raw_items.get("items") or raw_items.get("results") or raw_items.get("data") or []
            for raw in raw_items:
                product_id = raw.get("sku") or raw.get("item_id")
                hit = SearchHit(
                    platform=platform,
                    query=search_observation.get("search_query", ""),
                    rank=raw.get("rank"),
                    title=str(raw.get("title") or ""),
                    url=raw.get("url"),
                    product_id=str(product_id) if product_id else None,
                    shop_name=raw.get("shop"),
                    list_price_raw=raw.get("price"),
                    raw=raw,
                )
                candidates.append(
                    classifier.classify(
                        hit,
                        target_brand=brand,
                        target_spec=brand_spec.get(brand),
                    ).model_dump(mode="json")
                )

    deduped = {}
    for item in candidates:
        key = (item["platform"], item.get("product_id"), item["target_brand"])
        deduped.setdefault(key, item)
    counts = Counter(item["candidate_type"] for item in deduped.values())
    report = {
        "raw_item_count": len(candidates),
        "deduplicated_candidate_count": len(deduped),
        "classification_rate": 1.0 if candidates else 0.0,
        "formal_price_count": sum(1 for item in deduped.values() if item["is_formal_price"]),
        "candidate_type_counts": dict(counts),
        "candidates": list(deduped.values()),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report

