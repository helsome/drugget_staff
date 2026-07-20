from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import openpyxl

from price_specialist.catalog import (
    BRAND_TO_GENERIC,
    DRUG_MAP,
    find_target_brand,
    normalize_brand,
    normalize_spec,
    parse_control_prices,
    parse_package_units,
    CONTROL_PRICE_RULE_FIELDNAMES,
)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data/raw"
OUT = ROOT / "data/knowledge-base"
ANTUO = SOURCE / "安托监控数据2026年4-6月.xlsx"
QUWEI = SOURCE / "趣维1-3月总数据.xlsx"
STORES = SOURCE / "网络店铺档案明细表_2026.xlsx"
CONTROL = SOURCE / "价格标准表.md"

PLATFORM_CODE = {"京东": "jd", "天猫": "taobao", "淘宝": "taobao", "美团": "meituan", "拼多多": "pinduoduo", "药师帮": "yaoshibang", "1药城": "yiyaocheng", "京东/O2O": "jd_o2o"}
EMPTY = {None, "", "/", "待定", "无", "nan", "None"}


def clean(v):
    if v in EMPTY: return None
    s = str(v).strip()
    return s if s not in EMPTY else None


def dec(v):
    if v in EMPTY: return None
    try: return Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError): return None


def excel_date(v):
    if isinstance(v, datetime): return v.date()
    if isinstance(v, date): return v
    if isinstance(v, (int, float)): return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
    try: return datetime.fromisoformat(str(v)).date()
    except (TypeError, ValueError): return None


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()


def rows(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    ws.reset_dimensions()
    it = ws.iter_rows(values_only=True)
    headers = [clean(x) for x in next(it)]
    idx = {h: i for i, h in enumerate(headers) if h}
    try:
        for n, row in enumerate(it, 2):
            yield n, idx, row
    finally: wb.close()


def get(row, idx, key):
    i = idx.get(key)
    return row[i] if i is not None and i < len(row) else None


def normalize_shop(v):
    return re.sub(r"\s+", "", str(v or "")).strip()


def product_id(platform, url, raw=None):
    if clean(raw): return str(raw).split(".")[0]
    u = str(url or "")
    q = parse_qs(urlparse(u).query)
    if platform == "京东":
        m = re.search(r"/(\d{4,})\.html", u)
        return m.group(1) if m else None
    for k in ("id", "goods_id", "wholesaleid"):
        if q.get(k): return q[k][0]
    m = re.search(r"wholesaleid=(\d+)", u)
    return m.group(1) if m else None


def write_csv(name, fields, records):
    p = OUT / name
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in records: w.writerow({k: "" if r.get(k) is None else r.get(k) for k in fields})
    return p


def existing_control_metadata(output: Path) -> dict[tuple[str, str, str, str, str], dict[str, str]]:
    """Keep business approval fields when raw-source reconstruction is repeated."""
    path = output / "control_price_rules.csv"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            (row.get("brand", ""), row.get("generic_name", ""), row.get("spec_key", ""), row.get("min_unit", ""), row.get("source_line", "")): row
            for row in csv.DictReader(handle)
        }


def rebuilt_control_records(controls, preserved_controls, source_name: str):
    """Generate legacy rows while retaining approved full-spec business rows."""
    records = []
    for x in controls:
        key = (x.brand, x.generic_name, x.spec_key or "", x.min_unit, x.source_line)
        prior = preserved_controls.get(key, {})
        records.append({
            "brand": x.brand, "generic_name": x.generic_name, "spec_key": x.spec_key,
            "control_price_value": prior.get("control_price_value") or str(x.price),
            "control_price_basis": prior.get("control_price_basis") or "per_min_unit",
            "control_price_per_min_unit": prior.get("control_price_per_min_unit") or str(x.price),
            "min_unit": x.min_unit, "effective_from": prior.get("effective_from") or "2026-04-01",
            "effective_to": prior.get("effective_to") or "", "active": prior.get("active") or "True",
            "source_file": source_name, "source_line": x.source_line,
            "business_confirmed": prior.get("business_confirmed") or "False",
            "confirmed_by": prior.get("confirmed_by") or "", "confirmed_at": prior.get("confirmed_at") or "",
            "approval_reference": prior.get("approval_reference") or "",
        })
    generated_keys = {(row["brand"], row["generic_name"], row.get("spec_key") or "", row["min_unit"], row["source_line"]) for row in records}
    for key, prior in preserved_controls.items():
        if key not in generated_keys and prior.get("business_confirmed", "").strip().lower() == "true":
            records.append({field: prior.get(field, "") for field in CONTROL_PRICE_RULE_FIELDNAMES})
    return records


