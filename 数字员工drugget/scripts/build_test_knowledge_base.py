from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "data/knowledge-base"
DEFAULT_OUTPUT = ROOT / "data/fixtures" / "业务知识库测试集"
DEFAULT_CONFIG = ROOT / "data/config" / "test_fixture_targets.json"
PLATFORMS = ("taobao", "yaoshibang")

# Hardcoded fallback targets (used when config file is missing)
_HARDCODED_TARGETS: dict[tuple[str, str], tuple[str, ...]] = {
    ("taobao", "W00001"): ("依伦平", "优立维"),
    ("taobao", "W00038"): ("托妥",),
    ("yaoshibang", "W00010"): ("葛泰",),
    ("yaoshibang", "W00019"): ("优立维",),
    ("yaoshibang", "W06410"): ("依伦平", "托妥"),
}

_HARDCODED_GLOBAL_SEARCH_BRANDS = ("依伦平", "优立维", "托妥")

_HARDCODED_TECHNICAL_CLOSED_LOOP = (
    {
        "platform_code": "taobao", "store_id": "W00001", "brand": "托妥",
        "shop_home": "https://shop163215406.taobao.com/",
        "selection_reason": "technical_closed_loop_fixture",
    },
)


def _load_targets(
    config_path: Path,
    *,
    drug_override: str | None = None,
    store_ids_override: str | None = None,
) -> tuple[dict[tuple[str, str], tuple[str, ...]], tuple[str, ...], tuple[dict[str, str], ...]]:
    """Load targets from JSON config, with optional CLI overrides.

    Returns (TARGETS, global_search_brands, TECHNICAL_CLOSED_LOOP_TARGETS).
    """
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        store_targets = config.get("store_drug_targets", {})
        targets: dict[tuple[str, str], tuple[str, ...]] = {}
        for platform, stores in store_targets.items():
            for store_id, brands in stores.items():
                targets[(platform, store_id)] = tuple(brands)
        global_brands = tuple(config.get("global_search_brands", _HARDCODED_GLOBAL_SEARCH_BRANDS))
        tech_loop = tuple(config.get("technical_closed_loop", _HARDCODED_TECHNICAL_CLOSED_LOOP))
    else:
        targets = dict(_HARDCODED_TARGETS)
        global_brands = _HARDCODED_GLOBAL_SEARCH_BRANDS
        tech_loop = _HARDCODED_TECHNICAL_CLOSED_LOOP

    # Apply CLI overrides
    if drug_override:
        global_brands = tuple(b.strip() for b in drug_override.split(",") if b.strip())
    if store_ids_override:
        overridden_store_ids = set(s.strip() for s in store_ids_override.split(",") if s.strip())
        targets = {k: v for k, v in targets.items() if k[1] in overridden_store_ids}

    return targets, global_brands, tech_loop


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


def select_fixture(
    source: Path,
    *,
    targets: dict[tuple[str, str], tuple[str, ...]] | None = None,
    global_search_brands: tuple[str, ...] | None = None,
    technical_closed_loop_targets: tuple[dict[str, str], ...] | None = None,
) -> dict[str, list[dict[str, str]]]:
    _targets = targets if targets is not None else _HARDCODED_TARGETS
    _global_search_brands = global_search_brands if global_search_brands is not None else _HARDCODED_GLOBAL_SEARCH_BRANDS
    _tech_loop = technical_closed_loop_targets if technical_closed_loop_targets is not None else _HARDCODED_TECHNICAL_CLOSED_LOOP
    stores = read_csv(source / "store_master.csv")
    drugs = read_csv(source / "drug_master.csv")
    packages = read_csv(source / "drug_package_master.csv")
    controls = read_csv(source / "control_price_rules.csv")
    tasks = read_csv(source / "monitor_task_master.csv")
    observations = read_csv(source / "price_observations_clean.csv")
    issues = read_csv(source / "data_quality_issues.csv")

    store_by_id = {row["store_id"]: row for row in stores}
    target_brands = {brand for brands in _targets.values() for brand in brands}
    selected_store_ids = {store_id for _, store_id in _targets}

    selected_stores = []
    for store in stores:
        key = (store["platform_code"], store["store_id"])
        if key not in _targets:
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
    for (platform, store_id), brands in _targets.items():
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
    for fixture_target in _tech_loop:
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
    global_search_brands = _global_search_brands
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


def required_source_files(source: Path) -> list[Path]:
    return [
        source / "manifest.json", source / "store_master.csv", source / "drug_master.csv",
        source / "drug_package_master.csv", source / "control_price_rules.csv",
        source / "monitor_task_master.csv", source / "price_observations_clean.csv",
        source / "data_quality_issues.csv",
    ]


