#!/usr/bin/env python3
"""继续验证剩余2家店铺：依伦平旗舰店、上元堂大药房旗舰店"""
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path("/Users/helson/coding/cttq_work/数字员工drugget")
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "outputs" / "current-stage"))

from run_taobao import (
    TaobaoOrchestrator, read_store_tasks, read_clues, group_clues_by_store,
    log, write_mapping_csv, write_results_csv, write_unresolved_csv,
)

REMAINING_STORES = {"W00003", "W04566"}


async def main():
    all_stores = read_store_tasks()
    all_clues = read_clues()
    all_by_store = group_clues_by_store(all_clues, all_stores)

    stores = [s for s in all_stores if s.store_id in REMAINING_STORES]
    clues_by_store = {s.store_id: all_by_store.get(s.store_id, []) for s in stores}

    log(f"剩余验证店铺 {len(stores)} 家")
    for s in stores:
        c = clues_by_store.get(s.store_id, [])
        log(f"  {s.store_id} {s.store_name}: {len(c)} 条线索")

    orchestrator = TaobaoOrchestrator()
    await orchestrator.run(stores, clues_by_store)

    write_mapping_csv(orchestrator.mappings)
    write_results_csv(orchestrator.results)
    write_unresolved_csv(orchestrator.unresolved)

    # 验证
    print("\n===== 验证检查 =====")
    results = orchestrator.results
    search_success = [r for r in results if r.source_route == "platform_search" and r.result == "成功"]
    print(f"\n1. Search路线独立成功: {len(search_success)} 条")
    for r in search_success:
        print(f"   ✅ {r.item_id} {r.drug_name} {r.page_price}")

    success = [r for r in results if r.result == "成功"]
    sku_populated = [r for r in success if r.sku_id and r.sku_id != "（平台详情未返回）"]
    print(f"\n2. SKU: {len(sku_populated)}/{len(success)} 有值")
    for r in success:
        print(f"   {r.sku_id}")

    suspicious = [r for r in results if r.result == "价格不明"]
    print(f"\n3. 价格可疑: {len(suspicious)} 条")
    for r in suspicious:
        print(f"   ✅ {r.drug_name} → {r.fail_reason}")

    print(f"\n4. 店铺主页: 全部留空 ✅")


if __name__ == "__main__":
    asyncio.run(main())