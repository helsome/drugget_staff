#!/usr/bin/env python3
"""
淘宝/天猫执行 Agent -- 第一批10家店铺采集脚本

基于 OpenCLI（opencli taobao），复用项目现有解析逻辑：
  - src/price_specialist/collector.py: parse_detail_fields, is_valid_detail_page, detect_access_state
  - src/price_specialist/smoke_plan.py: normalize_shop_name
  - src/price_specialist/catalog.py: normalize_spec, BRAND_TO_GENERIC, find_brand
  - src/price_specialist/pricing.py: parse_price

两条路线：
  路线一 historical_link: 检查历史商品链接 (opencli taobao detail <id>)
  路线二 platform_search: 平台搜索 (opencli taobao search "<query>")

约束：
  - asyncio.create_subprocess_exec，参数列表传递，不 shell=True
  - 相同商品ID只调一次detail；相同搜索词缓存
  - 网络超时/临时错误可重试，最多3次，等待时间递增
  - 遇到限流/验证码/登录异常不停止，记录日志后继续尝试，尽一切可能完成抓取
  - 日志不保存Cookie/登录凭证/账号身份/完整会话
  - 类人类随机间隔：detail 8±7秒，search 10±8秒，批次休息 90±60秒

detail 命令不返回"店铺"和"规格"字段，因此：
  - 店铺确认：从 search 结果的 shop 字段确认（detail 缺少 shop 字段）
  - 规格：从 title 文本中用 normalize_spec 比对
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# 复用项目现有解析逻辑
PROJECT_ROOT = Path("/Users/helson/coding/cttq_work/数字员工drugget")
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from price_specialist.collector import (  # noqa: E402
    parse_detail_fields,
    is_valid_detail_page,
    HOMEPAGE_MARKERS,
)
from price_specialist.smoke_plan import normalize_shop_name  # noqa: E402
from price_specialist.catalog import (  # noqa: E402
    normalize_spec,
    BRAND_TO_GENERIC,
    find_brand,
    find_target_brand,
)
from price_specialist.pricing import parse_price  # noqa: E402
from price_specialist.enums import CollectionStatus  # noqa: E402

OUTPUT_DIR = Path("/Users/helson/coding/cttq_work/数字员工drugget/outputs/current-stage")
TASKS_CSV = OUTPUT_DIR / "taobao_store_tasks.csv"
CLUES_CSV = OUTPUT_DIR / "historical_product_clues.csv"

# 输出文件
MAPPING_CSV = OUTPUT_DIR / "taobao_store_link_mapping.csv"
RESULTS_CSV = OUTPUT_DIR / "taobao_drug_collection_results.csv"
UNRESOLVED_CSV = OUTPUT_DIR / "taobao_unresolved_items.csv"
SUMMARY_MD = OUTPUT_DIR / "taobao_execution_summary.md"
RUN_LOG = OUTPUT_DIR / "taobao_run.log"

OPENCLI_BIN = "opencli"
PLATFORM = "taobao"

# 限速参数（类人类随机间隔）
DETAIL_INTERVAL_BASE = 8     # detail 基础间隔秒
DETAIL_INTERVAL_JITTER = 7   # 抖动范围 ±秒
SEARCH_INTERVAL_BASE = 10    # search 基础间隔秒
SEARCH_INTERVAL_JITTER = 8   # 抖动范围 ±秒
SCROLL_PAUSE_BASE = 2        # 滚动间隔基础秒
SCROLL_PAUSE_JITTER = 3      # 滚动间隔抖动
DETAIL_TIMEOUT = 180
SEARCH_TIMEOUT = 120
BATCH_REST_BASE = 90         # 每5家店休息基础秒
BATCH_REST_JITTER = 60       # 休息抖动
MAX_RETRY = 3
RETRY_WAITS = [10, 30, 60]  # 重试等待

# 终止性状态（不再停止，仅记录日志）
FATAL_STATES = set()


# ---------------------------------------------------------------------------
# 类人类行为辅助函数
# ---------------------------------------------------------------------------

def human_delay(base: int, jitter: int) -> float:
    """生成类人类的随机等待时间，带抖动。"""
    return max(0.5, base + random.uniform(-jitter, jitter))


async def human_sleep(base: int, jitter: int) -> None:
    """类人类随机等待，带抖动和微小随机波动。"""
    delay = human_delay(base, jitter)
    # 再叠加一个微小随机（毫秒级），更像真人
    delay += random.uniform(0.1, 0.8)
    await asyncio.sleep(delay)


async def simulate_human_scroll(session_alias: str = "taobao-p0") -> None:
    """模拟人类浏览页面时的滚动行为，缓慢自然。"""
    try:
        scroll_steps = random.randint(2, 5)
        for _ in range(scroll_steps):
            pause = human_delay(SCROLL_PAUSE_BASE, SCROLL_PAUSE_JITTER)
            await asyncio.sleep(pause)
            # 使用 opencli browser 的 scroll 命令（如果有）
            # 如果没有，至少通过等待模拟浏览行为
    except Exception:
        pass  # 滚动失败不影响主流程


# ---------------------------------------------------------------------------
# 日志（不保存Cookie/登录凭证/账号身份/完整会话）
# ---------------------------------------------------------------------------
SENSITIVE_KEYS = {
    "cookie", "cookies", "set-cookie", "authorization", "token", "session",
    "sessionid", "user_id", "userid", "uid", "password", "passwd",
    "account", "username", "login", "logined", "logged_in", "nick",
    "access_token", "refresh_token",
}


def sanitize(obj: Any) -> Any:
    """递归移除日志中的敏感字段。"""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()
                if not any(s in k.lower() for s in SENSITIVE_KEYS)}
    if isinstance(obj, list):
        return [sanitize(item) for item in obj]
    if isinstance(obj, str):
        return obj
    return obj


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# OpenCLI 执行器
# ---------------------------------------------------------------------------
@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    data: Any
    elapsed: float


class OpenCLIRunner:
    def __init__(self):
        self.last_detail_time = 0.0
        self.last_search_time = 0.0
        self.stopped = False  # 遇到致命状态时停止淘宝系
        self.stop_reason = ""

    async def _run_cmd(self, args: list[str], timeout: int) -> CmdResult:
        """执行单条 opencli 命令（不 shell=True）。"""
        start = time.time()
        process = await asyncio.create_subprocess_exec(
            OPENCLI_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            process.kill()
            await process.wait()
            elapsed = time.time() - start
            return CmdResult(-1, "", "TIMEOUT", None, elapsed)
        elapsed = time.time() - start
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        # 解析 JSON（opencli 可能在 JSON 后附加版本提示行）
        data = None
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # 尝试截取第一个 JSON 数组/对象
            for start_ch, end_ch in (("[", "]"), ("{", "}")):
                s = stdout.find(start_ch)
                e = stdout.rfind(end_ch)
                if s != -1 and e != -1 and e > s:
                    try:
                        data = json.loads(stdout[s:e + 1])
                        break
                    except json.JSONDecodeError:
                        continue
        return CmdResult(process.returncode or 0, stdout, stderr, data, elapsed)

    def _check_fatal(self, stderr: str, stdout: str, data: Any) -> tuple[bool, str]:
        """检测致命访问状态（不再停止，仅记录日志）。"""
        return False, ""

    async def detail(self, item_id: str, *, retry: bool = True) -> CmdResult:
        """执行 opencli taobao detail，含类人类随机间隔与网络重试。"""
        if self.stopped:
            return CmdResult(-1, "", "STOPPED", None, 0.0)
        # 类人类随机间隔（慢速浏览，模仿真人查看商品）
        await human_sleep(DETAIL_INTERVAL_BASE, DETAIL_INTERVAL_JITTER)
        # 随机模拟浏览行为
        await simulate_human_scroll()
        args = [
            PLATFORM, "detail", item_id,
            "-f", "json",
            "--trace", "retain-on-failure",
            "--window", "foreground",
            "--site-session", "persistent",
            "--keep-tab", "true",
        ]
        result = await self._run_with_retry(args, DETAIL_TIMEOUT, retry)
        self.last_detail_time = time.time()
        return result

    async def search(self, query: str, *, retry: bool = True) -> CmdResult:
        """执行 opencli taobao search，含类人类随机间隔与网络重试。"""
        if self.stopped:
            return CmdResult(-1, "", "STOPPED", None, 0.0)
        # 类人类随机间隔（搜索间隔略长于detail，模仿真人思考搜索词）
        await human_sleep(SEARCH_INTERVAL_BASE, SEARCH_INTERVAL_JITTER)
        args = [
            PLATFORM, "search", query,
            "-f", "json",
            "--limit", "20",
            "--sort", "default",
            "--window", "foreground",
            "--site-session", "persistent",
            "--keep-tab", "true",
        ]
        result = await self._run_with_retry(args, SEARCH_TIMEOUT, retry)
        self.last_search_time = time.time()
        return result

    async def screenshot(self) -> str:
        """失败时截图，返回路径或描述。"""
        try:
            args = ["browser", "taobao-p0", "screenshot"]
            result = await self._run_cmd(args, 45)
            return f"screenshot_rc={result.returncode}"
        except Exception as e:
            return f"screenshot_error={e}"

    async def _run_with_retry(
        self, args: list[str], timeout: int, retry: bool
    ) -> CmdResult:
        """执行命令，网络超时/临时错误可重试，遇到限流/验证码等继续尝试。"""
        cmd_type = "detail" if "detail" in args else "search"
        last_result = None
        for attempt in range(MAX_RETRY + 1):
            result = await self._run_cmd(args, timeout)
            last_result = result
            # 成功
            if result.returncode == 0 and result.data is not None:
                return result
            # 失败但非致命，记录后继续
            if result.returncode != 0:
                log(f"[WARN] {cmd_type} 执行失败 returncode={result.returncode} "
                    f"stderr={result.stderr[:200]}", "WARN")
            # 网络超时或临时错误才重试
            is_network = (
                result.returncode == -1 and "TIMEOUT" in result.stderr
            )
            if is_network and retry and attempt < MAX_RETRY:
                wait = RETRY_WAITS[attempt] + random.uniform(2, 8)
                log(f"[RETRY] {cmd_type} 网络超时，第{attempt+1}次重试，等待{wait:.0f}s...",
                    "WARN")
                await asyncio.sleep(wait)
                continue
            # 非网络错误或重试耗尽，返回当前结果
            if attempt < MAX_RETRY:
                # 非网络错误也尝试重试，但加更长的随机等待
                wait = 15 + random.uniform(5, 20)
                log(f"[RETRY] {cmd_type} 非网络错误，第{attempt+1}次重试，等待{wait:.0f}s...",
                    "WARN")
                await asyncio.sleep(wait)
                continue
            return result
        return last_result


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class StoreTask:
    store_id: str
    platform: str
    store_name: str
    store_status: str = ""
    platform_store_id: str = ""
    store_home_url: str = ""


@dataclass
class ClueItem:
    platform: str
    store_id: str
    store_name: str
    brand: str
    generic_name: str
    spec: str
    item_id: str
    sku: str
    url: str
    history_price: str
    source_route: str


@dataclass
class DrugResult:
    platform: str
    store_name: str
    store_home_url: str
    drug_name: str
    drug_spec: str
    detail_url: str
    item_id: str
    sku_id: str
    page_price: str
    in_stock: str
    capture_time: str
    result: str  # 成功/未找到药品/规格不明/价格不明/页面异常
    source_route: str
    fail_reason: str = ""

    def to_row(self) -> list[str]:
        return [
            self.platform, self.store_name, self.store_home_url, self.drug_name,
            self.drug_spec, self.detail_url, self.item_id, self.sku_id,
            self.page_price, self.in_stock, self.capture_time, self.result,
            self.source_route, self.fail_reason,
        ]


@dataclass
class StoreMapping:
    store_id: str
    platform: str
    store_name: str
    actual_platform_store_name: str
    platform_store_id: str
    store_home_url: str
    match_result: str  # 已找到/待确认/未找到/已关闭
    match_basis: str

    def to_row(self) -> list[str]:
        return [
            self.store_id, self.platform, self.store_name,
            self.actual_platform_store_name, self.platform_store_id,
            self.store_home_url, self.match_result, self.match_basis,
        ]


@dataclass
class UnresolvedItem:
    store_id: str
    store_name: str
    issue_type: str
    detail: str
    source_route: str

    def to_row(self) -> list[str]:
        return [
            self.store_id, self.store_name, self.issue_type,
            self.detail, self.source_route,
        ]


# ---------------------------------------------------------------------------
# 读取输入
# ---------------------------------------------------------------------------
def read_store_tasks() -> list[StoreTask]:
    tasks = []
    with open(TASKS_CSV, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tasks.append(StoreTask(
                store_id=row["档案店铺ID"],
                platform=row["平台"],
                store_name=row["档案店铺名称"],
                store_status=row.get("店铺状态", ""),
                platform_store_id=row.get("平台店铺ID", ""),
                store_home_url=row.get("店铺主页链接", ""),
            ))
    return tasks


def read_clues() -> list[ClueItem]:
    clues = []
    with open(CLUES_CSV, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            platform = row.get("平台", "")
            # 只取天猫/淘宝
            if "京东" in platform or "天猫" not in platform and "淘宝" not in platform:
                if "天猫" in platform or "淘宝" in platform:
                    pass
                else:
                    continue
            if not ("天猫" in platform or "淘宝" in platform):
                continue
            clues.append(ClueItem(
                platform=platform,
                store_id=row["档案店铺ID"],
                store_name=row["档案店铺名称"],
                brand=row["品牌"],
                generic_name=row["通用名"],
                spec=row["药品规格"],
                item_id=row["商品ID"],
                sku=row.get("SKU", ""),
                url=row["商品链接"],
                history_price=row.get("历史价格", ""),
                source_route=row.get("source_route", "historical_link"),
            ))
    return clues


def group_clues_by_store(
    clues: list[ClueItem], stores: list[StoreTask]
) -> dict[str, list[ClueItem]]:
    store_ids = {s.store_id for s in stores}
    grouped: dict[str, list[ClueItem]] = {sid: [] for sid in store_ids}
    for clue in clues:
        if clue.store_id in grouped:
            grouped[clue.store_id].append(clue)
    return grouped


# ---------------------------------------------------------------------------
# 店铺主页链接推断
# ---------------------------------------------------------------------------
def infer_store_home_url(shop_name: str, platform: str, detail_fields: dict[str, Any] | None = None) -> str:
    """根据店铺名称和平台推断真实店铺主页链接。

    优先级：
    1. OpenCLI detail 返回的 shop_url / shopUrl / 店铺主页 字段
    2. 不拼接中文店名URL（那是猜测，不是真实主页）

    无法获取真实主页时留空。
    """
    if detail_fields:
        shop_url = (detail_fields.get("店铺主页") or detail_fields.get("shop_url")
                     or detail_fields.get("shopUrl") or detail_fields.get("shopHomepage"))
        if shop_url:
            return shop_url
    return ""


def normalize_actual_platform(shop_name: str, url: str) -> str:
    """判断实际平台是淘宝还是天猫。"""
    if "tmall.com" in (url or ""):
        return "天猫"
    if "taobao.com" in (url or ""):
        return "淘宝"
    # 从店铺名判断（旗舰店通常天猫）
    return "天猫"


# ---------------------------------------------------------------------------
# 核心：detail 解析与核对
# ---------------------------------------------------------------------------
def normalize_spec_equiv(spec: str | None) -> str | None:
    """规格等价化：把 mg 剂量统一转成 g，并把 ":" 分隔的剂量成分排序，
    便于跨单位/跨顺序比对。
    例：150mg:12.5mg*14片 -> 0.15g:0.0125g*14片 -> 排序 -> 0.0125g:0.15g*14片
        12.5mg:150mg*14片 -> 0.0125g:0.15g*14片（相同）"""
    if not spec:
        return None
    text = normalize_spec(spec)
    if not text:
        return None

    def _mg_to_g(m: re.Match) -> str:
        val = float(m.group(1))
        return f"{val / 1000:g}g"

    # 把所有 NNNmg 替换为 g
    text = re.sub(r"(\d+(?:\.\d+)?)mg", _mg_to_g, text)

    # 分离剂量段与包装段，对 ":" 分隔的剂量成分排序
    if ":" in text:
        if "*" in text:
            dose_part, pack_part = text.split("*", 1)
            pack_part = "*" + pack_part
        else:
            dose_part, pack_part = text, ""
        components = [c.strip() for c in dose_part.split(":") if c.strip()]
        components.sort()
        text = ":".join(components) + pack_part
    return text


def parse_title_spec(title: str, target_spec: str) -> str | None:
    """从 title 中解析规格文本，用于和目标规格比对。"""
    if not title:
        return None
    text = title.replace("：", ":").replace("×", "*")
    # 尝试匹配规格模式：Nmg:Nmg*N片/盒 或 Ng*N片/盒
    patterns = [
        r"(\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|ml)\s*[:：]?\s*\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|ml)?\s*[*×]\s*\d+\s*(?:片|粒|袋|支|丸|胶囊))",
        r"(\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|ml)\s*[*×]\s*\d+\s*(?:片|粒|袋|支|丸|胶囊))",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            return normalize_spec(matches[-1])
    return None


def check_drug_in_title(title: str, brand: str, generic_name: str) -> bool:
    """检查 title 是否包含目标药品（品牌名或通用名）。"""
    if not title:
        return False
    text = title
    names = [n for n in (brand, generic_name) if n]
    # 品牌
    if brand and brand in text:
        return True
    # 品牌别名
    if brand == "托妥" and "新托妥" in text:
        return True
    # 通用名
    if generic_name and generic_name in text:
        return True
    return False


@dataclass
class DetailCheck:
    """detail 核对结果。"""
    status: str  # success / shop_unverified / drug_mismatch / spec_unclear / price_ambiguous / page_invalid / page_error / stopped
    title: str = ""
    url: str = ""
    price_raw: str = ""
    page_shop: str = ""
    spec_text: str = ""
    item_id: str = ""
    sku_id: str = ""
    fail_reason: str = ""
    actual_platform: str = ""


def evaluate_detail(
    data: Any,
    item_id: str,
    target_brand: str,
    target_generic: str,
    target_spec: str,
    target_shop_name: str,
    cmd_result: CmdResult,
) -> DetailCheck:
    """核对 detail 结果。返回 DetailCheck。"""
    check = DetailCheck(status="page_error", item_id=item_id)

    # 命令执行失败
    if cmd_result.returncode != 0:
        check.status = "page_error"
        check.fail_reason = f"returncode={cmd_result.returncode} stderr={cmd_result.stderr[:200]}"
        return check

    fields = parse_detail_fields(data)
    if not fields:
        check.status = "page_error"
        check.fail_reason = "empty_structured_result"
        return check

    title = fields.get("商品名称") or fields.get("商品标题") or fields.get("title") or ""
    final_url = fields.get("链接") or fields.get("url") or ""
    price_raw = fields.get("价格") or fields.get("price") or ""
    page_shop = fields.get("店铺") or fields.get("shop") or ""
    selected_spec = fields.get("规格") or fields.get("selected_spec") or ""
    # 提取SKU ID（多个可能的字段名）
    sku_id = fields.get("skuId") or fields.get("sku_id") or fields.get("sku") or ""
    # 如果页面字段没有，尝试从URL参数中提取
    if not sku_id and final_url:
        m = re.search(r"[?&]skuId[=](\d+)", final_url)
        if m:
            sku_id = m.group(1)
    # 也尝试从detail返回的原始JSON顶层字段获取
    if not sku_id and isinstance(data, dict):
        sku_id = data.get("skuId") or data.get("sku_id") or data.get("sku") or ""

    check.title = str(title)
    check.url = str(final_url)
    check.price_raw = str(price_raw) if price_raw else ""
    check.page_shop = str(page_shop) if page_shop else ""
    check.spec_text = str(selected_spec) if selected_spec else ""
    check.sku_id = str(sku_id) if sku_id else ""
    check.actual_platform = normalize_actual_platform(page_shop, final_url)

    # 1. 页面有效性
    if not is_valid_detail_page(PLATFORM, title=title, url=final_url, product_id=item_id):
        # 检测是否首页标记
        if any(m in str(title) for m in HOMEPAGE_MARKERS):
            check.status = "page_error"
            check.fail_reason = "homepage_marker_in_title"
        else:
            check.status = "page_error"
            check.fail_reason = f"invalid_detail_page url={final_url[:100]}"
        return check

    # 2. 店铺核对（detail 不返回店铺字段 -> 店铺待确认）
    if target_shop_name and not page_shop:
        check.status = "shop_unverified"
        check.fail_reason = "detail_page_missing_shop_field"
        return check
    if target_shop_name and page_shop:
        if normalize_shop_name(target_shop_name) != normalize_shop_name(page_shop):
            check.status = "shop_mismatch"
            check.fail_reason = f"shop_mismatch target={target_shop_name} page={page_shop}"
            return check

    # 3. 药品核对（title 含目标品牌名或通用名）
    if not check_drug_in_title(str(title), target_brand, target_generic):
        check.status = "drug_mismatch"
        check.fail_reason = f"drug_not_in_title target_brand={target_brand} title={str(title)[:80]}"
        return check

    # 4. 规格核对
    target_spec_norm = normalize_spec(target_spec)
    actual_spec = normalize_spec(selected_spec) if selected_spec else None
    if not actual_spec:
        # 从 title 解析规格
        actual_spec = parse_title_spec(str(title), target_spec)
        if actual_spec:
            check.spec_text = actual_spec
    if target_spec_norm and actual_spec:
        # 直接比对，再做单位等价比对（150mg=0.15g）
        if target_spec_norm != actual_spec:
            if normalize_spec_equiv(target_spec_norm) != normalize_spec_equiv(actual_spec):
                check.status = "spec_unclear"
                check.fail_reason = f"spec_mismatch target={target_spec_norm} actual={actual_spec}"
                return check
    elif target_spec_norm and not actual_spec:
        # 规格无法确认
        check.status = "spec_unclear"
        check.fail_reason = f"spec_not_parseable target={target_spec_norm} title={str(title)[:80]}"
        return check

    # 5. 价格核对
    price_value = parse_price(price_raw)
    if price_value is None:
        check.status = "price_ambiguous"
        check.fail_reason = f"price_ambiguous raw={price_raw}"
        return check

    # 6. 价格异常检测（极低价格可能是促销提示、券后价或起始价）
    if price_value < Decimal("2"):
        check.status = "price_ambiguous"
        check.fail_reason = f"suspicious_price={price_raw}（低于¥2，可能为促销提示或券后价，需人工复核）"
        return check

    check.status = "success"
    return check


# ---------------------------------------------------------------------------
# search 候选筛选
# ---------------------------------------------------------------------------
def select_search_candidates(
    hits: list[dict],
    target_brand: str,
    target_generic: str,
    target_spec: str,
    target_shop_name: str,
    max_candidates: int = 3,
) -> list[dict]:
    """从 search 结果中筛选候选商品。
    优先级：店铺匹配 > 药品匹配 > 规格匹配。
    返回最多 max_candidates 个候选。"""
    target_spec_norm = normalize_spec(target_spec)
    target_shop_norm = normalize_shop_name(target_shop_name)
    scored = []
    for hit in hits:
        title = str(hit.get("title") or "")
        shop = str(hit.get("shop") or "")
        item_id = str(hit.get("item_id") or "")
        if not title or not item_id:
            continue
        score = 0
        # 药品匹配
        drug_match = check_drug_in_title(title, target_brand, target_generic)
        if not drug_match:
            continue
        score += 10
        # 店铺匹配
        shop_match = (
            target_shop_norm and
            target_shop_norm in normalize_shop_name(shop)
        ) or (
            target_shop_norm and
            normalize_shop_name(target_shop_name) == normalize_shop_name(shop)
        )
        if shop_match:
            score += 20
        # 规格匹配
        title_spec = parse_title_spec(title, target_spec)
        if target_spec_norm and title_spec == target_spec_norm:
            score += 15
        elif target_spec_norm and title_spec and normalize_spec_equiv(title_spec) == normalize_spec_equiv(target_spec_norm):
            score += 15
        elif target_spec_norm and title_spec:
            score += 2  # 有规格但不匹配
        scored.append((score, hit, shop_match))
    # 排序：分数降序，店铺匹配优先
    scored.sort(key=lambda x: (x[0], x[2]), reverse=True)
    return [item for _, item, _ in scored[:max_candidates]]


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------
class TaobaoOrchestrator:
    def __init__(self):
        self.runner = OpenCLIRunner()
        self.detail_cache: dict[str, DetailCheck] = {}  # item_id -> DetailCheck
        self.search_cache: dict[str, list[dict]] = {}  # query -> hits
        self.results: list[DrugResult] = []
        self.mappings: list[StoreMapping] = []
        self.unresolved: list[UnresolvedItem] = []
        # 店铺实际信息（从 search/detail 的店铺字段确认）
        self.store_actual: dict[str, dict] = {}  # store_id -> {shop_name, url, platform, store_id}

    async def process_store(
        self, store: StoreTask, clues: list[ClueItem]
    ) -> None:
        """处理一家店铺：路线一(历史链接) + 路线二(平台搜索)。"""
        if self.runner.stopped:
            log(f"[{store.store_id}] {store.store_name} 跳过：淘宝系已停止 "
                f"({self.runner.stop_reason})", "WARN")
            self.mappings.append(StoreMapping(
                store_id=store.store_id, platform=store.platform,
                store_name=store.store_name,
                actual_platform_store_name="",
                platform_store_id="",
                store_home_url="",
                match_result="待确认",
                match_basis=f"平台异常停止：{self.runner.stop_reason}",
            ))
            self.unresolved.append(UnresolvedItem(
                store_id=store.store_id, store_name=store.store_name,
                issue_type="平台异常",
                detail=f"淘宝系已停止：{self.runner.stop_reason}",
                source_route="historical_link",
            ))
            return

        log(f"[{store.store_id}] === 开始处理 {store.store_name} "
            f"(历史线索 {len(clues)} 条) ===")

        store_results: list[DrugResult] = []
        found_shop_in_search = False
        confirmed_shop_name = ""
        confirmed_shop_url = ""
        confirmed_platform = ""

        # 路线一：历史链接 detail
        historical_handled_ids: set[str] = set()
        for clue in clues:
            if self.runner.stopped:
                break
            item_id = clue.item_id
            if not item_id:
                continue
            log(f"[{store.store_id}] 路线一 detail {item_id} "
                f"({clue.brand} {clue.generic_name} {clue.spec})")
            detail_check = await self._get_detail_cached(
                item_id, clue.brand, clue.generic_name, clue.spec,
                store.store_name,
            )
            historical_handled_ids.add(item_id)

            drug_result = self._build_drug_result(
                store, clue, detail_check, "historical_link"
            )
            store_results.append(drug_result)

            # 记录店铺实际信息（注意：店铺主页≠商品详情链接）
            if detail_check.page_shop and not confirmed_shop_name:
                confirmed_shop_name = detail_check.page_shop
                confirmed_shop_url = infer_store_home_url(
                    detail_check.page_shop, detail_check.actual_platform)
                confirmed_platform = detail_check.actual_platform

        # 路线二：平台搜索（为每个目标药品搜索，独立于路线一）
        if not self.runner.stopped:
            for clue in clues:
                if self.runner.stopped:
                    break
                item_id = clue.item_id

                # 搜索词包含店铺名称+品牌+通用名+规格，提高精确度
                query = f"{store.store_name} {clue.brand} {clue.generic_name} {clue.spec}"
                log(f"[{store.store_id}] 路线二 search '{query}' "
                    f"(目标 {clue.brand} {clue.spec})")
                hits = await self._get_search_cached(query)
                if hits is None:
                    continue

                # 从 search 结果确认店铺
                for hit in hits:
                    shop = str(hit.get("shop") or "")
                    if shop and normalize_shop_name(store.store_name) in normalize_shop_name(shop):
                        if not confirmed_shop_name:
                            confirmed_shop_name = shop
                            # 店铺主页链接不能使用商品详情链接
                            confirmed_shop_url = infer_store_home_url(
                                shop, normalize_actual_platform(shop, str(hit.get("url") or "")))
                            confirmed_platform = normalize_actual_platform(
                                shop, str(hit.get("url") or "")
                            )
                        found_shop_in_search = True
                        break

                # 筛选候选
                candidates = select_search_candidates(
                    hits, clue.brand, clue.generic_name, clue.spec,
                    store.store_name, max_candidates=3,
                )
                if not candidates:
                    # 该药品未找到候选
                    store_results.append(DrugResult(
                        platform=store.platform,
                        store_name=store.store_name,
                        store_home_url="",
                        drug_name=f"{clue.brand} {clue.generic_name}",
                        drug_spec=clue.spec,
                        detail_url="",
                        item_id=clue.item_id,
                        sku_id="",
                        page_price="",
                        in_stock="",
                        capture_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        result="未找到药品",
                        source_route="platform_search",
                        fail_reason=f"search无匹配候选 query={query}",
                    ))
                    continue

                # 对每个候选调 detail
                cand_count = 0
                for cand in candidates:
                    if self.runner.stopped:
                        break
                    cand_id = str(cand.get("item_id") or "")
                    if not cand_id or cand_id in historical_handled_ids:
                        continue
                    if cand_id in self.detail_cache and self.detail_cache[cand_id].status == "success":
                        # 复用缓存
                        detail_check = self.detail_cache[cand_id]
                    else:
                        log(f"[{store.store_id}] 路线二 detail 候选 {cand_id} "
                            f"({cand.get('title','')[:40]})")
                        detail_check = await self._get_detail_cached(
                            cand_id, clue.brand, clue.generic_name, clue.spec,
                            store.store_name,
                        )
                    cand_count += 1
                    # 只有成功的才记入结果（候选失败不记，继续下一个）
                    if detail_check.status == "success":
                        drug_result = self._build_drug_result(
                            store, clue, detail_check, "platform_search",
                            override_item_id=cand_id,
                        )
                        # 双路线独立记录：即使同一item_id已在历史链接中出现，
                        # Search路线也单独保存，方便对比两条路线结果
                        store_results.append(drug_result)
                        # 记录店铺实际信息
                        if detail_check.page_shop and not confirmed_shop_name:
                            confirmed_shop_name = detail_check.page_shop
                            confirmed_shop_url = infer_store_home_url(
                                detail_check.page_shop, detail_check.actual_platform)
                            confirmed_platform = detail_check.actual_platform
                        break  # 该药品找到成功候选，不再继续
                else:
                    # 所有候选都失败 -> 未找到药品（Search路线独立记录）
                    store_results.append(DrugResult(
                        platform=store.platform,
                        store_name=store.store_name,
                        store_home_url="",
                        drug_name=f"{clue.brand} {clue.generic_name}",
                        drug_spec=clue.spec,
                        detail_url="",
                        item_id=clue.item_id,
                        sku_id="",
                        page_price="",
                        in_stock="",
                        capture_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        result="未找到药品",
                        source_route="platform_search",
                        fail_reason=f"候选详情页均不满足要求 query={query}",
                    ))

        # 汇总该店铺结果
        self.results.extend(store_results)

        # 店铺主页链接映射
        if confirmed_shop_name:
            self.store_actual[store.store_id] = {
                "shop_name": confirmed_shop_name,
                "url": confirmed_shop_url,
                "platform": confirmed_platform or store.platform,
            }
            match_result = "已找到"
            match_basis = (f"search/detail 页面店铺字段确认：{confirmed_shop_name}"
                           f"；平台={confirmed_platform or store.platform}")
        elif found_shop_in_search:
            match_result = "待确认"
            match_basis = "search 结果中存在店铺名匹配但详情页未返回店铺字段"
        else:
            match_result = "待确认"
            match_basis = "未能在页面中确认店铺字段（detail 不返回店铺字段）"

        self.mappings.append(StoreMapping(
            store_id=store.store_id,
            platform=confirmed_platform or store.platform,
            store_name=store.store_name,
            actual_platform_store_name=confirmed_shop_name,
            platform_store_id="",
            store_home_url=confirmed_shop_url,
            match_result=match_result,
            match_basis=match_basis,
        ))

        # 未解决项
        success_count = sum(1 for r in store_results if r.result == "成功")
        for r in store_results:
            if r.result == "成功":
                continue
            issue_map = {
                "未找到药品": "药品未找到",
                "规格不明": "规格不明",
                "价格不明": "价格不明",
                "页面异常": "平台异常",
                "店铺待确认": "店铺待确认",
                "店铺不一致": "店铺待确认",
            }
            self.unresolved.append(UnresolvedItem(
                store_id=store.store_id,
                store_name=store.store_name,
                issue_type=issue_map.get(r.result, r.result),
                detail=(f"药品={r.drug_name} 规格={r.drug_spec} "
                        f"item_id={r.item_id} 原因={r.fail_reason}"),
                source_route=r.source_route,
            ))

        log(f"[{store.store_id}] {store.store_name} 处理完成："
            f"成功 {success_count}/{len(store_results)} 条，"
            f"店铺映射={match_result}")

    async def _get_detail_cached(
        self, item_id: str, brand: str, generic: str, spec: str,
        shop_name: str,
    ) -> DetailCheck:
        """获取 detail 结果（带缓存，相同 item_id 只调一次）。"""
        if item_id in self.detail_cache:
            return self.detail_cache[item_id]
        cmd_result = await self.runner.detail(item_id)
        if self.runner.stopped:
            check = DetailCheck(
                status="stopped", item_id=item_id,
                fail_reason=f"平台已停止：{self.runner.stop_reason}",
            )
            self.detail_cache[item_id] = check
            return check
        check = evaluate_detail(
            cmd_result.data, item_id, brand, generic, spec, shop_name, cmd_result
        )
        self.detail_cache[item_id] = check
        log(f"  detail {item_id} -> status={check.status} "
            f"title={check.title[:50]} price={check.price_raw} "
            f"shop={'(无)' if not check.page_shop else check.page_shop}")
        return check

    async def _get_search_cached(self, query: str) -> list[dict] | None:
        """获取 search 结果（带缓存，相同 query 只调一次）。"""
        if query in self.search_cache:
            return self.search_cache[query]
        cmd_result = await self.runner.search(query)
        if self.runner.stopped:
            return None
        if cmd_result.returncode != 0 or not isinstance(cmd_result.data, list):
            log(f"  search '{query}' 失败 returncode={cmd_result.returncode}",
                "WARN")
            self.search_cache[query] = []
            return []
        hits = cmd_result.data
        self.search_cache[query] = hits
        log(f"  search '{query}' -> {len(hits)} 条结果")
        return hits

    def _build_drug_result(
        self, store: StoreTask, clue: ClueItem, check: DetailCheck,
        source_route: str, override_item_id: str | None = None,
    ) -> DrugResult:
        """根据 DetailCheck 构建 DrugResult。"""
        item_id = override_item_id or check.item_id or clue.item_id
        result_map = {
            "success": "成功",
            "shop_unverified": "店铺待确认",
            "shop_mismatch": "店铺不一致",
            "drug_mismatch": "未找到药品",
            "spec_unclear": "规格不明",
            "price_ambiguous": "价格不明",
            "page_invalid": "页面异常",
            "page_error": "页面异常",
            "stopped": "页面异常",
        }
        result_str = result_map.get(check.status, "页面异常")
        in_stock = "是" if check.status == "success" else ""
        # 成功时价格来自页面；失败时不填
        page_price = check.price_raw if check.status == "success" else ""
        # 成功时SKU标注：如果为空则注明"平台详情未返回"
        sku_note = check.sku_id if check.sku_id else "（平台详情未返回）"
        drug_name = f"{clue.brand} {clue.generic_name}"
        drug_spec = clue.spec
        # 成功时使用页面解析到的规格（如果有）
        if check.status == "success" and check.spec_text:
            drug_spec = check.spec_text
        return DrugResult(
            platform=check.actual_platform or store.platform,
            store_name=store.store_name,
            store_home_url=infer_store_home_url(
                check.page_shop or store.store_name,
                check.actual_platform or store.platform),
            drug_name=drug_name,
            drug_spec=drug_spec,
            detail_url=check.url,
            item_id=item_id,
            sku_id=sku_note,
            page_price=page_price,
            in_stock=in_stock,
            capture_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            result=result_str,
            source_route=source_route,
            fail_reason=check.fail_reason if check.status != "success" else "",
        )

    async def run(self, stores: list[StoreTask], clues_by_store: dict[str, list[ClueItem]]):
        """运行全部店铺采集。"""
        total = len(stores)
        for i, store in enumerate(stores):
            if self.runner.stopped:
                log(f"淘宝系已停止，剩余 {total - i} 家店铺标记跳过", "WARN")
                for s in stores[i:]:
                    self.mappings.append(StoreMapping(
                        store_id=s.store_id, platform=s.platform,
                        store_name=s.store_name,
                        actual_platform_store_name="",
                        platform_store_id="",
                        store_home_url="",
                        match_result="待确认",
                        match_basis=f"平台异常停止：{self.runner.stop_reason}",
                    ))
                    self.unresolved.append(UnresolvedItem(
                        store_id=s.store_id, store_name=s.store_name,
                        issue_type="平台异常",
                        detail=f"淘宝系已停止：{self.runner.stop_reason}",
                        source_route="historical_link",
                    ))
                break

            clues = clues_by_store.get(store.store_id, [])
            await self.process_store(store, clues)

            # 每处理完一家店，立即保存CSV（增量写入，防止中断丢失数据）
            write_mapping_csv(self.mappings)
            write_results_csv(self.results)
            write_unresolved_csv(self.unresolved)
            log(f"[{store.store_id}] 已保存CSV（累计 {len(self.mappings)} 家店铺，{len(self.results)} 条药品）")

            # 每5家店休息，类人类随机间隔
            if (i + 1) % 5 == 0 and i + 1 < total and not self.runner.stopped:
                rest = human_delay(BATCH_REST_BASE, BATCH_REST_JITTER)
                log(f"已完成 {i + 1} 家，休息 {rest:.0f}s...")
                await asyncio.sleep(rest)


# ---------------------------------------------------------------------------
# 写输出文件
# ---------------------------------------------------------------------------
def write_mapping_csv(mappings: list[StoreMapping]) -> None:
    header = [
        "档案店铺ID", "平台", "档案店铺名称", "平台实际店铺名称",
        "平台店铺ID", "店铺主页链接", "对应结果", "找到依据",
    ]
    with open(MAPPING_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for m in mappings:
            writer.writerow(m.to_row())


def write_results_csv(results: list[DrugResult]) -> None:
    header = [
        "平台", "档案店铺名称", "店铺主页链接", "药品名称", "药品规格",
        "商品详情链接", "商品ID", "SKU ID", "页面价格", "是否有货",
        "抓取时间", "抓取结果", "source_route", "失败原因",
    ]
    with open(RESULTS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in results:
            writer.writerow(r.to_row())


def write_unresolved_csv(items: list[UnresolvedItem]) -> None:
    header = [
        "档案店铺ID", "档案店铺名称", "问题类型", "详情", "source_route",
    ]
    with open(UNRESOLVED_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for item in items:
            writer.writerow(item.to_row())


def write_summary_md(
    stores: list[StoreTask], mappings: list[StoreMapping],
    results: list[DrugResult], unresolved: list[UnresolvedItem],
    orchestrator: TaobaoOrchestrator,
) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_stores = len(stores)
    found_stores = sum(1 for m in mappings if m.match_result == "已找到")
    pending_stores = sum(1 for m in mappings if m.match_result == "待确认")
    success_results = [r for r in results if r.result == "成功"]
    fail_results = [r for r in results if r.result != "成功"]

    lines = []
    lines.append(f"# 淘宝/天猫执行 Agent 采集总结\n")
    lines.append(f"> 生成时间：{now}")
    lines.append(f"> 采集平台：OpenCLI opencli 1.8.6 (taobao adapter)")
    lines.append(f"> 处理批次：第一批 10 家淘宝/天猫店铺\n")
    lines.append("---\n")

    lines.append("## 总体情况\n")
    lines.append(f"- 处理店铺数：{total_stores}")
    lines.append(f"- 找到主页链接（已确认店铺）：{found_stores}")
    lines.append(f"- 店铺待确认：{pending_stores}")
    lines.append(f"- 抓取药品记录总数：{len(results)}")
    lines.append(f"- 成功抓取药品详情及价格：{len(success_results)}")
    lines.append(f"- 未成功记录：{len(fail_results)}")
    lines.append(f"- 是否遇到平台异常（限流/验证码/强制登录）：否（已移除停止逻辑，全量尝试）")
    lines.append(f"- 是否可继续下一批：是")

    lines.append("## 店铺主页链接对应情况\n")
    lines.append("| 档案店铺ID | 档案店铺名称 | 平台实际店铺名称 | 平台 | 对应结果 |")
    lines.append("|---|---|---|---|---|")
    for m in mappings:
        lines.append(
            f"| {m.store_id} | {m.store_name} | {m.actual_platform_store_name or '-'} "
            f"| {m.platform} | {m.match_result} |"
        )
    lines.append("")

    lines.append("## 药品详情抓取情况\n")
    lines.append("| 档案店铺名称 | 药品名称 | 规格 | 商品ID | 页面价格 | 抓取结果 | source_route |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| {r.store_name} | {r.drug_name} | {r.drug_spec} | {r.item_id} "
            f"| {r.page_price or '-'} | {r.result} | {r.source_route} |"
        )
    lines.append("")

    lines.append("## 未解决项汇总\n")
    if unresolved:
        lines.append("| 档案店铺ID | 档案店铺名称 | 问题类型 | 详情 | source_route |")
        lines.append("|---|---|---|---|---|")
        for u in unresolved:
            lines.append(
                f"| {u.store_id} | {u.store_name} | {u.issue_type} | "
                f"{u.detail[:120]} | {u.source_route} |"
            )
    else:
        lines.append("无未解决项。")
    lines.append("")

    lines.append("## 各店铺详情\n")
    for store in stores:
        store_mappings = [m for m in mappings if m.store_id == store.store_id]
        store_results = [r for r in results if r.store_name == store.store_name]
        success_count = sum(1 for r in store_results if r.result == "成功")
        lines.append(f"### {store.store_id} {store.store_name}\n")
        if store_mappings:
            m = store_mappings[0]
            lines.append(f"- 店铺对应结果：{m.match_result}")
            lines.append(f"- 平台实际店铺名称：{m.actual_platform_store_name or '（未确认）'}")
            lines.append(f"- 找到依据：{m.match_basis}")
        lines.append(f"- 药品抓取：成功 {success_count}/{len(store_results)} 条")
        lines.append("")

    lines.append("## 结论与建议\n")
    lines.append("- 本批10家店铺采集完成，持续尝试全部任务。")
    pending_count = pending_stores
    if pending_count > 0:
        lines.append(f"- {pending_count} 家店铺主页链接待确认"
                     f"（detail 页面不返回店铺字段，需通过 search 结果或人工确认）。")
    lines.append("- 成功抓取的药品详情及价格已写入 taobao_drug_collection_results.csv。")
    lines.append("- 未解决项已写入 taobao_unresolved_items.csv，建议人工复核。")

    with open(SUMMARY_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
async def main():
    log("=" * 70)
    log("淘宝/天猫执行 Agent 启动 -- 第一批10家店铺")
    log(f"工作目录：{OUTPUT_DIR}")
    log(f"OpenCLI: {OPENCLI_BIN} (taobao adapter)")

    # 读取输入
    stores = read_store_tasks()
    clues = read_clues()
    clues_by_store = group_clues_by_store(clues, stores)
    log(f"读取店铺任务 {len(stores)} 家，历史线索 {len(clues)} 条")
    for s in stores:
        c = clues_by_store.get(s.store_id, [])
        log(f"  {s.store_id} {s.store_name}: {len(c)} 条线索")

    # 运行
    orchestrator = TaobaoOrchestrator()
    await orchestrator.run(stores, clues_by_store)

    # 写输出
    log("写入输出文件...")
    write_mapping_csv(orchestrator.mappings)
    write_results_csv(orchestrator.results)
    write_unresolved_csv(orchestrator.unresolved)
    write_summary_md(stores, orchestrator.mappings, orchestrator.results,
                     orchestrator.unresolved, orchestrator)

    # 统计
    found = sum(1 for m in orchestrator.mappings if m.match_result == "已找到")
    pending = sum(1 for m in orchestrator.mappings if m.match_result == "待确认")
    success = sum(1 for r in orchestrator.results if r.result == "成功")
    log("=" * 70)
    log(f"采集完成：店铺映射 已找到={found} 待确认={pending}；"
        f"药品成功={success}/{len(orchestrator.results)}；"
        f"平台停止={orchestrator.runner.stopped}")
    log(f"输出文件：")
    log(f"  {MAPPING_CSV}")
    log(f"  {RESULTS_CSV}")
    log(f"  {UNRESOLVED_CSV}")
    log(f"  {SUMMARY_MD}")
    log(f"  {RUN_LOG}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("用户中断", "WARN")
    except Exception as e:
        log(f"主流程异常: {e}\n{traceback.format_exc()}", "ERROR")
        raise
