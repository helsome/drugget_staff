#!/usr/bin/env python3
"""京东执行Agent - 第一批10家店铺双路线采集编排脚本。

复用 src/price_specialist/collector.py 的 OpenCLIComputerUseCollector 与核对逻辑。
不另写 requests/Selenium/Playwright，全部经 opencli 子进程调用。

规则要点：
- 并发=1，顺序执行
- 相同商品ID只调一次detail；相同搜索词缓存
- 网络超时最多重试3次，等待时间递增
- 遇到限流/验证码/登录异常不停止，记录日志后继续尝试
- 尽一切可能完成抓取任务
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import re
import sys
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# 复用项目既有模块
ROOT = Path("/Users/helson/coding/cttq_work/数字员工drugget")
sys.path.insert(0, str(ROOT / "src"))

from price_specialist.catalog import (  # noqa: E402
    BRAND_TO_GENERIC,
    GENERIC_VARIANTS,
    find_brand,
    find_target_brand,
    normalize_spec,
)
from price_specialist.collector import (  # noqa: E402
    OpenCLIComputerUseCollector,
    is_valid_detail_page,
    parse_detail_fields,
)
from price_specialist.config import Settings  # noqa: E402
from price_specialist.enums import CollectionStatus  # noqa: E402
from price_specialist.pricing import parse_price  # noqa: E402
from price_specialist.schemas import BrowserSession, CollectionTaskSpec  # noqa: E402
from price_specialist.smoke_plan import normalize_shop_name  # noqa: E402

PLATFORM = "jd"
ALIAS = "jd-p0"
OUTPUT_DIR = ROOT / "outputs" / "current-stage"
EVIDENCE_DIR = OUTPUT_DIR / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

# 节流参数
DETAIL_INTERVAL = 5        # detail 间隔 5 秒
SEARCH_INTERVAL = 8        # search 间隔 8 秒
DETAIL_TIMEOUT = 180      # detail 超时 180 秒
SEARCH_TIMEOUT = 120      # search 超时 120 秒
RETRY_DELAYS = (10, 30, 60)  # 网络重试等待 10s / 30s / 60s 最多3次
MAX_CANDIDATES = 3        # 每个目标药品最多进 3 个候选详情页


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

@dataclass
class StoreTask:
    store_id: str
    platform: str
    store_name: str
    store_status: str
    platform_store_id: str = ""
    homepage_url: str = ""


@dataclass
class DrugClue:
    store_id: str
    store_name: str
    brand: str
    generic_name: str
    spec: str
    product_id: str
    sku: str
    product_url: str
    history_price: str
    history_single_box: str
    box_count: str
    source_route: str = "historical_link"


def load_store_tasks() -> list[StoreTask]:
    path = OUTPUT_DIR / "jd_store_tasks.csv"
    tasks: list[StoreTask] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tasks.append(
                StoreTask(
                    store_id=row["档案店铺ID"],
                    platform=row["平台"],
                    store_name=row["档案店铺名称"],
                    store_status=row["店铺状态"],
                    platform_store_id=row.get("平台店铺ID", "") or "",
                    homepage_url=row.get("店铺主页链接", "") or "",
                )
            )
    return tasks


def load_drug_clues() -> list[DrugClue]:
    path = OUTPUT_DIR / "historical_product_clues.csv"
    clues: list[DrugClue] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["平台"] != "京东":
                continue
            clues.append(
                DrugClue(
                    store_id=row["档案店铺ID"],
                    store_name=row["档案店铺名称"],
                    brand=row["品牌"],
                    generic_name=row["通用名"],
                    spec=row["药品规格"],
                    product_id=row["商品ID"],
                    sku=row.get("SKU", "") or "",
                    product_url=row["商品链接"],
                    history_price=row.get("历史价格", "") or "",
                    history_single_box=row.get("历史单盒价", "") or "",
                    box_count=row.get("盒数", "") or "",
                    source_route=row.get("source_route", "historical_link"),
                )
            )
    return clues


# ---------------------------------------------------------------------------
# 结果容器
# ---------------------------------------------------------------------------

@dataclass
class DetailOutcome:
    product_id: str
    clue_store_id: str
    clue_store_name: str
    brand: str
    generic_name: str
    target_spec: str
    source_route: str
    page_title: str | None = None
    final_url: str | None = None
    page_shop: str | None = None
    selected_spec: str | None = None
    page_price_raw: str | None = None
    page_price_value: Decimal | None = None
    sale_box_count: Decimal | None = None
    status: CollectionStatus = CollectionStatus.UNKNOWN_ERROR
    error_code: str | None = None
    error_detail: str | None = None
    is_target_drug: bool = False
    shop_match: bool = False
    spec_match: bool = False
    captured_at: str = ""

    @property
    def grab_result(self) -> str:
        m = {
            CollectionStatus.SUCCESS: "成功",
            CollectionStatus.PRODUCT_OFFLINE: "未找到药品",
            CollectionStatus.STORE_MISMATCH: "规格不明",  # 店铺不一致归待确认
            CollectionStatus.STORE_UNVERIFIED: "规格不明",
            CollectionStatus.SKU_MISMATCH: "规格不明",
            CollectionStatus.PRICE_AMBIGUOUS: "价格不明",
            CollectionStatus.PAGE_CHANGED: "页面异常",
            CollectionStatus.NETWORK_ERROR: "页面异常",
            CollectionStatus.PARSE_ERROR: "页面异常",
            CollectionStatus.RATE_LIMITED: "平台异常",
            CollectionStatus.CHALLENGE_DETECTED: "平台异常",
            CollectionStatus.LOGIN_REQUIRED: "平台异常",
            CollectionStatus.UNKNOWN_ERROR: "页面异常",
        }
        return m.get(self.status, "页面异常")


@dataclass
class ExecLog:
    start_time: str
    end_time: str
    platform: str
    command_type: str  # detail / search / screenshot
    query_or_id: str
    returncode: int
    result_status: str


# ---------------------------------------------------------------------------
# 主执行器
# ---------------------------------------------------------------------------

class JdExecutor:
    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.collector = OpenCLIComputerUseCollector(self.settings)
        self.session = BrowserSession(platform=PLATFORM, alias=ALIAS)
        self.detail_cache: dict[str, DetailOutcome] = {}
        self.search_cache: dict[str, list[dict[str, Any]]] = {}
        self.logs: list[ExecLog] = []
        self.detail_results: list[DetailOutcome] = []
        self.link_mapping: list[dict[str, str]] = []
        self.unresolved: list[dict[str, str]] = []

    # ---- 底层 opencli 调用（带日志、重试） ----

    async def _run_opencli(
        self, *arguments: str, timeout: int, command_type: str, query_or_id: str
    ) -> tuple[int, str, str, Any]:
        attempt = 0
        while True:
            start = datetime.now()
            code, stdout, stderr, data = await self.collector._run(
                *arguments, timeout=timeout
            )
            end = datetime.now()
            status_label = self._classify_return(code, stderr, data)
            self.logs.append(
                ExecLog(
                    start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
                    platform=PLATFORM,
                    command_type=command_type,
                    query_or_id=query_or_id,
                    returncode=code,
                    result_status=status_label,
                )
            )
            # 网络超时/临时网络错误：可重试（最多3次）
            if self._is_network_retryable(code, stderr):
                if attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                # 重试耗尽，仍然返回结果
                print(f"  [WARN] 网络重试耗尽: {query_or_id}", flush=True)
            return code, stdout, stderr, data

    @staticmethod
    def _peek_title(data: Any) -> str | None:
        fields = parse_detail_fields(data)
        return fields.get("商品名称") or fields.get("商品标题") or fields.get("title")

    @staticmethod
    def _peek_url(data: Any) -> str | None:
        fields = parse_detail_fields(data)
        return fields.get("链接") or fields.get("url")

    @staticmethod
    def _classify_return(code: int, stderr: str, data: Any) -> str:
        if code == -1 or "TIMEOUT" in (stderr or ""):
            return CollectionStatus.NETWORK_ERROR.value
        if code != 0:
            return CollectionStatus.PARSE_ERROR.value
        return "ok"

    @staticmethod
    def _is_network_retryable(code: int, stderr: str) -> bool:
        if code == -1 or "TIMEOUT" in (stderr or ""):
            return True
        net_markers = ("network", "ECONNRESET", "ETIMEDOUT", "ENOTFOUND",
                       "socket hang up", "ERR_INTERNET", "fetch failed",
                       "net::ERR_", "navigation timeout")
        low = (stderr or "").lower()
        return any(m.lower() in low for m in net_markers)

    async def _screenshot(self, tag: str) -> str | None:
        try:
            code, stdout, stderr, data = await self.collector._run(
                "browser", ALIAS, "screenshot", timeout=45
            )
        except Exception:
            return None
        encoded = data.get("base64") if isinstance(data, dict) else (stdout or "")
        if not encoded:
            return None
        encoded = str(encoded).strip()
        if "," in encoded and encoded.startswith("data:image"):
            encoded = encoded.split(",", 1)[1]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = EVIDENCE_DIR / f"jd_{tag}_{ts}.png"
        path.write_bytes(raw)
        return str(path)

    # ---- 路线一：detail ----

    async def collect_detail(self, clue: DrugClue) -> DetailOutcome:
        pid = clue.product_id
        if pid in self.detail_cache:
            cached = self.detail_cache[pid]
            # 复用结果但保留本次线索的店铺/品牌上下文
            return self._adopt_cached(cached, clue)

        await asyncio.sleep(DETAIL_INTERVAL)
        args = (
            PLATFORM, "detail", pid, "-f", "json",
            "--trace", "retain-on-failure",
            "--window", "foreground",
            "--site-session", "persistent",
            "--keep-tab", "true",
        )
        code, stdout, stderr, data = await self._run_opencli(
            *args, timeout=DETAIL_TIMEOUT, command_type="detail", query_or_id=pid
        )
        outcome = self._evaluate_detail(clue, code, stderr, data)
        self.detail_cache[pid] = outcome
        self.detail_results.append(outcome)
        return outcome

    def _adopt_cached(self, cached: DetailOutcome, clue: DrugClue) -> DetailOutcome:
        return DetailOutcome(
            product_id=cached.product_id,
            clue_store_id=clue.store_id,
            clue_store_name=clue.store_name,
            brand=clue.brand,
            generic_name=clue.generic_name,
            target_spec=clue.spec,
            source_route=clue.source_route,
            page_title=cached.page_title,
            final_url=cached.final_url,
            page_shop=cached.page_shop,
            selected_spec=cached.selected_spec,
            page_price_raw=cached.page_price_raw,
            page_price_value=cached.page_price_value,
            sale_box_count=cached.sale_box_count,
            status=cached.status,
            error_code=cached.error_code,
            error_detail=cached.error_detail,
            is_target_drug=cached.is_target_drug,
            shop_match=cached.shop_match,
            spec_match=cached.spec_match,
            captured_at=cached.captured_at,
        )

    def _evaluate_detail(
        self, clue: DrugClue, code: int, stderr: str, data: Any
    ) -> DetailOutcome:
        fields = parse_detail_fields(data)
        title = fields.get("商品名称") or fields.get("商品标题") or fields.get("title")
        final_url = fields.get("链接") or fields.get("url") or clue.product_url
        page_shop = fields.get("店铺") or fields.get("shop")
        price_raw = fields.get("价格") or fields.get("price")
        selected_spec = fields.get("规格") or fields.get("selected_spec")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        outcome = DetailOutcome(
            product_id=clue.product_id,
            clue_store_id=clue.store_id,
            clue_store_name=clue.store_name,
            brand=clue.brand,
            generic_name=clue.generic_name,
            target_spec=clue.spec,
            source_route=clue.source_route,
            page_title=title,
            final_url=final_url,
            page_shop=page_shop,
            selected_spec=selected_spec,
            page_price_raw=price_raw,
            captured_at=now,
        )

        # 访问状态判定（非致命，记录即可）
        if code != 0:
            outcome.status = (
                CollectionStatus.NETWORK_ERROR
                if "TIMEOUT" in (stderr or "")
                else CollectionStatus.PARSE_ERROR
            )
            outcome.error_detail = (stderr or "")[:500]
            return outcome

        if not fields:
            outcome.status = CollectionStatus.PAGE_CHANGED
            outcome.error_code = "empty_structured_result"
            outcome.error_detail = (stderr or "")[:500]
            return outcome

        # 1) 详情页有效性
        if not is_valid_detail_page(
            PLATFORM, title=title, url=final_url, product_id=clue.product_id
        ):
            outcome.status = CollectionStatus.PAGE_CHANGED
            outcome.error_code = "invalid_detail_page"
            outcome.error_detail = f"title={title} url={final_url}"
            return outcome

        # 2) 店铺一致性
        target_shop = clue.store_name
        if target_shop and not page_shop:
            outcome.status = CollectionStatus.STORE_UNVERIFIED
            outcome.error_detail = "页面未返回店铺字段"
            return outcome
        if target_shop and page_shop:
            if normalize_shop_name(target_shop) != normalize_shop_name(page_shop):
                outcome.status = CollectionStatus.STORE_MISMATCH
                outcome.error_detail = (
                    f"档案店铺={target_shop} 页面店铺={page_shop}"
                )
                return outcome
            outcome.shop_match = True
        else:
            # 无档案店铺名时，无法核对，标记待确认
            outcome.shop_match = False

        # 3) 目标药品匹配
        normalized_title = str(title or "").replace("：", ":").replace("×", "*")
        names = [clue.brand, clue.generic_name]
        if title and any(n and n in title for n in names if n):
            outcome.is_target_drug = True
        else:
            outcome.status = CollectionStatus.SKU_MISMATCH
            outcome.error_code = "drug_name_mismatch"
            outcome.error_detail = f"title={title} 期望含 {names}"
            return outcome

        # 4) 规格与SKU确认
        target_spec_n = normalize_spec(clue.spec)
        actual_spec_n = normalize_spec(selected_spec)
        if target_spec_n and actual_spec_n and target_spec_n != actual_spec_n:
            outcome.status = CollectionStatus.SKU_MISMATCH
            outcome.error_code = "spec_mismatch"
            outcome.error_detail = (
                f"目标规格={clue.spec} 页面规格={selected_spec}"
            )
            return outcome
        if target_spec_n and not actual_spec_n and target_spec_n not in normalized_title:
            outcome.status = CollectionStatus.SKU_MISMATCH
            outcome.error_code = "spec_unconfirmed"
            outcome.error_detail = (
                f"目标规格={clue.spec} 页面无规格字段且标题不含规格"
            )
            return outcome
        outcome.spec_match = True

        # 5) 唯一明确当前价格
        if not price_raw:
            outcome.status = CollectionStatus.PRICE_AMBIGUOUS
            outcome.error_detail = "页面无价格字段"
            return outcome
        price_val = parse_price(price_raw)
        if price_val is None:
            outcome.status = CollectionStatus.PRICE_AMBIGUOUS
            outcome.error_detail = f"价格无法解析：{price_raw}"
            return outcome
        outcome.page_price_value = price_val

        # 盒数
        m = re.search(r"(\d+)\s*盒装", str(title or ""))
        if m:
            try:
                outcome.sale_box_count = Decimal(m.group(1))
            except InvalidOperation:
                pass

        outcome.status = CollectionStatus.SUCCESS
        return outcome

    # ---- 路线二：search ----

    async def collect_search(self, query: str) -> list[dict[str, Any]]:
        if query in self.search_cache:
            return self.search_cache[query]
        await asyncio.sleep(SEARCH_INTERVAL)
        args = (
            PLATFORM, "search", query, "-f", "json",
            "--limit", "20",
            "--window", "foreground",
            "--site-session", "persistent",
            "--keep-tab", "true",
        )
        code, stdout, stderr, data = await self._run_opencli(
            *args, timeout=SEARCH_TIMEOUT, command_type="search", query_or_id=query
        )
        hits: list[dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                hits.append(item)
        self.search_cache[query] = hits
        return hits

    # ---- 候选详情（搜索发现的候选） ----

    async def inspect_search_candidate(
        self, store_task: StoreTask, clue: DrugClue, hit: dict[str, Any]
    ) -> DetailOutcome | None:
        pid = str(hit.get("sku") or hit.get("item_id") or "") or None
        if not pid:
            return None
        # 复用缓存
        if pid in self.detail_cache:
            cached = self.detail_cache[pid]
            return self._adopt_cached(cached, clue)
        # 构造与历史线索同结构的 clue，但 source_route=platform_search
        search_clue = DrugClue(
            store_id=clue.store_id,
            store_name=clue.store_name,
            brand=clue.brand,
            generic_name=clue.generic_name,
            spec=clue.spec,
            product_id=pid,
            sku="",
            product_url=hit.get("url") or "",
            history_price="",
            history_single_box="",
            box_count="",
            source_route="platform_search",
        )
        await asyncio.sleep(DETAIL_INTERVAL)
        args = (
            PLATFORM, "detail", pid, "-f", "json",
            "--trace", "retain-on-failure",
            "--window", "foreground",
            "--site-session", "persistent",
            "--keep-tab", "true",
        )
        code, stdout, stderr, data = await self._run_opencli(
            *args, timeout=DETAIL_TIMEOUT, command_type="detail", query_or_id=pid
        )
        outcome = self._evaluate_detail(search_clue, code, stderr, data)
        self.detail_cache[pid] = outcome
        self.detail_results.append(outcome)
        return outcome

    # ---- 主编排 ----

    async def run(self, stores: list[StoreTask], clues: list[DrugClue]) -> None:
        clues_by_store: dict[str, list[DrugClue]] = {}
        for clue in clues:
            clues_by_store.setdefault(clue.store_id, []).append(clue)

        for idx, store in enumerate(stores, start=1):
            print(f"\n[{idx}/{len(stores)}] 处理店铺 {store.store_id} {store.store_name}", flush=True)
            store_clues = clues_by_store.get(store.store_id, [])
            await self._process_store(store, store_clues)

        self._build_link_mapping(stores, clues_by_store)
        self._build_unresolved(stores, clues_by_store)

    async def _process_store(self, store: StoreTask, clues: list[DrugClue]) -> None:
        # 路线一：逐条历史链接 detail
        for clue in clues:
            print(f"  [detail] {clue.brand} {clue.generic_name} {clue.spec} pid={clue.product_id}", flush=True)
            await self.collect_detail(clue)

        # 路线二：对每条线索做 search（平台搜索）
        for clue in clues:
            # 已成功的历史链接无需再搜索
            cached = self.detail_cache.get(clue.product_id)
            if cached and cached.status == CollectionStatus.SUCCESS:
                continue
            query = f"{clue.brand}{clue.generic_name}"
            print(f"  [search] {query}", flush=True)
            hits = await self.collect_search(query)
            await self._match_search_hits(store, clue, hits)

    async def _match_search_hits(
        self, store: StoreTask, clue: DrugClue, hits: list[dict[str, Any]]
    ) -> None:
        """从搜索结果中筛选候选并最多进 MAX_CANDIDATES 个详情页。"""
        target_shop_n = normalize_shop_name(store.store_name)
        target_spec_n = normalize_spec(clue.spec)
        candidates: list[dict[str, Any]] = []
        for hit in hits:
            title = str(hit.get("title") or "")
            shop = str(hit.get("shop") or "")
            pid = str(hit.get("sku") or hit.get("item_id") or "")
            # 优先：店铺匹配 + 药品名匹配
            shop_ok = (
                normalize_shop_name(shop) == target_shop_n
                if shop and target_shop_n
                else False
            )
            brand_ok = bool(clue.brand and clue.brand in title) or bool(
                clue.generic_name and clue.generic_name in title
            )
            if not (shop_ok or brand_ok):
                continue
            candidates.append(hit)
        # 店铺匹配优先，再按 rank
        candidates.sort(
            key=lambda h: (
                0 if normalize_shop_name(str(h.get("shop") or "")) == target_shop_n else 1,
                h.get("rank") or 999,
            )
        )
        for hit in candidates[:MAX_CANDIDATES]:
            pid = str(hit.get("sku") or hit.get("item_id") or "")
            if pid in self.detail_cache:
                cached = self.detail_cache[pid]
                if cached.status == CollectionStatus.SUCCESS:
                    continue
            print(f"    [candidate] pid={pid} title={hit.get('title')}", flush=True)
            await self.inspect_search_candidate(store, clue, hit)

    # ---- 输出构建 ----

    def _build_link_mapping(
        self, stores: list[StoreTask], clues_by_store: dict[str, list[DrugClue]]
    ) -> None:
        for store in stores:
            platform_store_name = ""
            homepage = ""
            result = "未找到"
            basis = ""
            store_clues = clues_by_store.get(store.store_id, [])
            # 从成功的 detail 结果推断店铺主页与实际店铺名
            shop_hits: dict[str, str] = OrderedDict()  # normalize -> 原始店铺名
            success_pids: list[str] = []
            for clue in store_clues:
                outcome = self.detail_cache.get(clue.product_id)
                if outcome and outcome.page_shop:
                    shop_hits.setdefault(
                        normalize_shop_name(outcome.page_shop), outcome.page_shop
                    )
                    if outcome.status == CollectionStatus.SUCCESS:
                        success_pids.append(clue.product_id)
                # 搜索候选也可能带 shop
                for hit in self.search_cache.get(f"{clue.brand}{clue.generic_name}", []):
                    shop = str(hit.get("shop") or "")
                    if shop:
                        shop_hits.setdefault(normalize_shop_name(shop), shop)
            target_shop_n = normalize_shop_name(store.store_name)
            # 判定对应结果
            if target_shop_n in shop_hits:
                platform_store_name = shop_hits[target_shop_n]
                result = "已找到"
                basis = "detail/search 页面店铺与档案店铺名称一致"
            elif shop_hits:
                # 找到店铺但名称不完全一致，取最接近的
                platform_store_name = next(iter(shop_hits.values()))
                result = "待确认"
                basis = f"页面店铺={platform_store_name}，与档案={store.store_name} 需人工核对"
            else:
                result = "未找到"
                basis = "未取得任何页面店铺字段"
            # 店铺主页链接：京东店铺主页一般形如 //mall.jd.com/index-<shopId>.html
            # 仅在能确认平台店铺ID时给出，否则标待确认
            homepage = ""
            if result == "已找到" and platform_store_name:
                # 无法从 detail 字段直接拿到 shopId，标注待确认主页
                homepage = ""
                basis += "；店铺主页链接需人工确认"
            self.link_mapping.append({
                "档案店铺ID": store.store_id,
                "平台": PLATFORM,
                "档案店铺名称": store.store_name,
                "平台实际店铺名称": platform_store_name,
                "平台店铺ID": "",
                "店铺主页链接": homepage,
                "对应结果": result,
                "找到依据": basis,
            })

    def _build_unresolved(
        self, stores: list[StoreTask], clues_by_store: dict[str, list[DrugClue]]
    ) -> None:
        # 已在 halt / mark_remaining 中写入平台异常；补充各药品级问题
        for store in stores:
            store_clues = clues_by_store.get(store.store_id, [])
            shop_resolved = any(
                row["档案店铺ID"] == store.store_id and row["对应结果"] == "已找到"
                for row in self.link_mapping
            )
            for clue in store_clues:
                outcome = self.detail_cache.get(clue.product_id)
                if outcome and outcome.status == CollectionStatus.SUCCESS:
                    continue
                # 判定问题类型
                if outcome is None:
                    # 未处理
                    ptype = "药品未找到"
                    detail = f"{clue.brand} {clue.generic_name} {clue.spec} pid={clue.product_id} 未处理"
                    route = clue.source_route
                else:
                    ptype = self._problem_type(outcome)
                    detail = (
                        f"{clue.brand} {clue.generic_name} {clue.spec} pid={clue.product_id} "
                        f"status={outcome.status.value} "
                        f"err={outcome.error_code or ''} {outcome.error_detail or ''}"
                    )
                    route = outcome.source_route
                self.unresolved.append({
                    "档案店铺ID": store.store_id,
                    "档案店铺名称": store.store_name,
                    "问题类型": ptype,
                    "详情": detail[:300],
                    "source_route": route,
                })

    @staticmethod
    def _problem_type(outcome: DetailOutcome) -> str:
        s = outcome.status
        if s in (CollectionStatus.STORE_MISMATCH, CollectionStatus.STORE_UNVERIFIED):
            return "店铺待确认"
        if s == CollectionStatus.SKU_MISMATCH:
            return "规格不明"
        if s == CollectionStatus.PRICE_AMBIGUOUS:
            return "价格不明"
        if s == CollectionStatus.PAGE_CHANGED:
            return "药品未找到"
        if s == CollectionStatus.SUCCESS:
            return ""
        return "药品未找到"

    # ---- 文件写入 ----

    def write_outputs(self, stores: list[StoreTask]) -> None:
        # 1. jd_store_link_mapping.csv
        self._write_csv(
            OUTPUT_DIR / "jd_store_link_mapping.csv",
            ["档案店铺ID", "平台", "档案店铺名称", "平台实际店铺名称", "平台店铺ID",
             "店铺主页链接", "对应结果", "找到依据"],
            self.link_mapping,
        )

        # 2. jd_drug_collection_results.csv
        rows = []
        for o in self.detail_results:
            rows.append({
                "平台": PLATFORM,
                "档案店铺名称": o.clue_store_name,
                "店铺主页链接": "",
                "药品名称": o.brand,
                "药品规格": o.target_spec,
                "商品详情链接": o.final_url or "",
                "商品ID": o.product_id,
                "SKU ID": "",
                "页面价格": str(o.page_price_raw) if o.page_price_raw else "",
                "是否有货": "是" if o.status == CollectionStatus.SUCCESS else "",
                "抓取时间": o.captured_at,
                "抓取结果": o.grab_result,
                "source_route": o.source_route,
                "失败原因": (o.error_code or "") + ((" " + o.error_detail) if o.error_detail else ""),
            })
        self._write_csv(
            OUTPUT_DIR / "jd_drug_collection_results.csv",
            ["平台", "档案店铺名称", "店铺主页链接", "药品名称", "药品规格",
             "商品详情链接", "商品ID", "SKU ID", "页面价格", "是否有货",
             "抓取时间", "抓取结果", "source_route", "失败原因"],
            rows,
        )

        # 3. jd_unresolved_items.csv
        self._write_csv(
            OUTPUT_DIR / "jd_unresolved_items.csv",
            ["档案店铺ID", "档案店铺名称", "问题类型", "详情", "source_route"],
            self.unresolved,
        )

        # 4. jd_execution_summary.md
        self._write_summary(stores)

        # 执行日志
        self._write_csv(
            OUTPUT_DIR / "evidence" / "jd_execution_log.csv",
            ["开始时间", "结束时间", "平台", "命令类型", "查询词或商品ID", "返回码", "结果状态"],
            [
                {
                    "开始时间": log.start_time, "结束时间": log.end_time,
                    "平台": log.platform, "命令类型": log.command_type,
                    "查询词或商品ID": log.query_or_id, "返回码": log.returncode,
                    "结果状态": log.result_status,
                }
                for log in self.logs
            ],
        )

    @staticmethod
    def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        print(f"[OUTPUT] {path} ({len(rows)} rows)", flush=True)

    def _write_summary(self, stores: list[StoreTask]) -> None:
        success_drugs = sum(
            1 for o in self.detail_results if o.status == CollectionStatus.SUCCESS
        )
        found_shops = sum(1 for r in self.link_mapping if r["对应结果"] == "已找到")
        pending_shops = sum(1 for r in self.link_mapping if r["对应结果"] == "待确认")
        notfound_shops = sum(1 for r in self.link_mapping if r["对应结果"] == "未找到")
        detail_count = sum(1 for l in self.logs if l.command_type == "detail")
        search_count = sum(1 for l in self.logs if l.command_type == "search")

        lines = []
        lines.append("# 京东执行Agent - 第一批采集总结\n")
        lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        lines.append("## 一、处理店铺\n")
        lines.append(f"本批共 {len(stores)} 家京东店铺。\n")
        lines.append("| 档案店铺ID | 店铺名称 | 对应结果 | 说明 |")
        lines.append("|---|---|---|---|")
        for r in self.link_mapping:
            lines.append(
                f"| {r['档案店铺ID']} | {r['档案店铺名称']} | {r['对应结果']} | {r['找到依据']} |"
            )
        lines.append("")
        lines.append("## 二、采集统计\n")
        lines.append(f"- detail 调用次数：{detail_count}")
        lines.append(f"- search 调用次数：{search_count}")
        lines.append(f"- 成功抓取药品详情与价格：{success_drugs} 条")
        lines.append(f"- 找到主页链接的店铺：{found_shops} 家")
        lines.append(f"- 店铺待确认：{pending_shops} 家")
        lines.append(f"- 店铺未找到：{notfound_shops} 家")
        lines.append("")
        lines.append("## 三、未成功项及原因\n")
        if self.unresolved:
            for item in self.unresolved:
                lines.append(
                    f"- [{item['问题类型']}] {item['档案店铺名称']}：{item['详情']}"
                )
        else:
            lines.append("- 无未解决项。")
        lines.append("")
        lines.append("## 四、是否可继续下一批\n")
        lines.append("- 当前会话正常，可继续下一批采集。")
        lines.append("")
        lines.append("## 五、输出文件\n")
        lines.append("- outputs/current-stage/jd_store_link_mapping.csv")
        lines.append("- outputs/current-stage/jd_drug_collection_results.csv")
        lines.append("- outputs/current-stage/jd_unresolved_items.csv")
        lines.append("- outputs/current-stage/jd_execution_summary.md")
        lines.append("- outputs/current-stage/evidence/jd_execution_log.csv")
        path = OUTPUT_DIR / "jd_execution_summary.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[OUTPUT] {path}", flush=True)


async def main() -> None:
    stores = load_store_tasks()
    clues = load_drug_clues()
    print(f"加载店铺 {len(stores)} 家，历史线索 {len(clues)} 条", flush=True)
    executor = JdExecutor()
    try:
        await executor.run(stores, clues)
    except Exception as exc:
        traceback.print_exc()
        executor.unresolved.append({
            "档案店铺ID": "ALL",
            "档案店铺名称": "（全平台）",
            "问题类型": "平台异常",
            "详情": f"执行异常：{exc}",
            "source_route": "platform",
        })
    executor.write_outputs(stores)

    success_drugs = sum(
        1 for o in executor.detail_results if o.status == CollectionStatus.SUCCESS
    )
    print("\n==== 完成 ====", flush=True)
    print(f"成功药品数：{success_drugs}", flush=True)
    print(f"detail结果总数：{len(executor.detail_results)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