def build_knowledge_base(source: Path = SOURCE, output: Path = OUT) -> dict[str, object]:
    """Build a complete knowledge-base snapshot in ``output``.

    Callers that publish a snapshot should use :func:`build_atomically`; this
    function intentionally writes only to the supplied output directory.
    """
    global SOURCE, OUT, ANTUO, QUWEI, STORES, CONTROL
    SOURCE = source.resolve()
    OUT = output.resolve()
    ANTUO = SOURCE / "安托监控数据2026年4-6月.xlsx"
    QUWEI = SOURCE / "趣维1-3月总数据.xlsx"
    STORES = SOURCE / "网络店铺档案明细表_2026.xlsx"
    CONTROL = SOURCE / "价格标准表.md"
    missing = [path for path in (STORES, QUWEI, ANTUO, CONTROL) if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少知识库原始输入: " + ", ".join(str(path) for path in missing))
    preserved_controls = existing_control_metadata(OUT)
    OUT.mkdir(exist_ok=True)
    issues = []
    source_stats = {}

    # Store master: the workbook is the current responsibility source.
    store_fields = ["store_id", "platform", "platform_code", "platform_store_key", "shop_name", "license_name", "unified_credit_code", "customer_code", "responsible_unit", "responsible_person", "contact", "province", "city", "shop_status", "upstream_commercial_code", "upstream_commercial_name", "involved_products", "notes", "fixed_tier"]
    stores = []
    store_key = {}
    for n, idx, row in rows(STORES):
        sid, platform, shop = clean(get(row, idx, "店铺ID")), clean(get(row, idx, "平台")), clean(get(row, idx, "店铺名称"))
        if not sid: continue
        has_resp = any(clean(get(row, idx, k)) for k in ("责任单位", "责任人", "联系人"))
        status = clean(get(row, idx, "店铺状态")) or "未知"
        tier = "responsibility_core" if status == "正常" and clean(get(row, idx, "涉及品种")) and has_resp else "observation_only"
        r = {"store_id": sid, "platform": platform, "platform_code": PLATFORM_CODE.get(platform, platform), "platform_store_key": clean(get(row, idx, "平台&店铺")), "shop_name": shop, "license_name": clean(get(row, idx, "营业执照名称")), "unified_credit_code": clean(get(row, idx, "统一社会信用代码")), "customer_code": clean(get(row, idx, "客户编码")), "responsible_unit": clean(get(row, idx, "责任单位")), "responsible_person": clean(get(row, idx, "责任人")), "contact": clean(get(row, idx, "联系人")), "province": clean(get(row, idx, "省")), "city": clean(get(row, idx, "市")), "shop_status": status, "upstream_commercial_code": clean(get(row, idx, "上游商业编码")), "upstream_commercial_name": clean(get(row, idx, "上游商业名称")), "involved_products": clean(get(row, idx, "涉及品种")), "notes": clean(get(row, idx, "备注")), "fixed_tier": tier}
        stores.append(r); store_key[(r["platform_code"], normalize_shop(shop))] = r
    source_stats[STORES.name] = len(stores)

    controls = parse_control_prices(CONTROL)
    control_by_brand = {x.brand: x for x in controls}
    drug_counts, drug_specs = Counter(), defaultdict(set)
    observations = []
    exact_seen = Counter(); business_seen = Counter(); formula_mismatch = 0

    def add_obs(r):
        nonlocal formula_mismatch
        observations.append(r)
        drug_counts[r["brand"]] += 1
        if r["spec_normalized"]: drug_specs[r["brand"]].add(r["spec_normalized"])
        exact_seen[tuple(r.get(k) for k in ("source_file", "captured_at", "platform", "url", "spec_raw", "shop_name", "price"))] += 1
        business_seen[tuple(r.get(k) for k in ("source_file", "captured_at", "platform", "url", "spec_raw", "shop_name"))] += 1
        if r.get("source_file") == QUWEI.name and r.get("price") is not None and r.get("box_count") not in (None, 0) and r.get("single_box_price") is not None:
            if abs(Decimal(r["price"]) / Decimal(r["box_count"]) - Decimal(r["single_box_price"])) > Decimal("0.01"):
                formula_mismatch += 1; r["quality_status"] = "quarantined_formula_mismatch"

    # Historical price facts from Quwei.
    for n, idx, row in rows(QUWEI):
        brand = find_target_brand(get(row, idx, "商品关键字"), get(row, idx, "商品标题"), get(row, idx, "规格"))
        if not brand:
            issues.append({"source_file": QUWEI.name, "row_number": n, "issue_type": "unrecognized_drug", "severity": "medium", "business_key": clean(get(row, idx, "商品链接")), "details": clean(get(row, idx, "商品关键字"))}); continue
        spec_raw, url, shop, platform = clean(get(row, idx, "规格")), clean(get(row, idx, "商品链接")), clean(get(row, idx, "店铺名称")), clean(get(row, idx, "平台"))
        d = excel_date(get(row, idx, "采集时间")); c = control_by_brand.get(brand)
        sr = {"source_file": QUWEI.name, "source_row": n, "period": "2026-01~2026-03", "captured_at": d, "platform": platform, "platform_code": PLATFORM_CODE.get(platform, platform), "brand": brand, "generic_name": BRAND_TO_GENERIC[brand], "spec_raw": spec_raw, "spec_normalized": normalize_spec(spec_raw), "title": clean(get(row, idx, "商品标题")), "url": url, "product_id": product_id(platform, url), "price": dec(get(row, idx, "当前价格")), "box_count": dec(get(row, idx, "盒数")), "single_box_price": dec(get(row, idx, "单盒价")), "source_control_price": dec(get(row, idx, "控价")), "canonical_control_price": c.price if c else None, "source_below_flag": clean(get(row, idx, "是否低价")), "shop_name": shop, "seller_company": clean(get(row, idx, "商业")), "province": clean(get(row, idx, "省")), "city": clean(get(row, idx, "市")), "quality_status": "valid"}
        sr["store_id"] = (store_key.get((sr["platform_code"], normalize_shop(shop))) or {}).get("store_id")
        sr["store_match_status"] = "matched" if sr["store_id"] else "unmatched"
        if not sr["store_id"]: issues.append({"source_file": QUWEI.name, "row_number": n, "issue_type": "store_unmatched", "severity": "medium", "business_key": url, "details": f"{platform}|{shop}"})
        add_obs(sr)
    source_stats[QUWEI.name] = sum(1 for x in observations if x["source_file"] == QUWEI.name)

    # Historical monitoring facts from Antuo.
    for n, idx, row in rows(ANTUO):
        brand = normalize_brand(get(row, idx, "品牌"))
        if brand not in BRAND_TO_GENERIC:
            issues.append({"source_file": ANTUO.name, "row_number": n, "issue_type": "unrecognized_drug", "severity": "high", "business_key": clean(get(row, idx, "商品ID")), "details": clean(get(row, idx, "品牌"))}); continue
        spec_raw, url, shop, platform = clean(get(row, idx, "规格")), clean(get(row, idx, "链接")), clean(get(row, idx, "店铺")), clean(get(row, idx, "平台"))
        d = excel_date(get(row, idx, "创建时间")); c = control_by_brand.get(brand)
        sr = {"source_file": ANTUO.name, "source_row": n, "period": "2026-04~2026-06", "captured_at": d, "platform": platform, "platform_code": PLATFORM_CODE.get(platform, platform), "brand": brand, "generic_name": BRAND_TO_GENERIC[brand], "spec_raw": spec_raw, "spec_normalized": normalize_spec(spec_raw), "title": clean(get(row, idx, "商品标题")), "url": url, "product_id": product_id(platform, url, get(row, idx, "商品ID")), "price": dec(get(row, idx, "最低成交价")), "box_count": dec(get(row, idx, "数量")), "single_box_price": dec(get(row, idx, "单盒到手价")), "source_control_price": None, "canonical_control_price": c.price if c else None, "source_below_flag": clean(get(row, idx, "是否破价")), "source_break_amount": dec(get(row, idx, "破价金额")), "shop_name": shop, "seller_company": clean(get(row, idx, "卖家")), "province": clean(get(row, idx, "发货地址")), "city": None, "quality_status": "valid"}
        sr["store_id"] = (store_key.get((sr["platform_code"], normalize_shop(shop))) or {}).get("store_id")
        sr["store_match_status"] = "matched" if sr["store_id"] else "unmatched"
        if not sr["store_id"]: issues.append({"source_file": ANTUO.name, "row_number": n, "issue_type": "store_unmatched", "severity": "medium", "business_key": url, "details": f"{platform}|{shop}"})
        add_obs(sr)
    source_stats[ANTUO.name] = sum(1 for x in observations if x["source_file"] == ANTUO.name)

    raw_observation_count = len(observations)
    # Remove exact duplicates while retaining same-key rows with different prices.
    # The latter are legitimate competing observations and remain visible.
    seen_clean = set()
    deduped = []
    for r in observations:
        key = tuple(r.get(k) for k in ("source_file", "captured_at", "platform", "url", "spec_raw", "shop_name", "price", "single_box_price"))
        if key in seen_clean:
            continue
        seen_clean.add(key); deduped.append(r)
    observations = deduped

    # Apply derived fields after package units and control prices are known.
    for r in observations:
        units, min_unit = parse_package_units(r["spec_normalized"])
        r["units_per_box"], r["min_unit"] = units, min_unit
        r["comparison_unit_price"] = (r["single_box_price"] / units) if r["single_box_price"] is not None and units else None
        cp, up = r["canonical_control_price"], r["comparison_unit_price"]
        r["canonical_price_status"] = ("below_control" if up < cp else "at_or_above_control") if cp is not None and up is not None else "not_evaluated"
        if not r["url"] or not r["product_id"]: issues.append({"source_file": r["source_file"], "row_number": r["source_row"], "issue_type": "missing_product_identity", "severity": "high", "business_key": r["url"], "details": r["brand"]})

    obs_fields = ["source_file","source_row","period","captured_at","platform","platform_code","store_id","store_match_status","brand","generic_name","spec_raw","spec_normalized","title","url","product_id","price","box_count","single_box_price","units_per_box","min_unit","comparison_unit_price","source_control_price","canonical_control_price","canonical_price_status","source_below_flag","source_break_amount","shop_name","seller_company","province","city","quality_status"]
    write_csv("price_observations_clean.csv", obs_fields, observations)

    package_records = []
    for brand in sorted(drug_specs):
        for spec in sorted(drug_specs[brand]):
            units, unit = parse_package_units(spec)
            raws = sorted({r["spec_raw"] for r in observations if r["brand"] == brand and r["spec_normalized"] == spec and r["spec_raw"]})
            package_records.append({"package_id": f"{brand}|{spec}", "brand": brand, "generic_name": BRAND_TO_GENERIC[brand], "spec_normalized": spec, "spec_raw_examples": "；".join(raws), "units_per_box": units, "min_unit": unit, "source_count": sum(1 for r in observations if r["brand"] == brand and r["spec_normalized"] == spec), "verified": "historical_observation"})
    write_csv("drug_package_master.csv", ["package_id","brand","generic_name","spec_normalized","spec_raw_examples","units_per_box","min_unit","source_count","verified"], package_records)

    control_records = rebuilt_control_records(controls, preserved_controls, CONTROL.name)
    write_csv("control_price_rules.csv", CONTROL_PRICE_RULE_FIELDNAMES, control_records)

    drug_records = []
    categories = {}
    cat = "未标注"
    for line in CONTROL.read_text(encoding="utf-8").splitlines():
        if line.strip() in {"甲类","乙类","丙类"}: cat = line.strip()
        for brand in BRAND_TO_GENERIC:
            if brand in line: categories[brand] = cat
    for generic, brand in sorted(DRUG_MAP.items(), key=lambda x: x[1]):
        cp = control_by_brand.get(brand)
        drug_records.append({"brand": brand, "generic_name": generic, "category": categories.get(brand,"未标注"), "control_rule_count": sum(1 for x in controls if x.brand == brand), "history_record_count": drug_counts.get(brand,0), "history_covered": bool(drug_counts.get(brand)), "coverage_status": "historical_covered" if drug_counts.get(brand) else "search_cold_start", "default_control_price_per_min_unit": cp.price if cp else None, "default_min_unit": cp.min_unit if cp else None})
    write_csv("drug_master.csv", ["brand","generic_name","category","control_rule_count","history_record_count","history_covered","coverage_status","default_control_price_per_min_unit","default_min_unit"], drug_records)
    write_csv("store_master.csv", store_fields, stores)
    write_csv("responsibility_relations.csv", ["store_id","platform","platform_code","shop_name","shop_status","responsible_unit","responsible_person","contact","province","city","involved_products","fixed_tier"], stores)

    # One monitor task per historical store/product/spec; latest link is the active candidate.
    grouped = defaultdict(list)
    for r in observations:
        if r["url"] and r["brand"] and r["spec_normalized"]:
            grouped[(r["platform_code"], r["store_id"] or "UNMATCHED", r["brand"], r["spec_normalized"], r["product_id"] or "UNKNOWN")].append(r)
    tasks = []
    for (plat, sid, brand, spec, pid), rs in sorted(grouped.items()):
        latest = max(rs, key=lambda x: x["captured_at"] or date.min)
        store = next((s for s in stores if s["store_id"] == sid), None)
        dates = sorted({str(x["captured_at"]) for x in rs if x["captured_at"]})
        stable = len(dates) >= 2
        tier = store["fixed_tier"] if store else "observation_only"
        tasks.append({"task_key": f"{plat}|{sid}|{brand}|{spec}|{pid}", "task_type": "recheck_existing_link", "platform_code": plat, "store_id": None if sid == "UNMATCHED" else sid, "shop_name": latest["shop_name"], "brand": brand, "generic_name": latest["generic_name"], "spec_normalized": spec, "product_id": None if pid == "UNKNOWN" else pid, "url": latest["url"], "fixed_tier": tier, "stable_link": stable, "historical_observation_count": len(rs), "distinct_capture_dates": len(dates), "latest_captured_at": latest["captured_at"], "enabled": bool(pid != "UNKNOWN" and stable), "review_reason": "stable_historical_link" if stable else "single_capture_or_unstable_link"})
    write_csv("monitor_task_master.csv", ["task_key","task_type","platform_code","store_id","shop_name","brand","generic_name","spec_normalized","product_id","url","fixed_tier","stable_link","historical_observation_count","distinct_capture_dates","latest_captured_at","enabled","review_reason"], tasks)

    issues += [{"source_file": QUWEI.name, "row_number": None, "issue_type": "exact_duplicate_rows", "severity": "medium", "business_key": None, "details": sum(v-1 for v in exact_seen.values() if v > 1)}, {"source_file": QUWEI.name, "row_number": None, "issue_type": "business_key_duplicates", "severity": "high", "business_key": None, "details": sum(v-1 for v in business_seen.values() if v > 1)}, {"source_file": QUWEI.name, "row_number": None, "issue_type": "formula_mismatches", "severity": "high", "business_key": None, "details": formula_mismatch}]
    write_csv("data_quality_issues.csv", ["source_file","row_number","issue_type","severity","business_key","details"], issues)

    manifest = {"generated_at": datetime.now().isoformat(), "source_directory": str(SOURCE), "source_files": [{"file": p.name, "sha256": sha(p), "row_count": source_stats.get(p.name), "role": "raw_source"} for p in (STORES, QUWEI, ANTUO, CONTROL)], "outputs": {"store_master": len(stores), "responsibility_relations": len(stores), "drug_master": len(drug_records), "control_price_rules": len(control_records), "drug_package_master": len(package_records), "price_observations_clean": len(observations), "monitor_task_master": len(tasks), "data_quality_issues": len(issues)}, "quality_summary": {"raw_recognized_observation_rows": raw_observation_count, "exact_duplicates_removed": raw_observation_count - len(observations), "unrecognized_rows": sum(1 for x in issues if x["issue_type"] == "unrecognized_drug"), "unmatched_store_rows": sum(1 for x in issues if x["issue_type"] == "store_unmatched"), "formula_mismatch_rows": formula_mismatch, "exact_duplicates_extra_rows": sum(v-1 for v in exact_seen.values() if v > 1), "business_key_duplicate_extra_rows": sum(v-1 for v in business_seen.values() if v > 1)}, "rules": {"canonical_price_source": CONTROL.name, "historical_source_flag_preserved": True, "formal_price_requires_detail_page": True, "responsibility_source": STORES.name, "unmatched_or_unstable_tasks_enabled": False}}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = f'''# 价格专员业务知识库\n\n生成时间：{manifest["generated_at"]}\n\n本知识库由项目目录中的历史 Excel、控价标准和店铺档案生成。原始文件未修改。\n\n## 知识域\n\n- `store_master.csv`：网络店铺档案主表。\n- `drug_master.csv`：药品/商品名/通用名/分类及历史覆盖。\n- `drug_package_master.csv`：药品与标准化规格、包装数量和最小单位。\n- `control_price_rules.csv`：控价规则，当前判断以 `价格标准表.md` 为准。\n- `responsibility_relations.csv`：店铺、责任单位、责任人及监控层级。\n- `monitor_task_master.csv`：由历史链接聚合出的复查任务；只有商品身份完整且至少跨 2 个采集日期出现的链接默认启用。\n- `price_observations_clean.csv`：统一后的 1–6 月历史价格事实，保留来源侧控价和低价标记，并计算统一单件价。\n- `data_quality_issues.csv`：未识别药品、店铺未匹配、重复、公式不一致和缺少商品身份等问题。\n\n## 质量摘要\n\n- 价格事实：{len(observations):,} 条（识别后原始行 {raw_observation_count:,}，已移除完全重复 {raw_observation_count-len(observations):,}）；监控任务：{len(tasks):,} 条；店铺：{len(stores):,} 家。\n- 未识别药品行：{manifest["quality_summary"]["unrecognized_rows"]:,}；店铺未匹配行：{manifest["quality_summary"]["unmatched_store_rows"]:,}。\n- 趣维单盒价公式不一致：{formula_mismatch:,} 行；这些行标记为隔离，不参与可信计算。\n- 规则解释：历史来源中的“是否低价/是否破价”仅作历史事实；当前规则判断使用标准表控价，并要求规格、包装和最小单位可解析。\n- 安托工作簿的 Excel 维度元数据异常，读取时已按实际单元格重置维度；源文件未改写。\n\n## 使用边界\n\n正式价格结论必须经过详情页确认；搜索列表价只能作为候选发现。责任关系以当前店铺档案为准；历史中无法匹配责任关系的任务保持观察态，不自动通知。\n'''
    (OUT / "README.md").write_text(md, encoding="utf-8")
    missing_identity = sum(1 for x in issues if x["issue_type"] == "missing_product_identity")
    unmatched_rows = manifest["quality_summary"]["unmatched_store_rows"]
    report = f'''# 数据清洗与质量报告\n\n## 数据范围\n\n- 店铺档案：{len(stores):,} 行，来源为 `网络店铺档案明细表_2026.xlsx`。\n- 趣维历史：{source_stats[QUWEI.name]:,} 行识别为正式药品记录。\n- 安托历史：{source_stats[ANTUO.name]:,} 行识别为正式药品记录。\n- 控价规则：{len(control_records):,} 条；同一商品存在多个规格规则时保留为多条。\n\n## 清洗动作\n\n1. 重置 Excel 错误的工作表维度后读取实际数据，不改写原始文件。\n2. 统一平台编码、商品名、规格、包装数量和最小单位。\n3. 从 URL 或商品 ID 提取商品身份；无法提取的记录进入质量问题清单。\n4. 按平台+店铺名匹配当前店铺档案，无法匹配的记录不进入责任核心。\n5. 完全重复事实移除；同业务键但价格不同的记录保留并单独统计。\n6. 以控价标准表计算标准化单件价状态，同时保留历史来源侧标记。\n\n## 发现与风险\n\n|问题|数量|严重度|影响|\n|---|---:|---|---|\n|未识别药品|{manifest["quality_summary"]["unrecognized_rows"]:,}|中/高|无法可靠关联药品知识|\n|店铺未匹配|{unmatched_rows:,}|中|不能自动路由责任人|\n|缺少商品身份|{missing_identity:,}|高|不能生成稳定复查任务|\n|单盒价公式不一致|{formula_mismatch:,}|高|金额计算存在可信性风险|\n|完全重复移除|{raw_observation_count-len(observations):,}|中|避免重复计数|\n|同业务键重复额外行|{manifest["quality_summary"]["business_key_duplicate_extra_rows"]:,}|高|需结合采集轮次/价格判断是否为重复抓取|\n\n详细行级记录见 `data_quality_issues.csv`，生成摘要和源文件哈希见 `manifest.json`。\n\n## 建议\n\n优先补齐缺少商品 ID/链接的 {missing_identity:,} 行，并建立店铺别名映射处理 {unmatched_rows:,} 行未匹配记录；对趣维业务键重复先按采集轮次和价格确认是否应压缩；正式上线前将“详情页确认、规格匹配、责任关系匹配、控价版本有效期”设为任务放行条件。\n'''
    (OUT / "data_quality_report.md").write_text(report, encoding="utf-8")
    return manifest


def validate_snapshot(directory: Path) -> None:
    required = {
        "manifest.json", "README.md", "data_quality_report.md",
        "store_master.csv", "drug_master.csv", "drug_package_master.csv",
        "control_price_rules.csv", "responsibility_relations.csv",
        "monitor_task_master.csv", "price_observations_clean.csv",
        "data_quality_issues.csv",
    }
    missing = sorted(name for name in required if not (directory / name).is_file())
    if missing:
        raise ValueError("知识库快照不完整: " + ", ".join(missing))
    json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def build_atomically(source: Path, output: Path) -> dict[str, object]:
    """Stage and validate all generated files before replacing published files."""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="knowledge-base-", dir=output.parent) as temp:
        staged = Path(temp) / "snapshot"
        manifest = build_knowledge_base(source, staged)
        validate_snapshot(staged)
        output.mkdir(parents=True, exist_ok=True)
        for path in staged.iterdir():
            os.replace(path, output / path.name)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="从原始业务数据构建完整价格知识库")
    parser.add_argument("--source", type=Path, default=ROOT / "data/raw", help="原始 Excel 与控价 Markdown 所在目录")
    parser.add_argument("--output", type=Path, default=ROOT / "data/knowledge-base", help="发布知识库目录")
    parser.add_argument("--check", action="store_true", help="仅检查原始输入是否齐全，不生成文件")
    args = parser.parse_args()
    source = args.source.resolve()
    required = [
        source / "网络店铺档案明细表_2026.xlsx",
        source / "趣维1-3月总数据.xlsx",
        source / "安托监控数据2026年4-6月.xlsx",
        source / "价格标准表.md",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        parser.error("缺少原始输入: " + ", ".join(str(path) for path in missing))
    if args.check:
        print(json.dumps({"source": str(source), "status": "ready"}, ensure_ascii=False))
        return
    manifest = build_atomically(source, args.output)
    print(json.dumps(manifest["outputs"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
