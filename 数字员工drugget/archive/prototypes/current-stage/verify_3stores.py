#!/usr/bin/env python3
"""3家店铺验证脚本。

验证目标：
1. Search路线能独立找到商品（676811555503 / platform_search / ¥85.8）
2. SKU ID真实提取或标注"平台未返回"
3. 店铺主页链接留空（不拼接中文店名）
4. ¥1价格被拦截为"价格可疑"
"""
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path("/Users/helson/coding/cttq_work/数字员工drugget")
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "outputs" / "current-stage"))

from run_taobao import (
    TaobaoOrchestrator, StoreTask, ClueItem,
    read_store_tasks, read_clues, group_clues_by_store,
    log, write_mapping_csv, write_results_csv, write_unresolved_csv,
)

OUTPUT_DIR = PROJECT_ROOT / "outputs" / "current-stage"

# 验证店铺：依伦平旗舰店、阿里健康大药房、上元堂大药房
VERIFY_STORES = {"W00001", "W00003", "W04566"}


async def main():
    all_stores = read_store_tasks()
    all_clues = read_clues()
    all_by_store = group_clues_by_store(all_clues, all_stores)

    # 只选3家验证店铺
    stores = [s for s in all_stores if s.store_id in VERIFY_STORES]
    clues_by_store = {s.store_id: all_by_store.get(s.store_id, []) for s in stores}

    log(f"验证店铺 {len(stores)} 家")
    for s in stores:
        c = clues_by_store.get(s.store_id, [])
        log(f"  {s.store_id} {s.store_name}: {len(c)} 条线索")

    orchestrator = TaobaoOrchestrator()
    await orchestrator.run(stores, clues_by_store)

    # 最终保存
    write_mapping_csv(orchestrator.mappings)
    write_results_csv(orchestrator.results)
    write_unresolved_csv(orchestrator.unresolved)

    # 验证检查
    print("\n===== 验证检查 =====")
    results = orchestrator.results

    # 1. Search路线独立成功
    search_success = [r for r in results if r.source_route == "platform_search" and r.result == "成功"]
    print(f"\n1. Search路线独立成功: {len(search_success)} 条")
    for r in search_success:
        print(f"   ✅ {r.drug_name} {r.drug_spec} item_id={r.item_id} price={r.page_price}")

    # 2. SKU ID
    success = [r for r in results if r.result == "成功"]
    sku_populated = [r for r in success if r.sku_id and r.sku_id != "（平台详情未返回）"]
    print(f"\n2. SKU ID: {len(sku_populated)}/{len(success)} 条有值")
    for r in success:
        print(f"   {r.drug_name}: SKU={r.sku_id}")

    # 3. 店铺主页链接
    homepages = [r for r in success if r.store_home_url]
    print(f"\n3. 店铺主页链接: {len(homepages)}/{len(success)} 条有值")
    for r in success:
        hp = r.store_home_url if r.store_home_url else "(空)"
        print(f"   {r.store_name}: {hp}")

    # 4. ¥1价格拦截
    suspicious = [r for r in results if r.result == "价格不明"]
    print(f"\n4. 价格可疑: {len(suspicious)} 条")
    for r in suspicious:
        print(f"   ✅ {r.drug_name} {r.drug_spec}: {r.page_price} → {r.fail_reason}")

    # 5. 总体统计
    print(f"\n5. 总体统计 ({len(results)}条):")
    for r in results:
        print(f"   [{r.source_route}] {r.store_name} {r.drug_name} {r.drug_spec} → {r.result} {r.page_price}")

    print("\n验证完成！")


if __name__ == "__main__":
    asyncio.run(main())