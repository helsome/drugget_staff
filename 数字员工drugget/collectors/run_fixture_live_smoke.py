"""Run a bounded live smoke test from the store-driven fixture through Python.

The fixture database is read-only input. Results are persisted to the normal
runtime database through BatchOrchestrator; no OpenCLI command is invoked by
this script outside OpenCLIComputerUseCollector.
"""
from __future__ import annotations

import asyncio
import argparse
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update

from price_specialist.collector import OpenCLIComputerUseCollector
from price_specialist.config import Settings
from price_specialist.database import configured_database, init_database
from price_specialist.enums import TaskStatus, TaskType
from price_specialist.evidence import EvidenceStore
from price_specialist.models import CollectionRun, CollectionTask, DrugProduct, PriceObservation, SearchCandidate, StoreResponsibility
from price_specialist.orchestrator import BatchOrchestrator
from price_specialist.review_factory import build_review_orchestrator
from price_specialist.run_logger import BatchLogger
from price_specialist.schemas import CollectionTaskSpec
from price_specialist.services import StoreTaskPlanner, TaskQueueService
from export_fixture_run_csv import export_run_outputs, export_run_manifest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "data/fixtures/业务知识库测试集/price_specialist_test.sqlite3"
# Do not infer Taobao storefront URLs from shop names.  Store routes remain
# auditable human-review cases until a verified homepage is supplied.
VERIFIED_TAOBAO_HOMES: dict[str, str] = {}


def select_yaoshibang_seed(*, seed_key: str | None, store_id: str | None, brand: str | None) -> dict[str, str]:
    """Return one fixture seed; store selection chooses the drug, not a supplier."""
    if bool(seed_key) == bool(store_id or brand):
        raise ValueError("请提供 --seed-key，或同时提供 --store-id 和 --brand")
    if bool(store_id) != bool(brand):
        raise ValueError("--store-id 和 --brand 必须同时提供")
    with sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True) as fixture:
        fixture.row_factory = sqlite3.Row
        if seed_key:
            rows = fixture.execute(
                "SELECT * FROM task_seeds WHERE seed_key = ? AND platform_code = 'yaoshibang'", (seed_key,)
            ).fetchall()
        else:
            rows = fixture.execute(
                """SELECT * FROM task_seeds
                   WHERE platform_code = 'yaoshibang' AND seed_type = 'STORE_SEARCH'
                     AND store_id = ? AND brand = ?""",
                (store_id, brand),
            ).fetchall()
    if len(rows) != 1:
        selector = seed_key or f"{store_id}+{brand}"
        raise ValueError(f"药师帮测试种子必须唯一匹配；选择条件 {selector!r} 命中 {len(rows)} 条")
    return dict(rows[0])


def fixture_specs(brand: str) -> set[str]:
    with sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True) as fixture:
        return {
            str(row[0]) for row in fixture.execute(
                "SELECT spec_normalized FROM drug_package_master WHERE brand = ?", (brand,)
            ) if row[0]
        }


def validate_yaoshibang_detail(*, detail: PriceObservation | None, detail_task: CollectionTask,
                                expected_specs: set[str]) -> str:
    """Gate formal prices on the detail-page fields required by the fixture flow."""
    if detail is None or detail.channel != "detail" or detail.collection_status != "success" or detail.page_price_value is None:
        raise RuntimeError("详情页未形成确认成功的正式价格")
    if not detail.selected_spec or detail.selected_spec not in expected_specs:
        raise RuntimeError(f"详情规格未匹配测试规格: {detail.selected_spec}")
    if detail.sale_box_count is None or not detail.page_shop:
        raise RuntimeError("详情页缺少起购盒数或供应商身份")
    detail_spec = CollectionTaskSpec.model_validate(detail_task.payload)
    provider_id = str(detail_spec.metadata.get("provider_id") or "")
    if not provider_id:
        raise RuntimeError("详情任务未保存 provider_id")
    return provider_id


def validate_provider_store_search(store_search: PriceObservation | None) -> None:
    """Require the same-provider route to return at least one filtered hit."""
    hits = (store_search.raw_evidence or {}).get("hits", []) if store_search else []
    if store_search is None or store_search.collection_status != "success" or not isinstance(hits, list) or not hits:
        raise RuntimeError("供应商内搜索未返回有效结果")


