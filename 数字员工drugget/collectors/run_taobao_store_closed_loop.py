"""Run one Taobao technical closed loop, without changing business monitoring."""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path

from sqlalchemy import select

from price_specialist.collector import OpenCLIComputerUseCollector
from price_specialist.config import Settings
from price_specialist.database import configured_database, init_database
from price_specialist.enums import CollectionStatus, TaskType
from price_specialist.evidence import EvidenceStore
from price_specialist.models import DrugProduct, StoreResponsibility
from price_specialist.orchestrator import BatchOrchestrator
from price_specialist.schemas import BrowserSession, CollectionResult, CollectionTaskSpec, EvidenceBundle
from price_specialist.services import TaskQueueService
from export_fixture_run_csv import export_run


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "data/fixtures/业务知识库测试集/price_specialist_test.sqlite3"
PRIORITY_STORES = ("W00038", "W00001")
TECHNICAL_HOME = "https://shop163215406.taobao.com/"


def targets() -> list[sqlite3.Row]:
    with sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM store_drug_targets WHERE platform_code='taobao'").fetchall()
    return sorted(rows, key=lambda row: (0 if row["selection_reason"] == "technical_closed_loop_fixture" else 1,
                                         PRIORITY_STORES.index(row["store_id"]), row["brand"]))


async def main() -> None:
    settings = Settings.from_env(ROOT)
    engine, factory = configured_database(settings)
    init_database(engine)
    with factory() as db:
        queue = TaskQueueService(db)
        run = queue.create_run()
        collector = OpenCLIComputerUseCollector(settings)
        browser = BrowserSession(platform="taobao", alias="taobao-p0")
        health = await collector.health_check(browser)
        if health.collection_status != CollectionStatus.SUCCESS:
            raise RuntimeError(f"淘宝登录不可用: {health.collection_status.value}")

        attempts: list[dict[str, str]] = []
        chosen: CollectionTaskSpec | None = None
        for row in targets():
            drug = db.scalar(select(DrugProduct).where(DrugProduct.brand_name == row["brand"]))
            if drug is None:
                drug = DrugProduct(brand_name=row["brand"], generic_name=row["generic_name"])
                db.add(drug); db.flush()
            store = db.scalar(select(StoreResponsibility).where(StoreResponsibility.internal_store_id == row["store_id"]))
            if store is None:
                store = StoreResponsibility(internal_store_id=row["store_id"], platform="taobao", shop_name=row["shop_name"], shop_status="正常", fixed_tier="observation_only")
                db.add(store); db.flush()
            query = f"{row['brand']} {row['generic_name']}"
            technical = row["selection_reason"] == "technical_closed_loop_fixture"
            probe = CollectionTaskSpec(task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao", task_type=TaskType.SEARCH,
                session_alias="taobao-p0", drug_name=row["brand"], generic_name=row["generic_name"], shop_name=row["shop_name"], query=query)
            resolved = ({"shop_home_url": TECHNICAL_HOME, "platform_store_key": "163215406",
                         "source": "technical_closed_loop_fixture"} if technical
                        else await collector.resolve_taobao_store_home(probe, browser))
            attempts.append({"store_id": row["store_id"], "shop_name": row["shop_name"], "query": query, "resolved": str(bool(resolved))})
            if not resolved:
                continue
            store.shop_home_url = resolved["shop_home_url"]
            store.platform_store_key = resolved["platform_store_key"]
            chosen = CollectionTaskSpec(task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao", task_type=TaskType.STORE_SEARCH,
                session_alias="taobao-p0", priority=10, drug_name=row["brand"], generic_name=row["generic_name"], shop_name=row["shop_name"], query=query,
                metadata={"drug_id": drug.id, "target_brand": row["brand"], "route": "shop_home", "shop_home_url": store.shop_home_url,
                          "platform_store_key": store.platform_store_key, "homepage_discovery": resolved, "inspect_limit": 1,
                          "selection_reason": row["selection_reason"]})
            queue.enqueue(chosen)
            break
        if chosen is None:
            spec = CollectionTaskSpec(task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao", task_type=TaskType.SEARCH,
                session_alias="taobao-p0", query="淘宝店铺主页发现", metadata={"homepage_attempts": attempts})
            task = queue.enqueue(spec)
            queue.record_result(task, CollectionResult(collection_status=CollectionStatus.NOT_FOUND, error_code="store_home_not_found", error_detail="未确认真实店铺主页", evidence=EvidenceBundle(raw_fields={"homepage_attempts": attempts})), None)
            db.commit()
            print({"run_id": run.id, "status": "not_found", "attempts": attempts})
            return
        db.commit()
        outcome = await BatchOrchestrator(session=db, collector=collector, evidence_store=EvidenceStore(settings.evidence_dir), run_id=run.id).execute_all({"taobao": "taobao-p0"})
        output = ROOT / "artifacts/runs/current" / run.id
        export_run(run.id, output)
        print({"run_id": run.id, "store": chosen.shop_name, "outcome": outcome, "attempts": attempts, "csv_output": str(output)})


if __name__ == "__main__":
    asyncio.run(main())
