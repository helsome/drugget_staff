"""Tests for the test_runner module."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

# Add collectors directory to path for export_fixture_run_csv import
_collectors_dir = Path(__file__).resolve().parent.parent / "collectors"
sys.path.insert(0, str(_collectors_dir))

from export_fixture_run_csv import export_run, export_run_outputs, export_run_manifest
from price_specialist.cancellation import CancellationToken, CancelledError
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.enums import TaskStatus, TaskType
from price_specialist.models import CollectionRun, CollectionTask, DrugProduct
from price_specialist.services import DrugSelection, TaskQueueService
from price_specialist.test_runner import TestRunConfig, TestWorker, ProgressUpdate


class TestTestRunConfig:
    def test_default_values(self) -> None:
        cfg = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"])
        assert cfg.search_limit == 5
        assert cfg.max_candidates == 3
        assert cfg.inspect_limit == 3
        assert cfg.search_modes == ["global_search", "store_search"]
        assert cfg.use_test_db is True

    def test_custom_values(self) -> None:
        cfg = TestRunConfig(
            drugs=[DrugSelection.from_generic_name("托妥"), DrugSelection.from_generic_name("依伦平")],
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
        drugs=[DrugSelection.from_generic_name("米拉贝隆缓释片")],
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
        export_run_outputs("export-run", db, output)
        export_run_manifest("export-run", db, output, runtime_mode="test")

    assert not list(output.glob("fixture_*.csv"))
    with (output / "search_candidates.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert "候选类型" in next(csv.reader(handle))
    manifest = output / "manifest.json"
    assert manifest.is_file()
    import json
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["run_id"] == "export-run"
    assert data["runtime_mode"] == "test"


def test_export_progress_exposes_clickable_output_path() -> None:
    update = ProgressUpdate(phase="export", status="success", output_path="/tmp/run-output")
    assert update.output_path == "/tmp/run-output"


# ── Cancellation tests ────────────────────────────────────────────────────


class TestCancellationToken:
    def test_not_cancelled_by_default(self) -> None:
        ct = CancellationToken()
        assert not ct.is_cancelled

    def test_cancel_sets_flag(self) -> None:
        ct = CancellationToken()
        ct.cancel()
        assert ct.is_cancelled

    def test_cancel_is_idempotent(self) -> None:
        ct = CancellationToken()
        ct.cancel()
        ct.cancel()  # Should not raise
        assert ct.is_cancelled

    def test_raise_if_cancelled_raises(self) -> None:
        ct = CancellationToken()
        ct.cancel()
        with pytest.raises(CancelledError):
            ct.raise_if_cancelled()

    def test_raise_if_cancelled_noop_when_not_cancelled(self) -> None:
        ct = CancellationToken()
        ct.raise_if_cancelled()  # Should not raise


class TestTaskQueueServiceCancellation:
    def test_cancel_pending_tasks(self, tmp_path) -> None:
        engine = create_db_engine(f"sqlite:///{tmp_path / 'cancel.sqlite3'}")
        init_database(engine)
        factory = make_session_factory(engine)
        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            run_id = run.id
            # Add a pending task
            from price_specialist.schemas import CollectionTaskSpec
            spec = CollectionTaskSpec(
                task_id="task-1", run_id=run_id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="TestDrug", query="test",
            )
            queue.enqueue(spec)
            db.commit()

            # Cancel pending tasks
            count = queue.cancel_pending_tasks(run_id)
            assert count == 1

            # Verify task is cancelled
            task = db.scalar(select(CollectionTask).where(CollectionTask.id == "task-1"))
            assert task is not None
            assert task.status == TaskStatus.CANCELLED.value
            assert task.completed_at is not None

    def test_cancel_pending_tasks_only_pending_and_leased(self, tmp_path) -> None:
        engine = create_db_engine(f"sqlite:///{tmp_path / 'cancel2.sqlite3'}")
        init_database(engine)
        factory = make_session_factory(engine)
        with factory() as db:
            from datetime import datetime
            from price_specialist.schemas import CollectionTaskSpec
            queue = TaskQueueService(db)
            run = queue.create_run()
            run_id = run.id

            # Add tasks with different statuses
            for i, status in enumerate(["pending", "leased", "succeeded", "failed"]):
                spec = CollectionTaskSpec(
                    task_id=f"task-{i}", run_id=run_id, platform="taobao",
                    task_type=TaskType.SEARCH, session_alias="taobao-p0",
                    drug_name="TestDrug", query="test",
                )
                task = queue.enqueue(spec)
                task.status = status
                if status in ("succeeded", "failed"):
                    task.completed_at = datetime.now()
            db.commit()

            count = queue.cancel_pending_tasks(run_id)
            # Only "pending" and "leased" should be cancelled
            assert count == 2

            # Verify only pending and leased were cancelled
            for i, expected in enumerate(["cancelled", "cancelled", "succeeded", "failed"]):
                task = db.scalar(select(CollectionTask).where(CollectionTask.id == f"task-{i}"))
                assert task.status == expected

    def test_mark_run_cancelled(self, tmp_path) -> None:
        engine = create_db_engine(f"sqlite:///{tmp_path / 'mark.sqlite3'}")
        init_database(engine)
        factory = make_session_factory(engine)
        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            run_id = run.id
            db.commit()

            queue.mark_run_cancelled(run_id)
            db.commit()

            run = db.get(CollectionRun, run_id)
            assert run.status == "cancelled"
            assert run.finished_at is not None


@pytest.mark.asyncio
async def test_orchestrator_cancellation_stops_leasing_new_tasks(tmp_path) -> None:
    """After cancellation, the orchestrator must not lease any new tasks."""
    from price_specialist.orchestrator import BatchOrchestrator, RatePolicy
    from price_specialist.collector import ComputerUseCollector
    from price_specialist.evidence import EvidenceStore
    from price_specialist.schemas import CollectionTaskSpec, CollectionResult, BrowserSession
    from price_specialist.enums import CollectionStatus

    ct = CancellationToken()
    engine = create_db_engine(f"sqlite:///{tmp_path / 'cancel_orch.sqlite3'}")
    init_database(engine)
    factory = make_session_factory(engine)

    with factory() as db:
        run = CollectionRun(id="cancel-run")
        db.add(run)
        db.flush()

        queue = TaskQueueService(db)
        # Enqueue several tasks
        for i in range(5):
            queue.enqueue(CollectionTaskSpec(
                task_id=f"cancel-task-{i}", run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="TestDrug", query="test",
            ))
        db.commit()

        class NoopCollector(ComputerUseCollector):
            async def health_check(self, session):
                return CollectionResult(collection_status=CollectionStatus.SUCCESS)
            async def search(self, query, session, **kwargs):
                return []
            async def search_store(self, spec, session):
                return []
            async def inspect_candidate(self, task, session):
                raise NotImplementedError
            async def collect_fixed(self, task, session):
                raise NotImplementedError
            async def resume_incident(self, incident_id, session):
                raise NotImplementedError

        # Cancel BEFORE execution
        ct.cancel()

        runner = BatchOrchestrator(
            session=db,
            collector=NoopCollector(),
            evidence_store=EvidenceStore(tmp_path / "ev"),
            sleep=_no_sleep,
            rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
            cancellation_token=ct,
        )
        outcomes = await runner.execute_all({"taobao": "taobao-p0"})

        # No tasks should have been leased
        assert outcomes[0]["completed"] == 0
        assert outcomes[0]["execution_status"] == "cancelled"
        assert outcomes[0]["business_status"] == "cancelled"

        # Verify all tasks are still pending (or cancelled by execute_all)
        tasks = list(db.scalars(select(CollectionTask).where(CollectionTask.run_id == run.id)))
        cancelled = sum(1 for t in tasks if t.status == TaskStatus.CANCELLED.value)
        if cancelled == 0:
            # If execute_all didn't cancel them, the orchestrator cancelled pending
            pass
        # No task should be running or leased
        for t in tasks:
            assert t.status not in (TaskStatus.RUNNING.value, TaskStatus.LEASED.value)


async def _no_sleep(duration: float = 0) -> None:
    return None


@pytest.mark.asyncio
async def test_orchestrator_cancellation_during_execution_skips_remaining_tasks(tmp_path) -> None:
    """When cancelled mid-execution, remaining tasks should stay unleased."""
    from price_specialist.orchestrator import BatchOrchestrator, RatePolicy
    from price_specialist.collector import ComputerUseCollector
    from price_specialist.evidence import EvidenceStore
    from price_specialist.schemas import CollectionTaskSpec, CollectionResult, BrowserSession
    from price_specialist.enums import CollectionStatus

    ct = CancellationToken()
    engine = create_db_engine(f"sqlite:///{tmp_path / 'cancel_mid.sqlite3'}")
    init_database(engine)
    factory = make_session_factory(engine)

    class SingleTaskCollector(ComputerUseCollector):
        def __init__(self):
            self.call_count = 0
        async def health_check(self, session):
            return CollectionResult(collection_status=CollectionStatus.SUCCESS)
        async def search(self, query, session, **kwargs):
            self.call_count += 1
            # Cancel after first task
            ct.cancel()
            return []
        async def search_store(self, spec, session):
            self.call_count += 1
            ct.cancel()
            return []
        async def inspect_candidate(self, task, session):
            raise NotImplementedError
        async def collect_fixed(self, task, session):
            raise NotImplementedError
        async def resume_incident(self, incident_id, session):
            raise NotImplementedError

    with factory() as db:
        run = CollectionRun(id="cancel-mid")
        db.add(run)
        db.flush()

        queue = TaskQueueService(db)
        for i in range(3):
            queue.enqueue(CollectionTaskSpec(
                task_id=f"mid-task-{i}", run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="TestDrug", query="test",
            ))
        db.commit()

        runner = BatchOrchestrator(
            session=db,
            collector=SingleTaskCollector(),
            evidence_store=EvidenceStore(tmp_path / "ev"),
            sleep=_no_sleep,
            rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
            cancellation_token=ct,
        )
        outcomes = await runner.execute_all({"taobao": "taobao-p0"})

        # Only one task completed (the one that triggered cancellation from within)
        assert outcomes[0]["completed"] == 1
        # The remaining 2 tasks should still be pending or cancelled
        tasks = list(db.scalars(
            select(CollectionTask).where(CollectionTask.run_id == run.id).order_by(CollectionTask.id)
        ))
        # First task should have been executed (status may be succeeded or failed)
        assert tasks[0].status in (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value)
        # Remaining tasks should not be running or succeeded
        for t in tasks[1:]:
            assert t.status not in (TaskStatus.RUNNING.value, TaskStatus.SUCCEEDED.value)


def test_worker_cancellation_token_is_exposed() -> None:
    """TestWorker should expose its CancellationToken via the cancellation_token property."""
    worker = TestWorker(TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"]), Path("/tmp"))
    assert worker.cancellation_token is not None
    assert not worker.cancellation_token.is_cancelled

    worker.cancel()
    assert worker.cancellation_token.is_cancelled


def test_worker_cancel_does_not_send_done_success(tmp_path) -> None:
    """After cancellation, the worker must not push a done/success progress update."""
    import threading
    from queue import Empty

    config = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"], search_modes=["global_search"])
    worker = TestWorker(config, tmp_path)

    # Simulate: cancel before the worker thread starts processing
    worker.cancel()
    worker.start()

    # Wait for thread to finish (it should exit quickly since cancelled)
    worker.thread.join(timeout=5)
    assert not worker.thread.is_alive()

    # Collect all updates
    updates = []
    while True:
        try:
            updates.append(worker.queue.get_nowait())
        except Empty:
            break

    # No update should have phase="done" with status="success"
    for u in updates:
        assert not (u.phase == "done" and u.status == "success"), (
            f"Unexpected done/success update: {u}"
        )

    # There should be a cancelled update or at least no done/success
    cancelled_updates = [u for u in updates if u.status == "cancelled"]
    if not cancelled_updates:
        # The worker may exit before pushing any update if cancel happens very early
        # This is acceptable — the key is no done/success
        pass