async def run_yaoshibang_seed(*, seed: dict[str, str], max_candidates: int, output_root: Path | None) -> dict[str, object]:
    """One bounded global-discovery → detail → same-provider store-search loop."""
    settings = Settings.load(PROJECT_ROOT, mode="prod")
    engine, factory = configured_database(settings)
    init_database(engine)
    with factory() as db:
        queue = TaskQueueService(db)
        run = queue.create_run()
        drug = db.scalar(select(DrugProduct).where(DrugProduct.brand_name == seed["brand"]))
        if drug is None:
            drug = DrugProduct(brand_name=seed["brand"], generic_name=seed["generic_name"])
            db.add(drug)
            db.flush()
        query = f"{seed['brand']} {seed['generic_name']}"
        expected_specs = fixture_specs(seed["brand"])
        provider_search_task = queue.enqueue(CollectionTaskSpec(
            task_id=str(uuid.uuid4()), run_id=run.id, platform="yaoshibang", task_type=TaskType.SEARCH,
            session_alias="yaoshibang-p0", priority=10, drug_name=seed["brand"], generic_name=seed["generic_name"],
            spec=seed["spec_normalized"] or None, query=query,
            metadata={"drug_id": drug.id, "target_brand": seed["brand"],
                      "target_spec": seed["spec_normalized"] or None, "fixture_seed_key": seed["seed_key"],
                      "inspect_limit": max_candidates},
        ))
        db.commit()
        review_orchestrator = build_review_orchestrator(
            session=db, settings=settings, run_id=run.id, event_sink=None,
            runtime_mode="production",
        )
        runner = BatchOrchestrator(
            session=db, collector=OpenCLIComputerUseCollector(settings),
            evidence_store=EvidenceStore(settings.evidence_dir), run_id=run.id,
            review_orchestrator=review_orchestrator,
        )
        await runner.execute_platform("yaoshibang", "yaoshibang-p0", task_types={TaskType.SEARCH.value})
        detail_task = db.scalar(select(CollectionTask).where(
            CollectionTask.run_id == run.id, CollectionTask.task_type == TaskType.INSPECT_CANDIDATE.value,
        ))
        if detail_task is None:
            raise RuntimeError("全站搜索没有产生有效候选，未创建详情任务")
        await runner.execute_platform("yaoshibang", "yaoshibang-p0", task_types={TaskType.INSPECT_CANDIDATE.value})
        detail = db.scalar(select(PriceObservation).where(PriceObservation.task_id == detail_task.id))
        detail_spec = CollectionTaskSpec.model_validate(detail_task.payload)
        provider_id = validate_yaoshibang_detail(
            detail=detail, detail_task=detail_task, expected_specs=expected_specs,
        )
        store = db.scalar(select(StoreResponsibility).where(
            StoreResponsibility.platform == "yaoshibang", StoreResponsibility.platform_store_key == provider_id,
        ))
        if store is None:
            store = StoreResponsibility(
                internal_store_id=f"ysb-provider-{provider_id}", platform="yaoshibang", platform_store_key=provider_id,
                shop_name=detail.page_shop, shop_status="发现待复核", fixed_tier="observation_only",
            )
            db.add(store)
        else:
            store.shop_name = detail.page_shop
        queue.enqueue(CollectionTaskSpec(
            task_id=str(uuid.uuid4()), run_id=run.id, platform="yaoshibang", task_type=TaskType.STORE_SEARCH,
            session_alias="yaoshibang-p0", priority=20, drug_name=seed["brand"], generic_name=seed["generic_name"],
            spec=seed["spec_normalized"] or None, shop_name=detail.page_shop, query=query,
            metadata={"drug_id": drug.id, "target_brand": seed["brand"], "target_spec": seed["spec_normalized"] or None,
                      "fixture_seed_key": seed["seed_key"], "provider_id": provider_id,
                      "route": "provider_profile", "source": "verified_global_detail", "inspect_limit": -1},
        ))
        db.commit()
        await runner.execute_platform("yaoshibang", "yaoshibang-p0", task_types={TaskType.STORE_SEARCH.value})
        provider_search = db.scalar(select(PriceObservation).where(PriceObservation.task_id == provider_search_task.id))
        validate_provider_store_search(provider_search)
        run.status = "completed"
        run.finished_at = datetime.now()
        run.summary = {"fixture_seed_key": seed["seed_key"], "provider_id": provider_id,
                       "product_id": detail_spec.product_id, "captured_page_price": str(detail.page_price_value),
                       "spec": detail.selected_spec, "sale_box_count": str(detail.sale_box_count)}
        db.commit()
        destination = (output_root or (PROJECT_ROOT / "artifacts/runs/current" / datetime.now().strftime("%Y-%m-%d"))) / run.id
        destination.mkdir(parents=True, exist_ok=True)
        export_run_outputs(run.id, db, destination)
        export_run_manifest(run.id, db, destination, run=run, source_type="fixture_smoke")
        return {"run_id": run.id, "seed_key": seed["seed_key"], "provider_id": provider_id,
                "shop_name": detail.page_shop, "price": str(detail.page_price_value),
                "spec": detail.selected_spec, "sale_box_count": str(detail.sale_box_count), "output": str(destination)}