def validate_source(source: Path) -> None:
    missing = [path for path in required_source_files(source) if not path.is_file()]
    if missing:
        raise FileNotFoundError("测试库来源不完整: " + ", ".join(str(path) for path in missing))


def build_database(
    source: Path,
    output: Path,
    *,
    targets: dict[tuple[str, str], tuple[str, ...]] | None = None,
    global_search_brands: tuple[str, ...] | None = None,
    technical_closed_loop_targets: tuple[dict[str, str], ...] | None = None,
) -> dict[str, object]:
    """Build a fixture snapshot in an isolated output directory."""
    validate_source(source)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    fixture = select_fixture(
        source,
        targets=targets,
        global_search_brands=global_search_brands,
        technical_closed_loop_targets=technical_closed_loop_targets,
    )
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

这个目录由 `scripts/build_test_knowledge_base.py` 从正式业务知识库确定性抽取，用于验证以店铺为入口的双路线调度。正式知识库和原始数据不会被修改。

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
.venv/bin/python scripts/build_test_knowledge_base.py
```

输出数据库：`{database.name}`  
详细数量：`summary.json`
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    return summary


def validate_fixture(output: Path) -> None:
    database = output / "price_specialist_test.sqlite3"
    summary_path = output / "summary.json"
    readme = output / "README.md"
    if not database.is_file() or not summary_path.is_file() or not readme.is_file():
        raise ValueError("测试库快照不完整")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    with sqlite3.connect(database) as connection:
        version = connection.execute("SELECT value FROM fixture_info WHERE key='fixture_version'").fetchone()
        if version is None or version[0] != summary["fixture_version"]:
            raise ValueError("测试库 summary 与 SQLite fixture_version 不一致")


def rebuild_source_snapshot(raw_source: Path, destination: Path) -> Path:
    """Recreate a disposable complete source snapshot; never publish it as KB."""
    from build_knowledge_base import build_atomically

    build_atomically(raw_source.resolve(), destination.resolve())
    validate_source(destination)
    return destination


def build_atomically(
    source: Path,
    output: Path,
    *,
    targets: dict[tuple[str, str], tuple[str, ...]] | None = None,
    global_search_brands: tuple[str, ...] | None = None,
    technical_closed_loop_targets: tuple[dict[str, str], ...] | None = None,
) -> dict[str, object]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=output.parent) as temp:
        staged = Path(temp) / "fixture"
        summary = build_database(source, staged, targets=targets, global_search_brands=global_search_brands, technical_closed_loop_targets=technical_closed_loop_targets)
        validate_fixture(staged)
        output.mkdir(parents=True, exist_ok=True)
        for path in staged.iterdir():
            os.replace(path, output / path.name)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="从正式业务知识库生成店铺驱动测试库")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="测试目标配置文件路径（JSON）")
    parser.add_argument("--drugs", type=str, default=None,
                        help="覆盖全局搜索药品列表（逗号分隔）")
    parser.add_argument("--store-ids", type=str, default=None,
                        help="覆盖店铺 ID 列表（逗号分隔）")
    parser.add_argument(
        "--rebuild-source", action="store_true",
        help="当来源缺少 price_observations_clean.csv 时，在临时目录重建完整来源快照",
    )
    parser.add_argument(
        "--raw-source", type=Path, default=ROOT / "data/raw",
        help="--rebuild-source 使用的原始数据目录",
    )
    args = parser.parse_args()
    targets, global_brands, tech_loop = _load_targets(
        args.config, drug_override=args.drugs, store_ids_override=args.store_ids,
    )
    source = args.source.resolve()
    if not (source / "price_observations_clean.csv").is_file() and not args.rebuild_source:
        parser.error("来源缺少 price_observations_clean.csv；如需临时恢复，请显式传入 --rebuild-source")
    if args.rebuild_source and not (source / "price_observations_clean.csv").is_file():
        with tempfile.TemporaryDirectory(prefix="fixture-source-", dir=args.output.resolve().parent) as temp:
            source = rebuild_source_snapshot(args.raw_source, Path(temp) / "knowledge-base")
            summary = build_atomically(
                source, args.output,
                targets=targets, global_search_brands=global_brands, technical_closed_loop_targets=tech_loop,
            )
    else:
        summary = build_atomically(
            source, args.output,
            targets=targets, global_search_brands=global_brands, technical_closed_loop_targets=tech_loop,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
