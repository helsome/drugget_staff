"""Tests for the test_runner module."""
from __future__ import annotations

import csv

import pytest
from sqlalchemy import select

from export_fixture_run_csv import export_run
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.enums import TaskType
from price_specialist.models import CollectionRun, CollectionTask, DrugProduct
from price_specialist.services import TaskQueueService
from price_specialist.test_runner import TestRunConfig, TestWorker, ProgressUpdate


class TestTestRunConfig:
    def test_default_values(self) -> None:
        cfg = TestRunConfig(drugs=["托妥"], platforms=["taobao"])
        assert cfg.search_limit == 5
        assert cfg.max_candidates == 3
        assert cfg.inspect_limit == 3
        assert cfg.search_modes == ["global_search", "store_search"]
        assert cfg.use_test_db is True

    def test_custom_values(self) -> None:
        cfg = TestRunConfig(
            drugs=["托妥", "依伦平"],
            platforms=["yaoshibang"],
            search_modes=["global_search"],
            search_limit=10,
            max_candidates=5,
            use_test_db=False,
        )
        assert cfg.search_limit == 10
        assert cfg.max_candidates == 5
        assert cfg.search_modes == ["global_search"]
        assert cfg.use_test_db is False

    def test_empty_drugs(self) -> None:
        cfg = TestRunConfig(drugs=[], platforms=["taobao"])
        assert cfg.drugs == []


class TestProgressUpdate:
    def test_default_values(self) -> None:
        update = ProgressUpdate()
        assert update.timestamp == ""
        assert update.status == ""
        assert update.platform_total == 0
        assert update.platform_completed == 0

    def test_custom_values(self) -> None:
        update = ProgressUpdate(
            timestamp="10:00:00",
            run_id="abc-123",
            platform="taobao",
            phase="search",
            status="success",
            message="测试完成",
            drug_name="托妥",
            platform_total=5,
            platform_completed=3,
        )
        assert update.platform == "taobao"
        assert update.phase == "search"
        assert update.status == "success"
        assert update.platform_total == 5
        assert update.platform_completed == 3


@pytest.mark.asyncio
async def test_workbench_enqueues_catalog_drug_with_candidate_metadata(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runner.sqlite3'}")
    init_database(engine)
    factory = make_session_factory(engine)
    config = TestRunConfig(
        drugs=["米拉贝隆缓释片"],
        platforms=["yaoshibang"],
        search_modes=["global_search"],
        search_limit=7,
        max_candidates=2,
        inspect_limit=1,
    )
    worker = TestWorker(config, tmp_path)

    with factory() as db:
        queue = TaskQueueService(db)
        run = queue.create_run()
        await worker._enqueue_platform_tasks(db, queue, run.id, "yaoshibang", config)
        db.commit()

        task = db.scalar(select(CollectionTask).where(CollectionTask.run_id == run.id))
        drug = db.scalar(select(DrugProduct).where(DrugProduct.brand_name == "晴诺舒"))

        assert task is not None
        assert drug is not None
        assert task.task_type == TaskType.SEARCH.value
        assert task.payload["drug_name"] == "晴诺舒"
        assert task.payload["generic_name"] == "米拉贝隆缓释片"
        assert task.payload["metadata"] == {
            "drug_id": drug.id,
            "target_brand": "晴诺舒",
            "search_limit": 7,
            "candidate_limit": 2,
            "inspect_limit": 1,
            "source": "test_workbench",
            "route": "global",
        }


def test_export_contains_only_run_outputs_and_keeps_empty_headers(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "export.sqlite3"
    monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", f"sqlite:///{database_path}")
    engine = create_db_engine(f"sqlite:///{database_path}")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        db.add(CollectionRun(id="export-run"))
        db.commit()

    output = tmp_path / "csv"
    export_run("export-run", output, test_mode=True)

    assert not list(output.glob("fixture_*.csv"))
    with (output / "search_candidates.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert "候选类型" in next(csv.reader(handle))


def test_export_progress_exposes_clickable_output_path() -> None:
    update = ProgressUpdate(phase="export", status="success", output_path="/tmp/run-output")
    assert update.output_path == "/tmp/run-output"