def seed_smoke_tasks(queue: TaskQueueService, run_id: str, db, *, only_store_id: str | None = None) -> None:
    """Queue the complete fixture coverage for a bounded live run.

    This includes every store-driven target (8) plus one primary
    ``brand + generic`` global search per fixture drug and platform (8).
    Storefront URLs are used only when explicitly verified in this module.
    """
    with sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True) as fixture:
        fixture.row_factory = sqlite3.Row
        rows = fixture.execute("SELECT * FROM task_seeds WHERE seed_type = 'STORE_SEARCH' ORDER BY platform_code, store_id, brand").fetchall()
        store_names = {
            (row["platform_code"], row["store_id"]): row["shop_name"]
            for row in fixture.execute("SELECT platform_code, store_id, shop_name FROM store_drug_targets")
        }
        drugs = fixture.execute("SELECT brand, generic_name FROM drug_master ORDER BY brand").fetchall()
        global_rows = fixture.execute(
            "SELECT * FROM task_seeds WHERE seed_type = 'GLOBAL_SEARCH' AND query_type = 'brand_generic'"
        ).fetchall()

    # Synthesize any missing brand+generic rows from the fixture drug master so
    # every test drug receives the same direct-search coverage.
    existing_global = {(row["platform_code"], row["brand"]): row for row in global_rows}
    for drug in drugs:
        for platform in ("taobao", "yaoshibang"):
            if (platform, drug["brand"]) not in existing_global:
                existing_global[(platform, drug["brand"])] = {
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
                }
    if only_store_id:
        rows = [row for row in rows if row["store_id"] == only_store_id]
        if not rows:
            raise ValueError(f"测试数据集中没有店铺ID: {only_store_id}")
    else:
        rows = [*rows, *[existing_global[key] for key in sorted(existing_global)]]
    # 历史手填的 yaoshibang provider_id（W00010=5201, W00019=21288, W06410=9023）
    # 从未在任何真实搜索/观测中出现，是假值；清空后强制 collector 走 resolve-provider
    # 发现真实 provider_id，并由 orchestrator 回填到 StoreResponsibility。
    # 使用公共 StoreTaskPlanner.sanitize_fake_provider_ids 服务。
    StoreTaskPlanner.sanitize_fake_provider_ids(db)
    db.flush()
    for row in rows:
        drug = db.scalar(select(DrugProduct).where(DrugProduct.brand_name == row["brand"]))
        if drug is None:
            drug = DrugProduct(brand_name=row["brand"], generic_name=row["generic_name"])
            db.add(drug)
            db.flush()
        store = None
        if row["store_id"]:
            store = db.scalar(select(StoreResponsibility).where(StoreResponsibility.internal_store_id == row["store_id"]))
            if store is None:
                shop = store_names[(row["platform_code"], row["store_id"])]
                store = StoreResponsibility(
                    internal_store_id=row["store_id"], platform=row["platform_code"], shop_name=shop,
                    platform_store_key=VERIFIED_TAOBAO_HOMES.get(row["store_id"]) if row["platform_code"] == "taobao" else None,
                    shop_status="正常", fixed_tier="observation_only",
                )
                db.add(store)
                db.flush()
        store_route = row["seed_type"] == "STORE_SEARCH"
        stored_provider_id = (
            str(store.platform_store_key or "")
            if store is not None and row["platform_code"] == "yaoshibang"
            else ""
        )
        queue.enqueue(CollectionTaskSpec(
            task_id=str(uuid.uuid4()), run_id=run_id, platform=row["platform_code"],
            task_type=TaskType.STORE_SEARCH if store_route else TaskType.SEARCH,
            session_alias=f"{row['platform_code']}-p0", priority=int(row["priority"]),
            drug_name=row["brand"], generic_name=row["generic_name"], spec=row["spec_normalized"] or None,
            shop_name=store.shop_name if store else None,
            query=(f"{row['brand']} {row['generic_name']}" if store_route else row["query"]),
            metadata={
                "drug_id": drug.id, "target_brand": row["brand"], "target_spec": row["spec_normalized"] or None,
                "route": "shop_home" if row["platform_code"] == "taobao" and store_route else "provider_profile" if store_route else "global",
                "shop_home_url": (store.shop_home_url or VERIFIED_TAOBAO_HOMES.get(row["store_id"])) if row["platform_code"] == "taobao" and store_route else None,
                "provider_id": stored_provider_id or None,
                # Fixture intentionally has no provider_id. The collector now
                # calls `resolve-provider` and proceeds only on a unique exact
                # name match; ambiguity remains an auditable human action.
                "fixture_seed_key": row["seed_key"],
                "inspect_limit": 1,
            },
        ))


