from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import openpyxl

from .catalog import BRAND_TO_GENERIC, find_brand, normalize_brand, normalize_spec
from .data_quality import ANTUO_FILE, QUWEI_FILE, valid_value
from .enums import FixedTier


def normalize_shop_name(value: object) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    prefixes = (
        r"^(?:\d*人看过|\d*条评价|\d+\+?)?100%好评",
        r"^\+?\d+(?:\.\d+)?(?:万)?\+?人(?:种草|付款|已买|好评)",
        r"^好评(?:率)?\d+(?:\.\d+)?%",
        r"^(?:买过的店|广告|包邮|官方补贴)",
    )
    changed = True
    while changed:
        before = text
        for pattern in prefixes:
            text = re.sub(pattern, "", text)
        changed = text != before
    return text.strip()


def excel_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def extract_product_id(platform: str, url: str | None, raw_id: object = None) -> str | None:
    if raw_id not in (None, ""):
        value = str(raw_id).split(".")[0]
        if value and value.lower() != "none":
            return value
    if platform == "jd":
        match = re.search(r"/(\d{4,})\.html", str(url or ""))
    else:
        match = re.search(r"[?&]id=(\d+)", str(url or ""))
    return match.group(1) if match else None


def _read_rows(path: Path):
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    sheet.reset_dimensions()
    rows = sheet.iter_rows(values_only=True)
    headers = list(next(rows))
    index = {header: pos for pos, header in enumerate(headers) if header is not None}
    try:
        yield from ((index, row) for row in rows)
    finally:
        workbook.close()


def build_smoke_plan(
    *,
    source_dir: Path,
    smoke_store_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    smoke_stores = json.loads(smoke_store_path.read_text(encoding="utf-8"))
    selected_by_platform = {
        "jd": {item["shop_name_raw"]: item for item in smoke_stores["jd_top10"]},
        "taobao": {item["shop_name_raw"]: item for item in smoke_stores["tmall_top10"]},
    }
    records: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        "jd": defaultdict(lambda: defaultdict(list)),
        "taobao": defaultdict(lambda: defaultdict(list)),
    }

    for idx, row in _read_rows(source_dir / ANTUO_FILE):
        if row[idx["平台"]] != "京东":
            continue
        shop = normalize_shop_name(row[idx["店铺"]])
        if shop not in selected_by_platform["jd"]:
            continue
        brand = normalize_brand(row[idx["品牌"]])
        if brand not in BRAND_TO_GENERIC:
            continue
        url = str(row[idx["链接"]] or "")
        records["jd"][shop][url].append(
            {
                "captured_date": excel_date(row[idx["创建时间"]]),
                "brand": brand,
                "generic_name": BRAND_TO_GENERIC[brand],
                "spec_raw": row[idx["规格"]],
                "spec_normalized": normalize_spec(row[idx["规格"]]),
                "product_id": extract_product_id("jd", url, row[idx["商品ID"]]),
                "url": url,
                "history_price": row[idx["商品标价"]],
                "history_single_box_price": row[idx["单盒到手价"]],
                "history_box_count": row[idx["数量"]],
            }
        )

    for idx, row in _read_rows(source_dir / QUWEI_FILE):
        if row[idx["平台"]] != "天猫":
            continue
        shop = normalize_shop_name(row[idx["店铺名称"]])
        if shop not in selected_by_platform["taobao"]:
            continue
        brand = find_brand(row[idx["商品关键字"]], row[idx["商品标题"]], row[idx["规格"]])
        if not brand:
            continue
        url = str(row[idx["商品链接"]] or "")
        records["taobao"][shop][url].append(
            {
                "captured_date": excel_date(row[idx["采集时间"]]),
                "brand": brand,
                "generic_name": BRAND_TO_GENERIC[brand],
                "spec_raw": row[idx["规格"]],
                "spec_normalized": normalize_spec(row[idx["规格"]]),
                "product_id": extract_product_id("taobao", url),
                "url": url,
                "history_price": row[idx["当前价格"]],
                "history_single_box_price": row[idx["单盒价"]],
                "history_box_count": row[idx["盒数"]],
            }
        )

    plan: dict[str, list[dict[str, Any]]] = {"jd": [], "taobao": []}
    for platform in ("jd", "taobao"):
        for shop, store in selected_by_platform[platform].items():
            candidates = []
            for url, history in records[platform].get(shop, {}).items():
                dates = sorted({item["captured_date"] for item in history if item["captured_date"]})
                latest = max(
                    history,
                    key=lambda item: item["captured_date"] or date.min,
                )
                candidates.append(
                    (
                        len(dates) >= 2,
                        len(dates),
                        dates[-1] if dates else date.min,
                        len(history),
                        latest,
                        dates,
                    )
                )
            candidates.sort(key=lambda item: item[:4], reverse=True)
            if not candidates:
                plan[platform].append(
                    {
                        "platform": platform,
                        "shop_name": shop,
                        "internal_store_id": store["internal_store_id"],
                        "fixed_tier": FixedTier.OBSERVATION_ONLY,
                        "stable_link": False,
                        "enabled": False,
                        "reason": "no_historical_link",
                    }
                )
                continue
            stable, distinct_dates, latest_date, count, target, dates = candidates[0]
            has_responsibility = any(
                valid_value(store.get(field))
                for field in ("responsible_unit", "responsible_person", "contact")
            )
            has_involved = valid_value(store.get("involved_products_raw"))
            eligibility_basis = (
                "store_involved_products"
                if has_involved
                else "confirmed_historical_drug_record"
            )
            tier = (
                FixedTier.RESPONSIBILITY_CORE
                if stable and has_responsibility and (has_involved or bool(target.get("brand")))
                else FixedTier.OBSERVATION_ONLY
            )
            plan[platform].append(
                {
                    **target,
                    "platform": platform,
                    "shop_name": shop,
                    "internal_store_id": store["internal_store_id"],
                    "responsible_unit": store.get("responsible_unit"),
                    "responsible_person": store.get("responsible_person"),
                    "contact": store.get("contact"),
                    "eligibility_basis": eligibility_basis,
                    "fixed_tier": tier,
                    "stable_link": stable,
                    "stable_link_evidence": {
                        "distinct_collection_dates": distinct_dates,
                        "dates": [str(value) for value in dates],
                        "record_count": count,
                        "latest_date": str(latest_date),
                    },
                    "enabled": bool(target.get("product_id")),
                    "reason": "selected_latest_stable_link" if stable else "latest_link_requires_review",
                }
            )

    output = {
        "generated_at": datetime.now().isoformat(),
        "selection_rule": "one target per unique store; stable link preferred; newest observation selected",
        "jd_unique_stores": len({item["shop_name"] for item in plan["jd"]}),
        "taobao_unique_stores": len({item["shop_name"] for item in plan["taobao"]}),
        "jd_targets": plan["jd"],
        "taobao_targets": plan["taobao"],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output
