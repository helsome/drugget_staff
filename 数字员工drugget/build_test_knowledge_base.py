from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "业务知识库"
DEFAULT_OUTPUT = ROOT / "测试数据" / "业务知识库测试集"
PLATFORMS = ("taobao", "yaoshibang")

TARGETS = {
    ("taobao", "W00001"): ("依伦平", "优立维"),
    ("taobao", "W00038"): ("托妥",),
    ("yaoshibang", "W00010"): ("葛泰",),
    ("yaoshibang", "W00019"): ("优立维",),
    ("yaoshibang", "W06410"): ("依伦平", "托妥"),
}

# This fixture proves the Taobao technical closed loop only.  It is deliberately
# separate from TARGETS, which remains the business-facing monitoring scope.
TECHNICAL_CLOSED_LOOP_TARGETS = (
    {
        "platform_code": "taobao", "store_id": "W00001", "brand": "托妥",
        "shop_home": "https://shop163215406.taobao.com/",
        "selection_reason": "technical_closed_loop_fixture",
    },
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def create_text_table(
    connection: sqlite3.Connection,
    table: str,
    rows: list[dict[str, str]],
    fieldnames: list[str] | None = None,
) -> None:
    columns = fieldnames or (list(rows[0]) if rows else [])
    if not columns:
        raise ValueError(f"{table}没有可用字段")
    connection.execute(f"DROP TABLE IF EXISTS {identifier(table)}")
    definition = ", ".join(f"{identifier(column)} TEXT" for column in columns)
    connection.execute(f"CREATE TABLE {identifier(table)} ({definition})")
    if rows:
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(identifier(column) for column in columns)
        connection.executemany(
            f"INSERT INTO {identifier(table)} ({column_sql}) VALUES ({placeholders})",
            [[row.get(column, "") for column in columns] for row in rows],
        )


def select_fixture(source: Path) -> dict[str, list[dict[str, str]]]:
    stores = read_csv(source / "store_master.csv")
    drugs = read_csv(source / "drug_master.csv")
    packages = read_csv(source / "drug_package_master.csv")
    controls = read_csv(source / "control_price_rules.csv")
    tasks = read_csv(source / "monitor_task_master.csv")
    observations = read_csv(source / "price_observations_clean.csv")
    issues = read_csv(source / "data_quality_issues.csv")

    store_by_id = {row["store_id"]: row for row in stores}
    target_brands = {brand for brands in TARGETS.values() for brand in brands}
    selected_store_ids = {store_id for _, store_id in TARGETS}

    selected_stores = []
    for store in stores:
        key = (store["platform_code"], store["store_id"])
        if key not in TARGETS:
            continue
        enriched = dict(store)
        enriched["aliases"] = ",".join(
            dict.fromkeys(
                value for value in (
                    store["shop_name"],
                    store["platform_store_key"],
                    store["shop_name"].replace("旗舰店", ""),
                ) if value
            )
        )
        enriched["shop_home"] = (
            "https://shop.tmall.com/" if store["platform_code"] == "taobao"
            else "https://dian.ysbang.cn/"
        )
        selected_stores.append(enriched)

    store_drug_targets = []
    for (platform, store_id), brands in TARGETS.items():
        store = store_by_id[store_id]
        for brand in brands:
            drug = next(row for row in drugs if row["brand"] == brand)
            store_drug_targets.append({
                "target_key": f"{platform}|{store_id}|{brand}",
                "platform_code": platform,
                "store_id": store_id,
                "shop_name": store["shop_name"],
                "brand": brand,
                "generic_name": drug["generic_name"],
                "target_status": "enabled",
                "selection_reason": "store_driven_test_target",
                "shop_home": "",
            })
    for fixture_target in TECHNICAL_CLOSED_LOOP_TARGETS:
        store = store_by_id[fixture_target["store_id"]]
        drug = next(row for row in drugs if row["brand"] == fixture_target["brand"])
        store_drug_targets.append({
            "target_key": f"{fixture_target['platform_code']}|{fixture_target['store_id']}|{fixture_target['brand']}|technical_closed_loop_fixture",
            "platform_code": fixture_target["platform_code"], "store_id": fixture_target["store_id"],
            "shop_name": store["shop_name"], "brand": fixture_target["brand"],
            "generic_name": drug["generic_name"], "target_status": "enabled",
            "selection_reason": fixture_target["selection_reason"],
            "shop_home": fixture_target["shop_home"],
        })

    target_keys = {(row["platform_code"], row["store_id"], row["brand"]) for row in store_drug_targets}
    historical_product_clues = []
    clue_product_keys = set()
    for task in tasks:
        key = (task["platform_code"], task["store_id"], task["brand"])
        if key not in target_keys:
            continue
        clue = dict(task)
        clue["clue_key"] = task["task_key"]
        clue["clue_status"] = "historical_clue"
        clue["clue_reason"] = "historical_product_link_degraded_to_clue"
        historical_product_clues.append(clue)
        clue_product_keys.add((task["platform_code"], task["product_id"]))

    packages_by_brand: dict[str, list[dict[str, str]]] = defaultdict(list)
    for package in packages:
        if package["brand"] in target_brands:
            packages_by_brand[package["brand"]].append(package)
    global_search_brands = ("依伦平", "优立维", "托妥")
    search_drugs = [row for row in drugs if row["brand"] in global_search_brands]
    task_seeds = []
    for target in store_drug_targets:
        task_seeds.append({
            "seed_key": f"STORE_SEARCH|{target['target_key']}",
            "seed_type": "STORE_SEARCH",
            "platform_code": target["platform_code"],
            "store_id": target["store_id"],
            "brand": target["brand"],
            "generic_name": target["generic_name"],
            "spec_normalized": "",
            "query": f"{target['brand']} {target['generic_name']}",
            "query_type": "store_product_search",
            "priority": "10",
            "expected_mode": "technical_closed_loop" if target["selection_reason"] == "technical_closed_loop_fixture" else "store_driven",
        })
    for platform in PLATFORMS:
        for drug in search_drugs:
            task_seeds.append({
                "seed_key": f"GLOBAL_SEARCH|{platform}|{drug['brand']}|brand_generic",
                "seed_type": "GLOBAL_SEARCH",
                "platform_code": platform,
                "store_id": "",
                "brand": drug["brand"],
                "generic_name": drug["generic_name"],
                "spec_normalized": "",
                "query": f"{drug['brand']} {drug['generic_name']}",
                "query_type": "brand_generic",
                "priority": "20",
                "expected_mode": "global_search",
            })
            package = sorted(packages_by_brand[drug["brand"]], key=lambda row: row["spec_normalized"])[0]
            task_seeds.append({
                "seed_key": f"GLOBAL_SEARCH|{platform}|{drug['brand']}|{package['spec_normalized']}",
                "seed_type": "GLOBAL_SEARCH",
                "platform_code": platform,
                "store_id": "",
                "brand": drug["brand"],
                "generic_name": drug["generic_name"],
                "spec_normalized": package["spec_normalized"],
                "query": f"{drug['brand']} {package['spec_normalized']}",
                "query_type": "brand_spec",
                "priority": "21",
                "expected_mode": "global_search",
            })

    selected_observations = [
        row for row in observations
        if (row["platform_code"], row["product_id"]) in clue_product_keys
    ]
    selected_brands = {row["brand"] for row in store_drug_targets}
    selected_issues = [
        row for row in issues
        if row.get("platform_code", "") in PLATFORMS
        and (not row.get("brand") or row["brand"] in selected_brands)
    ]
    return {
        "store_master": selected_stores,
        "store_drug_targets": store_drug_targets,
        "historical_product_clues": sorted(historical_product_clues, key=lambda row: row["clue_key"]),
        "task_seeds": task_seeds,
        "drug_master": [row for row in drugs if row["brand"] in selected_brands],
        "drug_package_master": [row for row in packages if row["brand"] in selected_brands],
        "control_price_rules": [row for row in controls if row["brand"] in selected_brands],
        "price_observations_clean": sorted(selected_observations, key=lambda row: (row["captured_at"], row["source_row"])),
        "data_quality_issues": selected_issues,
    }


def build_database(source: Path, output: Path) -> dict[str, object]:
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    fixture = select_fixture(source)
    output.mkdir(parents=True, exist_ok=True)
    database = output / "price_specialist_test.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(database) + suffix)
        if path.exists():
            path.unlink()

    manifest_digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    fixture_version = f"kb-{manifest['generated_at']}-{manifest_digest[:12]}"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        empty_table_fields = {
            "data_quality_issues": ["source_file", "row_number", "issue_type", "severity", "business_key", "details"],
        }
        for table, rows in fixture.items():
            create_text_table(connection, table, rows, empty_table_fields.get(table))
        create_text_table(connection, "fixture_info", [
            {"key": "fixture_version", "value": fixture_version},
            {"key": "source_generated_at", "value": manifest["generated_at"]},
            {"key": "source_manifest_sha256", "value": manifest_digest},
            {"key": "generated_at", "value": datetime.now().astimezone().isoformat()},
            {"key": "platforms", "value": ",".join(PLATFORMS)},
            {"key": "link_live_checked", "value": "false"},
        ])
        connection.execute("CREATE UNIQUE INDEX idx_store_drug_target_key ON store_drug_targets(target_key)")
        connection.execute("CREATE UNIQUE INDEX idx_historical_clue_key ON historical_product_clues(clue_key)")
        connection.execute("CREATE UNIQUE INDEX idx_task_seed_key ON task_seeds(seed_key)")
        connection.execute("CREATE INDEX idx_task_seed_route ON task_seeds(seed_type, platform_code)")
        connection.execute("CREATE INDEX idx_observation_product ON price_observations_clean(platform_code, product_id)")
        connection.commit()

    counts = {table: len(rows) for table, rows in fixture.items()}
    summary = {
        "fixture_version": fixture_version,
        "database": str(database),
        "source_manifest_sha256": manifest_digest,
        "counts": counts,
        "seed_counts": {
            f"{seed_type}:{platform}": sum(1 for row in fixture["task_seeds"] if row["seed_type"] == seed_type and row["platform_code"] == platform)
            for seed_type in ("STORE_SEARCH", "GLOBAL_SEARCH") for platform in PLATFORMS
        },
        "links_live_checked": False,
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme = f"""# 价格专员测试知识库

这个目录由 `build_test_knowledge_base.py` 从正式业务知识库确定性抽取，用于验证以店铺为入口的双路线调度。正式知识库和原始数据不会被修改。

## 当前规模

- 店铺：{counts['store_master']} 家（淘宝、药师帮）。
- 店铺药品监控目标：{counts['store_drug_targets']} 条。
- 历史商品链接线索：{counts['historical_product_clues']} 条；仅作线索，不作为当前有效入口。
- 统一任务种子：{counts['task_seeds']} 条。
- 历史价格样本：{counts['price_observations_clean']} 条。

## 店铺驱动目标

| 平台 | 店铺 | 目标药品 |
| --- | --- | --- |
| 淘宝 | W00001 阿里健康大药房 | 依伦平、优立维 |
| 淘宝 | W00038 阜胜堂医药专营店 | 托妥 |
| 药师帮 | W00010 云天下 | 葛泰 |
| 药师帮 | W00019 扶正药局 | 优立维 |
| 药师帮 | W06410 敬一堂 | 依伦平、托妥 |

## 任务路线

- `task_seeds.seed_type = STORE_SEARCH`：进入指定店铺搜索目标药品。
- `task_seeds.seed_type = GLOBAL_SEARCH`：按品牌+通用名、品牌+规格进行全局搜索，再解析候选店铺。
- `historical_product_clues`：历史商品链接降级后的候选线索，不能替代店铺搜索。
- 测试库不包含京东记录；`fixture_info.platforms` 为 `taobao,yaoshibang`。

## 重建

```bash
cd "{ROOT}"
.venv/bin/python build_test_knowledge_base.py
```

输出数据库：`{database.name}`  
详细数量：`summary.json`
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="从正式业务知识库生成店铺驱动测试库")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build_database(args.source.resolve(), args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