async def main(*, only_store_id: str | None = None, seed_key: str | None = None,
               store_id: str | None = None, brand: str | None = None,
               platform: str | None = None, max_candidates: int = 1,
               output_root: Path | None = None, max_tasks: int | None = None,
               resume_run_id: str | None = None) -> None:
    settings = Settings.load(PROJECT_ROOT, mode="prod")
    engine, factory = configured_database(settings)
    init_database(engine)

    if seed_key or store_id or brand:
        if platform not in {None, "yaoshibang"}:
            raise ValueError("按种子闭环当前仅支持 --platform yaoshibang")
        result = await run_yaoshibang_seed(
            seed=select_yaoshibang_seed(seed_key=seed_key, store_id=store_id, brand=brand),
            max_candidates=max_candidates, output_root=output_root,
        )
        print(result)
        return

    with factory() as db:
        if resume_run_id:
            run = db.get(CollectionRun, resume_run_id)
            if run is None:
                raise ValueError(f"要恢复的批次不存在: {resume_run_id}")
            # Reset leased/running tasks back to pending for re-execution.
            db.execute(
                update(CollectionTask)
                .where(CollectionTask.run_id == resume_run_id)
                .where(CollectionTask.status.in_((TaskStatus.LEASED.value, TaskStatus.RUNNING.value)))
                .values(status=TaskStatus.PENDING.value)
            )
            db.commit()
            run_id = resume_run_id
        else:
            queue = TaskQueueService(db)
            run = queue.create_run()
            run_id = run.id
            seed_smoke_tasks(queue, run.id, db, only_store_id=only_store_id)
            db.commit()

        output_dir = (output_root or PROJECT_ROOT / "artifacts/runs/current") / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        logger = BatchLogger(run_id, output_dir)

        # Determine which platforms to run
        sessions = {}
        if platform and platform == "taobao":
            sessions["taobao"] = "taobao-p0"
        elif platform and platform == "yaoshibang":
            sessions["yaoshibang"] = "yaoshibang-p0"
        elif not platform:
            sessions["taobao"] = "taobao-p0"
            sessions["yaoshibang"] = "yaoshibang-p0"

        review_orchestrator = build_review_orchestrator(
            session=db, settings=settings, run_id=run_id, event_sink=None,
            runtime_mode="production",
        )
        outcome = await BatchOrchestrator(
            session=db, collector=OpenCLIComputerUseCollector(settings),
            evidence_store=EvidenceStore(settings.evidence_dir), run_id=run_id,
            logger=logger, review_orchestrator=review_orchestrator,
        ).execute_all(sessions, max_tasks_per_platform=max_tasks)

        logger.close()

        # Auto-export CSV artifacts
        export_run_outputs(run_id, db, output_dir)
        export_run_manifest(run_id, db, output_dir, run=run, source_type="fixture_smoke")
        print({"run_id": run_id, "outcome": outcome, "output": str(output_dir)})
        print(f"CSV导出目录: {output_dir}")
        print(f"JSONL日志: {logger.log_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行测试集的受限在线采集")
    parser.add_argument("--only-store-id", help="仅执行指定店铺ID的店铺驱动任务，不执行全站搜索")
    parser.add_argument("--seed-key", help="选择一条药师帮测试种子并运行受限闭环")
    parser.add_argument("--store-id", help="与 --brand 一起选择一条药师帮店铺种子")
    parser.add_argument("--brand", help="与 --store-id 一起选择药品品牌")
    parser.add_argument("--platform", choices=("taobao", "yaoshibang"), help="限制只运行指定平台的任务")
    parser.add_argument("--max-candidates", type=int, default=1, choices=range(1, 2), help="最多确认的候选数（当前固定为 1）")
    parser.add_argument("--max-tasks", type=int, default=None, help="限制每个平台最多执行的任务数")
    parser.add_argument("--resume-run-id", help="恢复已有批次，不创建新 run 和种子任务")
    parser.add_argument("--output-root", type=Path, help="本批次 CSV 输出根目录")
    args = parser.parse_args()
    asyncio.run(main(only_store_id=args.only_store_id, seed_key=args.seed_key,
                     store_id=args.store_id, brand=args.brand, platform=args.platform,
                     max_candidates=args.max_candidates, output_root=args.output_root,
                     max_tasks=args.max_tasks, resume_run_id=args.resume_run_id))
