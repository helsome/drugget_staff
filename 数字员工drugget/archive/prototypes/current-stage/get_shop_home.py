#!/usr/bin/env python3
"""
店铺主页获取 + 店铺内搜索（修复版）

策略：
1. 打开商品详情页 → browser find 提取店铺链接
2. 构造店铺搜索URL: https://shopXXXXXX.taobao.com/search.htm?q=关键词
3. 直接打开搜索URL，用 browser state 提取结果
4. 记录真实店铺主页和店铺ID

不拼接中文店名，只用真实链接。
"""
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path("/Users/helson/coding/cttq_work/数字员工drugget")
sys.path.insert(0, str(PROJECT_ROOT / "src"))

OPENCLI = "opencli"
PLATFORM = "taobao"
ALIAS = "taobao-p0"


@dataclass
class ShopResult:
    store_id: str
    store_name: str
    actual_shop_name: str = ""
    shop_id: str = ""
    shop_home_url: str = ""
    found: bool = False
    drug_items: list[dict] = None

    def __post_init__(self):
        if self.drug_items is None:
            self.drug_items = []


async def run_cmd(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        OPENCLI, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return -1, "", "TIMEOUT"
    return process.returncode or 0, stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")


async def open_detail(item_id: str) -> dict[str, str]:
    print(f"  [detail] 打开商品 {item_id}...")
    code, stdout, stderr = await run_cmd(
        PLATFORM, "detail", item_id,
        "-f", "json",
        "--window", "foreground",
        "--site-session", "persistent",
        "--keep-tab", "true",
        timeout=180,
    )
    if code != 0:
        return {}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):
        return {item["field"]: item.get("value", "") for item in data if isinstance(item, dict)}
    return data if isinstance(data, dict) else {}


async def find_shop_link() -> str | None:
    await asyncio.sleep(2)
    code, stdout, stderr = await run_cmd(
        "browser", ALIAS, "find",
        "--css", "a[href*='shop' i], a[href*='tmall.com/shop']",
        "--limit", "10",
        timeout=30,
    )
    if code != 0:
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    for e in data.get("entries", []):
        text = e.get("text", "").strip()
        href = e.get("attrs", {}).get("href", "")
        if "进店" in text and href:
            return href
    for e in data.get("entries", []):
        href = e.get("attrs", {}).get("href", "")
        if href:
            return href
    return None


def normalize_shop_url(url: str) -> str:
    """规范化店铺URL。"""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    # 去掉 /category.htm 等后缀，保留纯净店铺主页
    url = re.sub(r"/category\.htm.*$", "/", url)
    url = re.sub(r"/search\.htm.*$", "/", url)
    if not url.endswith("/"):
        url += "/"
    return url


def extract_shop_id(shop_url: str) -> str:
    m = re.search(r"shop(\d+)", shop_url)
    return m.group(1) if m else ""


async def search_in_shop(shop_url: str, keyword: str) -> list[dict]:
    """在店铺内搜索药品：直接构造搜索URL，用browser eval提取结果。"""
    clean_url = normalize_shop_url(shop_url)
    search_url = f"{clean_url.rstrip('/')}/search.htm?q={keyword}"
    print(f"  [shop_search] {search_url}")

    code, stdout, stderr = await run_cmd(
        "browser", ALIAS, "open", search_url,
        "--window", "foreground",
        timeout=30,
    )
    if code != 0:
        return []

    await asyncio.sleep(4)

    # 用 eval 提取商品链接
    js = """
    JSON.stringify(
        Array.from(document.querySelectorAll('a[href*="item.htm"], a[href*="item.taobao"]'))
            .slice(0, 10)
            .map(function(a) {
                return {
                    title: (a.innerText || a.textContent || '').trim().replace(/\\n/g, ' ').substring(0, 80),
                    href: a.href,
                    item_id: (a.href.match(/[?&]id=(\\d+)/) || ['', ''])[1]
                }
            })
            .filter(function(x) { return x.title || x.item_id })
    )
    """
    code, stdout, stderr = await run_cmd(
        "browser", ALIAS, "eval", js,
        timeout=30,
    )
    if code != 0 or not stdout.strip():
        return []

    # stderr contains the eval output, stdout contains browser open result
    # Try loading from stderr first, then stdout
    for source in [stderr, stdout]:
        match = re.search(r'\[.*\]', source, re.DOTALL)
        if match:
            try:
                items = json.loads(match.group())
                if isinstance(items, list) and len(items) > 0:
                    return items
            except json.JSONDecodeError:
                continue
    return []


async def process_shop(store_id: str, store_name: str, item_id: str,
                       drug_keyword: str = "托妥 瑞舒伐他汀钙片") -> ShopResult:
    result = ShopResult(store_id=store_id, store_name=store_name)

    # 1. 打开商品详情页
    fields = await open_detail(item_id)
    if not fields:
        return result
    result.actual_shop_name = fields.get("店铺", "")

    # 2. 查找店铺入口链接
    shop_url = await find_shop_link()
    if not shop_url:
        return result

    result.shop_home_url = normalize_shop_url(shop_url)
    result.shop_id = extract_shop_id(shop_url)
    result.found = True

    # 3. 店铺内搜索
    await asyncio.sleep(2)
    items = await search_in_shop(result.shop_home_url, drug_keyword)
    result.drug_items = items

    return result


async def main():
    test_cases = [
        ("W00001", "阿里健康大药房", "648878452873", "托妥 瑞舒伐他汀钙片"),
        ("W00003", "依伦平旗舰店", "674234987846", "依伦平 厄贝沙坦氢氯噻嗪片"),
        ("W04566", "上元堂大药房旗舰店", "708364294118", "葛泰 地奥司明片"),
    ]

    results = []
    for store_id, store_name, item_id, keyword in test_cases:
        print(f"\n=== {store_id} {store_name} ===")
        r = await process_shop(store_id, store_name, item_id, keyword)
        results.append(r)

        if r.found:
            print(f"  ✅ 店铺主页: {r.shop_home_url}")
            print(f"  ✅ 店铺ID: {r.shop_id}")
            if r.drug_items:
                print(f"  ✅ 店铺内搜索找到 {len(r.drug_items)} 个商品:")
                for item in r.drug_items[:3]:
                    print(f"     - {item['title']} ({item['item_id']})")
            else:
                print(f"  ⚠️ 店铺内搜索未找到商品")
        else:
            print(f"  ❌ 未找到店铺主页")

        await asyncio.sleep(3 + __import__('random').uniform(0, 3))

    print("\n===== 结果汇总 =====")
    print(f"{'店铺ID':<10} {'店铺名称':<20} {'店铺主页':<50} {'平台店铺ID':<15} {'店内商品':<10}")
    print("-" * 110)
    for r in results:
        print(f"{r.store_id:<10} {r.store_name:<20} {r.shop_home_url:<50} {r.shop_id:<15} {len(r.drug_items):<10}")

    # 保存到CSV
    import csv
    csv_path = Path("/Users/helson/coding/cttq_work/数字员工drugget/outputs/current-stage/shop_home_verified.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["档案店铺ID", "档案店铺名称", "实际店铺名称", "平台店铺ID", "店铺主页链接", "获取方法", "店铺内搜索商品数"])
        for r in results:
            w.writerow([r.store_id, r.store_name, r.actual_shop_name, r.shop_id, r.shop_home_url,
                       "browser_find" if r.found else "未找到", len(r.drug_items)])
    print(f"\n✅ 已保存: {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())