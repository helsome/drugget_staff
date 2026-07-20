from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .collector import detect_access_state, is_valid_detail_page
from .pricing import parse_price
from .search import canonical_url


def _raw_field(observation: dict[str, Any], name: str) -> Any:
    for item in observation.get("opencli_data") or []:
        if isinstance(item, dict) and item.get("field") == name:
            return item.get("value")
    return None


def _review_observation(item: dict[str, Any]) -> dict[str, Any]:
    platform = "taobao" if item.get("platform") in {"tmall", "taobao"} else str(item.get("platform"))
    product_id = str(item.get("sku_id") or "")
    title = item.get("page_title")
    final_url = _raw_field(item, "链接") or item.get("final_url")
    access = detect_access_state(title, final_url, item.get("error_detail"))
    valid_page = bool(product_id) and access is None and is_valid_detail_page(
        platform,
        title=title,
        url=final_url,
        product_id=product_id,
    )
    parsed_price = parse_price(item.get("page_sale_price_raw"))
    extraction_eligible = valid_page and parsed_price is not None
    basis = str(item.get("calculation_basis_source") or "")
    package_cross_validated = "页面商品名称" in basis and "默认" not in basis
    calculation_eligible = bool(
        extraction_eligible
        and item.get("calculation_status") == "success"
        and package_cross_validated
        and item.get("sale_box_count")
        and item.get("units_per_box")
    )
    return {
        "platform": platform,
        "product_id": product_id,
        "collection_status": access.value if access else ("success" if valid_page else "page_changed"),
        "valid_detail_page": valid_page,
        "price_extraction_eligible": extraction_eligible,
        "package_cross_validated": package_cross_validated,
        "calculation_eligible": calculation_eligible,
        "claimed_result_status": item.get("result_status"),
        "confirmed_break_price": calculation_eligible and item.get("result_status") == "break_price",
        "reason": (
            access.value
            if access
            else "valid"
            if calculation_eligible
            else "not_detail_page"
            if not valid_page
            else "package_not_cross_validated"
            if not package_cross_validated
            else "not_calculable"
        ),
    }


def _search_metrics(payload: dict[str, Any], key: str) -> dict[str, Any]:
    raw = 0
    identities: set[str] = set()
    query_success = 0
    for query in payload.get(key) or []:
        if query.get("result_status") == "success":
            query_success += 1
        for hit in query.get("opencli_data") or []:
            if not isinstance(hit, dict):
                continue
            raw += 1
            product_id = str(hit.get("sku") or hit.get("item_id") or "")
            url = canonical_url(hit.get("url"))
            identities.add(product_id or url or f"invalid:{key}:{raw}")
    return {
        "queries": len(payload.get(key) or []),
        "successful_queries": query_success,
        "raw_hits": raw,
        "unique_hits": len(identities),
        "formal_price_observations": 0,
    }


def audit_legacy_smoke(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    observations = [
        *payload.get("jd_smoke_observations", []),
        *payload.get("tmall_smoke_observations", []),
    ]
    reviewed = [_review_observation(item) for item in observations]
    platforms: dict[str, Any] = {}
    for platform in ("jd", "taobao"):
        rows = [item for item in reviewed if item["platform"] == platform]
        statuses = Counter(item["collection_status"] for item in rows)
        platforms[platform] = {
            "targets": len(rows),
            "collection_statuses": dict(statuses),
            "valid_detail_pages": sum(item["valid_detail_page"] for item in rows),
            "price_extraction_eligible": sum(item["price_extraction_eligible"] for item in rows),
            "calculation_eligible": sum(item["calculation_eligible"] for item in rows),
            "confirmed_break_prices": sum(item["confirmed_break_price"] for item in rows),
        }
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": str(path),
        "stage": "P0",
        "verdict": "NOT_PASSED",
        "reason": [
            "京东首页跳转曾被计为成功并参与计算",
            "淘宝10条价格的页面盒数未由页面或第二独立证据交叉确认，不能计算单盒/最小单位价",
            "页面价格没有独立人工标注样本，因此不能声称准确率100%",
            "Search列表价仅为发现线索，正式价格观测数必须为0",
        ],
        "fixed_route": platforms,
        "search_route": {
            "jd": _search_metrics(payload, "jd_search"),
            "taobao": _search_metrics(payload, "tmall_search"),
        },
        "accuracy_metrics": {
            "page_price_accuracy": None,
            "sku_accuracy": None,
            "routing_accuracy": None,
            "note": "缺少独立人工真值样本；只报告可计算的覆盖数，不把未失败当作准确。",
        },
        "reviewed_observations": reviewed,
    }


def write_replay_report(report: dict[str, Any], *, json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    fixed = report["fixed_route"]
    search = report["search_route"]
    lines = [
        "# 7.14 历史烟测纠偏回放",
        "",
        f"- P0结论：**{report['verdict']}**",
        f"- 京东：{fixed['jd']['valid_detail_pages']}/{fixed['jd']['targets']} 个有效详情页，{fixed['jd']['calculation_eligible']} 个可计算",
        f"- 淘宝系：{fixed['taobao']['valid_detail_pages']}/{fixed['taobao']['targets']} 个有效详情页，{fixed['taobao']['calculation_eligible']} 个包装依据充分、可计算",
        f"- Search：京东 {search['jd']['raw_hits']} 条原始 / {search['jd']['unique_hits']} 条唯一；淘宝系 {search['taobao']['raw_hits']} 条原始 / {search['taobao']['unique_hits']} 条唯一",
        "- Search正式价格观测：0（必须进入详情页复核后才能入正式价格表）",
        "",
        "## 未通过原因",
        "",
        *[f"- {item}" for item in report["reason"]],
        "",
        "原始烟测 JSON 未修改；本报告是依据收紧后的口径生成的纠偏视图。",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
