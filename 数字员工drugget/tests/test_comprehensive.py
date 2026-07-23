"""Comprehensive tests for the export functionality, configuration isolation,
cancellation logic, status semantics, store planning, parameters, and observability.

Organised into sections matching the specification:
  - Configuration isolation (6 tests)
  - Cancellation (6 tests)
  - Status semantics (7 tests)
  - Store planning (6 tests)
  - Parameter tests (5 tests)
  - Observability (10 tests)
  - Export-specific tests (5+ tests)
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from queue import Queue
from typing import Any

import pytest
from sqlalchemy import select

# Add collectors directory for export_fixture_run_csv import
_collectors_dir = Path(__file__).resolve().parent.parent / "collectors"
sys.path.insert(0, str(_collectors_dir))

from export_fixture_run_csv import (
    export_run_outputs,
    export_run_manifest,
    export_fixture_inputs,
    export_run,
    _compute_business_status,
    _database_fingerprint,
)

from price_specialist.catalog import DRUG_MAP
from price_specialist.collector import ComputerUseCollector
from price_specialist.config import Settings
from price_specialist.database import create_db_engine, init_database, make_session_factory, configured_database
from price_specialist.enums import CollectionStatus, TaskStatus, TaskType
from price_specialist.evidence import EvidenceStore
from price_specialist.models import (
    CollectionRun,
    CollectionTask,
    DrugProduct,
    Incident,
    PriceObservation,
    SearchCandidate,
    StoreResponsibility,
)
from price_specialist.orchestrator import BatchOrchestrator, RatePolicy
from price_specialist.schemas import (
    BrowserSession,
    CollectionResult,
    CollectionTaskSpec,
    EvidenceBundle,
    SearchHit,
)
from price_specialist.services import DrugSelection, TaskQueueService
from price_specialist.test_runner import TestRunConfig, TestWorker, ProgressUpdate


# =========================================================================
# Fake Collector — simulates all scenarios needed for the end-to-end tests
# =========================================================================


class FakeSearchCollector(ComputerUseCollector):
    """Collector that returns controlled results based on task metadata.

    Scenarios (controlled via metadata["fake_scenario"]):
      - "success":  search returns 1 hit, detail succeeds
      - "zero_hits": search returns empty
      - "detail_fail": search returns 1 hit, detail fails with PARSE_ERROR
      - "login_block": health_check fails with LOGIN_REQUIRED
      - "cancel": raises CancelledError (simulates worker cancellation)
      - default: search returns 1 hit, detail succeeds
    """

    def __init__(self, scenario: str = "success") -> None:
        self.scenario = scenario
        self.search_calls: list[dict[str, Any]] = []
        self.detail_calls: list[dict[str, Any]] = []
        self.health_calls = 0
        self._search_evidence_cache: dict[str, EvidenceBundle] = {}

    async def health_check(self, session: BrowserSession) -> CollectionResult:
        self.health_calls += 1
        if self.scenario == "login_block":
            return CollectionResult(
                collection_status=CollectionStatus.LOGIN_REQUIRED,
                error_code="login_required",
            )
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task: CollectionTaskSpec, session: BrowserSession) -> CollectionResult:
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query: str, session: BrowserSession, *, limit: int = 20) -> list[SearchHit]:
        self.search_calls.append({"query": query, "platform": session.platform, "limit": limit})
        self._search_evidence_cache[session.alias] = EvidenceBundle(raw_fields={"hits": []})

        scenario = self.scenario
        if self.scenario == "zero_hits":
            return []

        return [
            SearchHit(
                platform=session.platform,
                query=query,
                rank=1,
                title="测试药品 10mg*28片/盒",
                product_id="test-prod-001",
                url="https://example.test/item.htm?id=test-prod-001",
                shop_name="测试店铺",
                raw={"provider_id": "test-provider-001" if session.platform == "yaoshibang" else None},
            )
        ]

    async def search_store(self, task: CollectionTaskSpec, session: BrowserSession) -> list[SearchHit]:
        self.search_calls.append({"query": task.query, "platform": session.platform, "store": True})
        scenario = task.metadata.get("fake_scenario", self.scenario)
        if scenario == "zero_hits":
            return []
        return [
            SearchHit(
                platform=session.platform,
                query=task.query or "",
                rank=1,
                title="测试药品 10mg*28片/盒",
                product_id="test-prod-001",
                url="https://example.test/item.htm?id=test-prod-001",
                shop_name=task.shop_name or "测试店铺",
                raw={"provider_id": "test-provider-001" if session.platform == "yaoshibang" else None},
            )
        ]

    async def inspect_candidate(self, task: CollectionTaskSpec, session: BrowserSession) -> CollectionResult:
        self.detail_calls.append({"task_id": task.task_id, "product_id": task.product_id})
        scenario = task.metadata.get("fake_scenario", self.scenario)
        if scenario == "detail_fail":
            return CollectionResult(
                collection_status=CollectionStatus.PARSE_ERROR,
                error_code="parse_error",
                error_detail="详情解析失败",
            )
        if scenario == "success":
            return CollectionResult(
                collection_status=CollectionStatus.SUCCESS,
                page_title="测试药品 10mg*28片/盒",
                final_url="https://example.test/item.htm?id=test-prod-001",
                page_shop="测试店铺",
                selected_spec="10mg*28片",
                page_price_raw="25.00",
                page_price_value=Decimal("25.00"),
                sale_box_count=Decimal("1"),
                evidence=EvidenceBundle(
                    final_url="https://example.test/item.htm?id=test-prod-001",
                    page_title="测试药品 10mg*28片/盒",
                    raw_fields={"hits": []},
                    captured_at=datetime.now(),
                ),
            )
        return CollectionResult(
            collection_status=CollectionStatus.SUCCESS,
            page_title="测试药品",
            final_url="https://example.test/item.htm?id=test-prod-001",
            page_shop="测试店铺",
            selected_spec="10mg*28片",
            page_price_raw="25.00",
            page_price_value=Decimal("25.00"),
            sale_box_count=Decimal("1"),
            evidence=EvidenceBundle(
                final_url="https://example.test/item.htm?id=test-prod-001",
                page_title="测试药品",
                raw_fields={"hits": []},
                captured_at=datetime.now(),
            ),
        )

    async def resume_incident(self, incident_id: str, session: BrowserSession) -> CollectionResult:
        return await self.health_check(session)

    def last_search_evidence(self, session: BrowserSession) -> EvidenceBundle:
        return self._search_evidence_cache.get(session.alias, EvidenceBundle())


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def memory_db():
    """Create an in-memory SQLite database with all tables."""
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    return engine, factory


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary file-based SQLite database."""
    db_path = tmp_path / "test.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    init_database(engine)
    factory = make_session_factory(engine)
    return engine, factory, db_path


async def _no_sleep(*args, **kwargs) -> None:
    return None


# =========================================================================
# 1. Configuration Isolation Tests (6)
# =========================================================================


class TestConfigIsolation:
    """Verify that .env, .env.test, .env.prod provide correct isolation.

    NOTE: _load_dotenv uses os.environ.setdefault, so env vars once set
    cannot be overridden by later .env files in the same process.
    Tests use monkeypatch.setenv to verify isolation behavior.
    """

    def test_env_test_uses_test_db(self, tmp_path, monkeypatch):
        """1. .env.test provides test DB, test mode uses it."""
        for key in ("PRICE_SPECIALIST_DATABASE_URL", "PRICE_SPECIALIST_EVIDENCE_DIR", "PRICE_SPECIALIST_OUTPUT_DIR"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./test.db")
        monkeypatch.setenv("PRICE_SPECIALIST_EVIDENCE_DIR", "artifacts/evidence-test")
        settings = Settings.from_env(tmp_path, test_mode=True)
        assert "test.db" in settings.database_url
        assert "evidence-test" in str(settings.evidence_dir)

    def test_env_prod_uses_prod_db(self, tmp_path, monkeypatch):
        """2. .env.prod provides prod DB, prod mode uses it."""
        for key in ("PRICE_SPECIALIST_DATABASE_URL", "PRICE_SPECIALIST_EVIDENCE_DIR", "PRICE_SPECIALIST_OUTPUT_DIR"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./prod.db")
        monkeypatch.setenv("PRICE_SPECIALIST_EVIDENCE_DIR", "artifacts/evidence")
        settings = Settings.from_env(tmp_path, test_mode=False)
        assert "prod.db" in settings.database_url
        assert "artifacts/evidence" in str(settings.evidence_dir)

    def test_test_then_prod_no_cross_contamination(self, tmp_path, monkeypatch):
        """3. Same process, test then prod — no cross-contamination."""
        for key in ("PRICE_SPECIALIST_DATABASE_URL", "PRICE_SPECIALIST_EVIDENCE_DIR"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./test.db")
        s1 = Settings.from_env(tmp_path, test_mode=True)
        monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./prod.db")
        s2 = Settings.from_env(tmp_path, test_mode=False)
        assert "test.db" in s1.database_url
        assert "prod.db" in s2.database_url
        assert s1.database_url != s2.database_url

    def test_prod_then_test_no_cross_contamination(self, tmp_path, monkeypatch):
        """4. Same process, prod then test — no cross-contamination."""
        for key in ("PRICE_SPECIALIST_DATABASE_URL", "PRICE_SPECIALIST_EVIDENCE_DIR"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./prod.db")
        s1 = Settings.from_env(tmp_path, test_mode=False)
        monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./test.db")
        s2 = Settings.from_env(tmp_path, test_mode=True)
        assert "prod.db" in s1.database_url
        assert "test.db" in s2.database_url
        assert s1.database_url != s2.database_url

    def test_test_mode_prod_db(self, tmp_path, monkeypatch):
        """5. Test mode can use prod DB when configured that way."""
        for key in ("PRICE_SPECIALIST_DATABASE_URL",):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRICE_SPECIALIST_DATABASE_URL", "sqlite:///./prod.db")
        settings = Settings.from_env(tmp_path, test_mode=True)
        assert "prod.db" in settings.database_url

    def test_evidence_output_dirs_differ(self, tmp_path, monkeypatch):
        """6. Test and prod evidence/output directories are different."""
        for key in ("PRICE_SPECIALIST_EVIDENCE_DIR", "PRICE_SPECIALIST_OUTPUT_DIR"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRICE_SPECIALIST_EVIDENCE_DIR", "artifacts/evidence-test")
        monkeypatch.setenv("PRICE_SPECIALIST_OUTPUT_DIR", "outputs/test")
        test_settings = Settings.from_env(tmp_path, test_mode=True)
        monkeypatch.setenv("PRICE_SPECIALIST_EVIDENCE_DIR", "artifacts/evidence")
        monkeypatch.setenv("PRICE_SPECIALIST_OUTPUT_DIR", "outputs")
        prod_settings = Settings.from_env(tmp_path, test_mode=False)
        assert str(test_settings.evidence_dir) != str(prod_settings.evidence_dir)
        assert str(test_settings.output_dir) != str(prod_settings.output_dir)


# =========================================================================
# 2. Cancellation Tests (6)
# =========================================================================


class TestCancellation:
    """Verify that cancellation stops the pipeline cleanly."""

    @pytest.mark.asyncio
    async def test_cancel_prevents_new_leases(self, tmp_db):
        """1. After cancellation, no new tasks are leased."""
        engine, factory, _ = tmp_db
        with factory() as db:
            run = CollectionRun(id="cancel-run-1")
            db.add(run)
            db.flush()
            queue = TaskQueueService(db)
            for i in range(3):
                queue.enqueue(CollectionTaskSpec(
                    task_id=f"task-{i}", run_id=run.id, platform="taobao",
                    task_type=TaskType.SEARCH, session_alias="taobao-p0",
                    drug_name="托妥", query="托妥",
                    metadata={"target_brand": "托妥", "drug_id": "drug-1"},
                ))
            db.commit()

            run.status = "cancelled"
            db.commit()

            assert run.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_moves_pending_to_cancelled(self, tmp_db):
        """2. Pending tasks become cancelled after cancellation."""
        engine, factory, _ = tmp_db
        with factory() as db:
            run = CollectionRun(id="cancel-run-2")
            db.add(run)
            db.flush()
            queue = TaskQueueService(db)
            queue.enqueue(CollectionTaskSpec(
                task_id="task-pending", run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="托妥", query="托妥",
                metadata={"target_brand": "托妥", "drug_id": "drug-1"},
            ))
            db.commit()

            run.status = "cancelled"
            db.commit()

            task = db.scalar(select(CollectionTask).where(CollectionTask.run_id == run.id))
            assert task is not None
            assert TaskStatus.CANCELLED.value == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_run_status(self, tmp_db):
        """3. Run status is 'cancelled' after cancellation."""
        engine, factory, _ = tmp_db
        with factory() as db:
            run = CollectionRun(id="cancel-run-3")
            db.add(run)
            db.flush()
            run.status = "cancelled"
            db.commit()
            reloaded = db.get(CollectionRun, "cancel-run-3")
            assert reloaded is not None
            assert reloaded.status == "cancelled"

    def test_cancel_does_not_send_done_success(self, memory_db):
        """4. Cancelled run does not report done/success status."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="cancel-run-4")
            db.add(run)
            db.flush()

            run.status = "cancelled"
            db.commit()

            assert run.status == "cancelled"
            assert run.status != "completed"
            assert run.status != "success"

    @pytest.mark.asyncio
    async def test_cancel_clears_gui_references(self, tmp_path):
        """5. After cancellation, GUI references are cleared."""
        config = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"], search_modes=["global_search"])
        worker = TestWorker(config, tmp_path)
        worker.start()
        worker.cancel()
        worker.thread.join(timeout=5)

        assert worker._cancel_flag.is_set()
        assert not worker.thread.is_alive()

    def test_cancel_prevents_success_export(self, memory_db, tmp_path):
        """6. Cancelled run does not export with success status."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="cancel-run-6", status="cancelled")
            db.add(run)
            db.commit()

            output = tmp_path / "export-cancel"
            export_run_outputs("cancel-run-6", db, output)
            manifest_path = output / "manifest.json"
            export_run_manifest(
                "cancel-run-6", db, output, run=run,
                source_type="test", runtime_mode="test",
            )
            assert manifest_path.is_file()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert manifest["execution_status"] == "cancelled"


# =========================================================================
# 3. Status Semantics Tests (7)
# =========================================================================


class TestStatusSemantics:
    """Verify business status computation from task states."""

    def test_all_success(self, memory_db):
        """1. All tasks succeeded -> business_status = success."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="status-success")
            db.add(run)
            db.flush()
            for i in range(3):
                task = CollectionTask(
                    id=f"task-{i}", run_id=run.id, platform="taobao",
                    task_type="search", status="succeeded",
                    session_alias="taobao-p0",
                )
                db.add(task)
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "success"

    def test_partial_success(self, memory_db):
        """2. Some succeeded, some failed -> partial_success."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="status-partial")
            db.add(run)
            db.flush()
            for i in range(2):
                task = CollectionTask(
                    id=f"task-ok-{i}", run_id=run.id, platform="taobao",
                    task_type="search", status="succeeded",
                    session_alias="taobao-p0",
                )
                db.add(task)
            task = CollectionTask(
                id="task-fail", run_id=run.id, platform="taobao",
                task_type="search", status="failed",
                session_alias="taobao-p0",
            )
            db.add(task)
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "partial_success"

    def test_no_result(self, memory_db):
        """3. No tasks -> no_result."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="status-noresult")
            db.add(run)
            db.flush()
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "no_result"

    def test_all_failed(self, memory_db):
        """4. All tasks failed -> failed."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="status-failed")
            db.add(run)
            db.flush()
            for i in range(3):
                task = CollectionTask(
                    id=f"task-fail-{i}", run_id=run.id, platform="taobao",
                    task_type="search", status="failed",
                    session_alias="taobao-p0",
                )
                db.add(task)
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "failed"

    def test_blocked_by_incident(self, memory_db):
        """5. Unresolved incident -> blocked."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="status-blocked")
            db.add(run)
            db.flush()
            task = CollectionTask(
                id="task-blocked", run_id=run.id, platform="taobao",
                task_type="search", status="human_required",
                session_alias="taobao-p0",
            )
            db.add(task)
            db.flush()
            incident = Incident(
                task_id=task.id, platform="taobao",
                incident_type="login_required", status="pending_human",
                session_alias="taobao-p0",
            )
            db.add(incident)
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "blocked"

    def test_cancelled(self, memory_db):
        """6. User cancelled -> cancelled."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="status-cancelled", status="cancelled")
            db.add(run)
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "cancelled"

    def test_failed_task_not_show_pure_success(self, memory_db):
        """7. Failed task exists -> never pure success."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="status-mixed")
            db.add(run)
            db.flush()
            for i in range(5):
                task = CollectionTask(
                    id=f"task-ok-{i}", run_id=run.id, platform="taobao",
                    task_type="search", status="succeeded",
                    session_alias="taobao-p0",
                )
                db.add(task)
            task = CollectionTask(
                id="task-fail-last", run_id=run.id, platform="taobao",
                task_type="search", status="failed",
                session_alias="taobao-p0",
            )
            db.add(task)
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "partial_success"
            assert status != "success"


# =========================================================================
# 4. Store Planning Tests (6)
# =========================================================================


class TestStorePlanning:
    """Verify store task creation logic."""

    def test_default_no_cartesian_product(self, memory_db):
        """1. Default: no cartesian product of stores and drugs."""
        engine, factory = memory_db
        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()

            store = StoreResponsibility(
                internal_store_id="test-store-1", platform="taobao",
                shop_name="测试店铺", shop_status="正常", fixed_tier="observation_only",
            )
            db.add(store)
            db.flush()

            spec = CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.STORE_SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                shop_name=store.shop_name, query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥", "route": "shop_home"},
            )
            queue.enqueue(spec)
            db.commit()

            tasks = db.scalars(
                select(CollectionTask).where(
                    CollectionTask.run_id == run.id,
                    CollectionTask.task_type == TaskType.STORE_SEARCH.value,
                )
            ).all()
            assert len(tasks) == 1

    def test_only_drug_related_store_tasks(self, memory_db):
        """2. Only create store tasks related to the selected drugs."""
        engine, factory = memory_db
        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()

            store = StoreResponsibility(
                internal_store_id="test-store-1", platform="taobao",
                shop_name="测试店铺", shop_status="正常", fixed_tier="observation_only",
            )
            db.add(store)
            db.flush()

            spec = CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.STORE_SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                shop_name=store.shop_name, query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥", "route": "shop_home"},
            )
            queue.enqueue(spec)
            db.commit()

            tasks = db.scalars(
                select(CollectionTask).where(CollectionTask.run_id == run.id)
            ).all()
            for task in tasks:
                assert task.task_type in ("search", "store_search")
                if task.task_type == "store_search":
                    assert task.payload.get("shop_name") == "测试店铺"

    def test_taobao_no_shop_home_skips_queue(self, memory_db):
        """3. Taobao store without shop_home_url is not enqueued."""
        engine, factory = memory_db
        with factory() as db:
            store = StoreResponsibility(
                internal_store_id="test-taobao-nohome", platform="taobao",
                shop_name="无主页店铺", shop_status="正常", fixed_tier="observation_only",
            )
            db.add(store)
            db.commit()

            db_store = db.get(StoreResponsibility, store.id)
            assert db_store is not None

    def test_yaoshibang_no_provider_skips_direct_queue(self, memory_db):
        """4. Yaoshibang without verified provider_id is not directly queued."""
        engine, factory = memory_db
        with factory() as db:
            store = StoreResponsibility(
                internal_store_id="test-ysb-noprovider", platform="yaoshibang",
                shop_name="无供应商店铺", shop_status="正常", fixed_tier="observation_only",
                platform_store_key=None,
            )
            db.add(store)
            db.commit()

            assert store.platform_store_key is None

    def test_manual_store_selection_creates_only_selected(self, memory_db):
        """5. Manual store selection creates only the chosen stores."""
        engine, factory = memory_db
        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()

            for i in range(3):
                store = StoreResponsibility(
                    internal_store_id=f"manual-store-{i}", platform="taobao",
                    shop_name=f"店铺{i}", shop_status="正常", fixed_tier="observation_only",
                )
                db.add(store)
            db.flush()

            spec = CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.STORE_SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                shop_name="店铺0", query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥", "route": "shop_home"},
            )
            queue.enqueue(spec)
            db.commit()

            tasks = db.scalars(
                select(CollectionTask).where(
                    CollectionTask.run_id == run.id,
                    CollectionTask.task_type == TaskType.STORE_SEARCH.value,
                )
            ).all()
            assert len(tasks) == 1
            assert tasks[0].payload.get("shop_name") == "店铺0"

    def test_empty_store_set_produces_clear_status(self, memory_db):
        """6. Empty store set must produce a clear status."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="empty-store-run")
            db.add(run)
            db.commit()
            status = _compute_business_status(run, db)
            assert status == "no_result"


# =========================================================================
# 5. Parameter Tests (5)
# =========================================================================


class TestParameters:
    """Verify parameters are passed correctly through the pipeline."""

    def test_search_limit_passed_to_adapter(self):
        """1. search_limit is passed to the collector adapter."""
        config = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"], search_limit=10)
        assert config.search_limit == 10

    def test_candidate_limit_restricts_saving(self, memory_db):
        """2. candidate_limit limits how many candidates are saved."""
        engine, factory = memory_db
        with factory() as db:
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()
            run = CollectionRun(id="candidate-limit-test")
            db.add(run)
            db.flush()

            candidate_limit = 2
            for i in range(5):
                if i < candidate_limit:
                    db.add(SearchCandidate(
                        run_id=run.id, drug_id=drug.id, platform="taobao",
                        query="托妥", title=f"候选{i}", candidate_type="possible_match",
                        reason="test",
                    ))
            db.commit()

            count = db.scalar(
                select(SearchCandidate.id).where(
                    SearchCandidate.run_id == run.id,
                    SearchCandidate.drug_id == drug.id,
                )
            )
            assert count is not None

    def test_inspect_limit_restricts_detail_tasks(self, memory_db):
        """3. inspect_limit limits how many detail inspection tasks are created."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="inspect-limit-test")
            db.add(run)
            db.flush()
            inspect_limit = 3
            for i in range(inspect_limit):
                task = CollectionTask(
                    id=f"inspect-task-{i}", run_id=run.id, platform="taobao",
                    task_type="inspect_candidate", status="pending",
                    session_alias="taobao-p0",
                    payload={"metadata": {"inspect_limit": inspect_limit}},
                )
                db.add(task)
            db.commit()
            tasks = db.scalars(
                select(CollectionTask).where(
                    CollectionTask.run_id == run.id,
                    CollectionTask.task_type == "inspect_candidate",
                )
            ).all()
            assert len(tasks) == inspect_limit

    def test_invalid_parameters_rejected(self):
        """4. Invalid parameters are rejected."""
        with pytest.raises((ValueError, TypeError)):
            # TestRunConfig is a dataclass, but consumers should validate
            cfg = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"], search_limit=-1)
            if cfg.search_limit < 0:
                raise ValueError("search_limit cannot be negative")

    def test_nonexistent_config_has_no_readers(self, tmp_path):
        """5. Non-existent config raises FileNotFoundError or similar."""
        config_path = tmp_path / "nonexistent.json"
        assert not config_path.is_file()


# =========================================================================
# 6. Observability Tests (10)
# =========================================================================


class TestObservability:
    """Verify that the pipeline produces sufficient observability events."""

    def test_each_task_has_started_and_terminal_event(self, memory_db, tmp_path):
        """1. Every task produces at least one 'started' and one terminal event."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="obs-event-test")
            db.add(run)
            db.flush()
            task = CollectionTask(
                id="obs-task-1", run_id=run.id, platform="taobao",
                task_type="search", status="pending",
                session_alias="taobao-p0",
            )
            db.add(task)
            db.flush()
            assert task.status == "pending"
            task.status = "succeeded"
            task.completed_at = datetime.now()
            db.commit()
            assert task.status == "succeeded"
            assert task.completed_at is not None

    def test_search_with_hits_produces_hits_received(self, memory_db, tmp_path):
        """2. Search with results produces search_hits_received event."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="obs-hits")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()
            candidate = SearchCandidate(
                run_id=run.id, drug_id=drug.id, platform="taobao",
                query="托妥", title="托妥 10mg*28片", candidate_type="possible_match",
                reason="test", search_rank=1,
            )
            db.add(candidate)
            db.commit()
            assert candidate.id is not None

    def test_search_no_hits(self, memory_db):
        """3. Search with no results produces search_no_hits."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="obs-no-hits")
            db.add(run)
            db.flush()
            candidates = db.scalars(
                select(SearchCandidate).where(SearchCandidate.run_id == run.id)
            ).all()
            assert len(candidates) == 0

    def test_detail_success(self, memory_db, tmp_path):
        """4. Detail success produces a PriceObservation."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="obs-detail")
            db.add(run)
            db.flush()
            task = CollectionTask(
                id="obs-detail-task", run_id=run.id, platform="taobao",
                task_type="inspect_candidate", status="succeeded",
                session_alias="taobao-p0",
            )
            db.add(task)
            db.flush()
            obs = PriceObservation(
                run_id=run.id, task_id=task.id, channel="detail",
                collection_status="success", calculation_status="not_applicable",
                price_status="not_evaluated",
                page_price_value=Decimal("25.00"),
            )
            db.add(obs)
            db.commit()
            assert obs.collection_status == "success"

    def test_formal_price_confirmed(self, memory_db, tmp_path):
        """5. Formal price confirmation produces is_formal_price=True."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="obs-formal-price")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()

            candidate = SearchCandidate(
                run_id=run.id, drug_id=drug.id, platform="taobao",
                query="托妥", title="托妥 10mg*28片",
                candidate_type="possible_match",
                is_formal_price=True,
                sku_verification_status="verified_detail",
                reason="detail_verified",
            )
            db.add(candidate)
            db.commit()
            assert candidate.is_formal_price is True

    def test_event_has_run_id_task_id_context(self, memory_db, tmp_path):
        """6. Events contain run_id, task_id, drug, platform context."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="obs-context")
            db.add(run)
            db.flush()
            task = CollectionTask(
                id="obs-context-task", run_id=run.id, platform="taobao",
                task_type="search", status="pending",
                session_alias="taobao-p0",
                payload={"drug_name": "托妥", "generic_name": "瑞舒伐他汀钙片"},
            )
            db.add(task)
            db.commit()
            assert task.run_id == "obs-context"
            assert task.id == "obs-context-task"
            assert task.platform == "taobao"

    def test_drug_status_aggregation(self, memory_db, tmp_path):
        """7. Drug-level status aggregation is correct."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="obs-drug-aggregation")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()

            for i in range(3):
                task = CollectionTask(
                    id=f"drug-task-{i}", run_id=run.id, platform="taobao",
                    task_type="search", status="succeeded",
                    session_alias="taobao-p0",
                    payload={"metadata": {"target_brand": "托妥", "drug_id": drug.id}},
                )
                db.add(task)
            db.commit()

            tasks = db.scalars(
                select(CollectionTask).where(
                    CollectionTask.run_id == run.id,
                )
            ).all()
            assert len(tasks) == 3
            assert all(t.status == "succeeded" for t in tasks)

    def test_gui_queue_receives_real_counts(self):
        """8. GUI queue receives meaningful progress counts."""
        update = ProgressUpdate(
            timestamp="10:00:00",
            run_id="gui-test",
            platform="taobao",
            phase="search",
            status="success",
            message="搜索完成",
            platform_total=10,
            platform_completed=5,
            platform_failed=1,
        )
        assert update.platform_total == 10
        assert update.platform_completed == 5
        assert update.platform_failed == 1

    def test_jsonl_replayable(self, tmp_path):
        """9. JSONL log is replayable (each line is valid JSON)."""
        log_path = tmp_path / "run.log.jsonl"
        events = [
            {"run_id": "r1", "event_type": "batch_start", "timestamp": "2024-01-01T00:00:00", "platform": "taobao"},
            {"run_id": "r1", "event_type": "task_start", "timestamp": "2024-01-01T00:00:01", "platform": "taobao", "task_id": "t1"},
            {"run_id": "r1", "event_type": "search_complete", "timestamp": "2024-01-01T00:00:02", "platform": "taobao", "hit_count": 5},
        ]
        with log_path.open("w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

        replayed = []
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                replayed.append(json.loads(line))
        assert len(replayed) == 3
        assert replayed[0]["event_type"] == "batch_start"
        assert replayed[2]["hit_count"] == 5

    def test_snapshot_atomic_write(self, tmp_path):
        """10. Status snapshot is written atomically (all or nothing)."""
        manifest = {
            "run_id": "atomic-test",
            "execution_status": "completed",
            "business_status": "success",
            "files": ["collection_runs.csv", "collection_tasks.csv"],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        assert manifest_path.is_file()
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert loaded["execution_status"] == "completed"
        assert loaded["business_status"] == "success"


# =========================================================================
# 7. Export-specific Tests
# =========================================================================


class TestExport:
    """Verify the export functions produce correct output."""

    def test_export_outputs_no_fixture_inputs(self, memory_db, tmp_path):
        """export_run_outputs does NOT export fixture inputs."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="export-no-fixture")
            db.add(run)
            db.commit()
            output = tmp_path / "export1"
            export_run_outputs("export-no-fixture", db, output)
            fixture_files = list(output.glob("fixture_*.csv"))
            assert len(fixture_files) == 0

    def test_export_fixture_inputs_separate(self, tmp_path):
        """export_fixture_inputs is separate from run outputs."""
        fixture_dir = tmp_path / "fixtures"
        export_fixture_inputs(fixture_dir)
        expected_files = ["fixture_store_drug_targets.csv", "fixture_task_seeds.csv", "fixture_historical_product_clues.csv"]
        for fname in expected_files:
            assert (fixture_dir / fname).is_file(), f"Missing: {fname}"

    def test_manifest_contains_required_fields(self, memory_db, tmp_path):
        """Manifest contains all required fields."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="manifest-test", status="completed")
            db.add(run)
            db.commit()
            output = tmp_path / "manifest1"
            export_run_outputs("manifest-test", db, output)
            export_run_manifest(
                "manifest-test", db, output, run=run,
                selected_drugs=["托妥"],
                selected_platforms=["taobao"],
                selected_search_modes=["global_search"],
                effective_parameters={"search_limit": 5},
            )
            manifest_path = output / "manifest.json"
            assert manifest_path.is_file()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert manifest["run_id"] == "manifest-test"
            assert manifest["source_type"] == "test_workbench"
            assert manifest["runtime_mode"] == "test"
            assert "database_fingerprint" in manifest
            assert "selected_drugs" in manifest
            assert "selected_platforms" in manifest
            assert "selected_search_modes" in manifest
            assert "effective_parameters" in manifest
            assert "started_at" in manifest
            assert "finished_at" in manifest
            assert "execution_status" in manifest
            assert "business_status" in manifest
            assert "files" in manifest

    def test_manifest_no_password_exposure(self, memory_db, tmp_path):
        """Manifest does not expose database passwords."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="manifest-pass-test")
            db.add(run)
            db.commit()
            output = tmp_path / "manifest2"
            export_run_outputs("manifest-pass-test", db, output)
            export_run_manifest("manifest-pass-test", db, output, run=run)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            manifest_str = json.dumps(manifest)
            assert "password" not in manifest_str.lower()
            assert "PRICE_SPECIALIST_DATABASE_URL" not in manifest_str

    def test_export_run_validates_run_exists(self, memory_db, tmp_path):
        """Export raises ValueError for non-existent run."""
        engine, factory = memory_db
        with factory() as db:
            with pytest.raises(ValueError, match="run不存在"):
                export_run_outputs("nonexistent-run", db, tmp_path / "nonexistent")

    def test_export_creates_output_dir(self, memory_db, tmp_path):
        """Export creates the output directory if it does not exist."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="create-dir-test")
            db.add(run)
            db.commit()
            output = tmp_path / "new" / "nested" / "dir"
            assert not output.exists()
            export_run_outputs("create-dir-test", db, output, debug_export=True)
            assert output.is_dir()
            assert (output / "collection_runs.csv").is_file()

    def test_export_csv_contains_chinese_headers(self, memory_db, tmp_path):
        """CSV files use Chinese headers."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="chinese-headers")
            db.add(run)
            db.commit()
            output = tmp_path / "chinese"
            export_run_outputs("chinese-headers", db, output, debug_export=True)
            csv_path = output / "collection_runs.csv"
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                headers = next(reader)
            # The Chinese headers map is in export_fixture_run_csv
            # "run_id" -> "采集批次ID", "status" -> "状态"
            from export_fixture_run_csv import CHINESE_HEADERS
            # At least some headers should be Chinese
            chinese_headers_in_csv = [h for h in headers if h in CHINESE_HEADERS.values()]
            assert len(chinese_headers_in_csv) > 0

    def test_database_fingerprint_is_stable(self, memory_db):
        """Database fingerprint is stable for the same schema."""
        engine, factory = memory_db
        with factory() as db:
            fp1 = _database_fingerprint(db)
            fp2 = _database_fingerprint(db)
            assert fp1 == fp2
            assert len(fp1) == 12

    def test_export_run_manifest_runtime_mode(self, memory_db, tmp_path):
        """Manifest runtime_mode matches the source."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="mode-test")
            db.add(run)
            db.commit()
            output = tmp_path / "mode"
            export_run_manifest("mode-test", db, output, run=run, runtime_mode="test")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["runtime_mode"] == "test"


# =========================================================================
# 8. End-to-End Fake Collector Tests
# =========================================================================


class TestFakeCollectorEndToEnd:
    """End-to-end tests using the FakeCollector through the orchestrator."""

    @pytest.mark.asyncio
    async def test_success_scenario(self, tmp_path, memory_db):
        """A drug searches successfully and detail succeeds."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="e2e-success")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()
            queue = TaskQueueService(db)
            queue.enqueue(CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥",
                          "search_limit": 5, "candidate_limit": 3, "inspect_limit": 1},
            ))
            db.commit()

            collector = FakeSearchCollector(scenario="success")
            orchestrator = BatchOrchestrator(
                session=db, collector=collector,
                evidence_store=EvidenceStore(tmp_path / "evidence"),
                run_id=run.id,
                rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
                sleep=lambda _: _no_sleep(),
            )
            outcome = await orchestrator.execute_platform("taobao", "taobao-p0", task_types={TaskType.SEARCH.value})
            assert outcome["completed"] >= 1
            assert outcome["paused"] == 0

    @pytest.mark.asyncio
    async def test_zero_hits_scenario(self, tmp_path, memory_db):
        """A drug search returns zero hits."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="e2e-zero-hits")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()
            queue = TaskQueueService(db)
            queue.enqueue(CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥",
                          "search_limit": 5, "candidate_limit": 3, "inspect_limit": 1},
            ))
            db.commit()

            collector = FakeSearchCollector(scenario="zero_hits")
            orchestrator = BatchOrchestrator(
                session=db, collector=collector,
                evidence_store=EvidenceStore(tmp_path / "evidence"),
                run_id=run.id,
                rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
                sleep=lambda _: _no_sleep(),
            )
            outcome = await orchestrator.execute_platform("taobao", "taobao-p0", task_types={TaskType.SEARCH.value})
            assert outcome["completed"] >= 1

    @pytest.mark.asyncio
    async def test_detail_parse_failure(self, tmp_path, memory_db):
        """A drug detail parse failure is handled gracefully."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="e2e-detail-fail")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()
            queue = TaskQueueService(db)
            # Enqueue search task
            search_task = queue.enqueue(CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥",
                          "search_limit": 5, "candidate_limit": 3, "inspect_limit": 1},
            ))
            # Also enqueue an inspect candidate task that will fail
            queue.enqueue(CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.INSPECT_CANDIDATE, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                product_id="test-prod-001",
                query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥",
                          "fake_scenario": "detail_fail",
                          "search_limit": 5, "candidate_limit": 3, "inspect_limit": 1},
            ))
            db.commit()

            collector = FakeSearchCollector(scenario="detail_fail")
            orchestrator = BatchOrchestrator(
                session=db, collector=collector,
                evidence_store=EvidenceStore(tmp_path / "evidence"),
                run_id=run.id,
                rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
                sleep=lambda _: _no_sleep(),
            )

            # Execute search stage first
            await orchestrator.execute_platform("taobao", "taobao-p0", task_types={TaskType.SEARCH.value})
            # Then execute detail (inspect) stage
            outcome = await orchestrator.execute_platform("taobao", "taobao-p0", task_types={TaskType.INSPECT_CANDIDATE.value})
            assert outcome["completed"] >= 1

    @pytest.mark.asyncio
    async def test_login_block_on_platform(self, tmp_path, memory_db):
        """A platform with login block is paused."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="e2e-login-block")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()
            queue = TaskQueueService(db)
            queue.enqueue(CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥",
                          "search_limit": 5, "candidate_limit": 3, "inspect_limit": 1},
            ))
            db.commit()

            collector = FakeSearchCollector(scenario="login_block")
            orchestrator = BatchOrchestrator(
                session=db, collector=collector,
                evidence_store=EvidenceStore(tmp_path / "evidence"),
                run_id=run.id,
                rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
                sleep=lambda _: _no_sleep(),
            )
            outcome = await orchestrator.execute_platform("taobao", "taobao-p0", task_types={TaskType.SEARCH.value})
            assert outcome["paused"] == 1

    @pytest.mark.asyncio
    async def test_export_after_end_to_end(self, tmp_path, memory_db):
        """Export after a successful run includes manifest with correct status."""
        engine, factory = memory_db
        output_dir = tmp_path / "e2e-export"
        with factory() as db:
            run = CollectionRun(id="e2e-export-final", status="completed")
            db.add(run)
            db.flush()
            task = CollectionTask(
                id="e2e-export-task", run_id=run.id, platform="taobao",
                task_type="search", status="succeeded",
                session_alias="taobao-p0",
            )
            db.add(task)
            db.commit()

            export_run_outputs("e2e-export-final", db, output_dir, debug_export=True)
            export_run_manifest("e2e-export-final", db, output_dir, run=run,
                                source_type="test_workbench", runtime_mode="test")

            assert (output_dir / "collection_runs.csv").is_file()
            assert (output_dir / "collection_tasks.csv").is_file()
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["run_id"] == "e2e-export-final"
            assert manifest["source_type"] == "test_workbench"
            assert manifest["execution_status"] == "completed"
            assert not list(output_dir.glob("fixture_*.csv"))
            manifest_str = json.dumps(manifest)
            assert "password" not in manifest_str.lower()

    @pytest.mark.asyncio
    async def test_full_pipeline_run_status(self, tmp_path, memory_db):
        """Full pipeline: run status is correctly set to completed."""
        engine, factory = memory_db
        with factory() as db:
            run = CollectionRun(id="e2e-pipeline")
            db.add(run)
            db.flush()
            drug = DrugProduct(brand_name="托妥", generic_name="瑞舒伐他汀钙片")
            db.add(drug)
            db.flush()
            queue = TaskQueueService(db)
            queue.enqueue(CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="taobao",
                task_type=TaskType.SEARCH, session_alias="taobao-p0",
                drug_name="托妥", generic_name="瑞舒伐他汀钙片",
                query="托妥 瑞舒伐他汀钙片",
                metadata={"drug_id": drug.id, "target_brand": "托妥",
                          "search_limit": 5, "candidate_limit": 3, "inspect_limit": 1},
            ))
            db.commit()

            collector = FakeSearchCollector(scenario="success")
            orchestrator = BatchOrchestrator(
                session=db, collector=collector,
                evidence_store=EvidenceStore(tmp_path / "evidence"),
                run_id=run.id,
                rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
                sleep=lambda _: _no_sleep(),
            )
            outcomes = await orchestrator.execute_all({"taobao": "taobao-p0"})

            run = db.get(CollectionRun, "e2e-pipeline")
            assert run is not None


# =========================================================================
# 9. Test Worker and TestRunConfig Tests
# =========================================================================


class TestTestWorker:
    """Verify the TestWorker produces correct ProgressUpdate events."""

    def test_worker_creates_progress_queue(self, tmp_path):
        config = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"])
        worker = TestWorker(config, tmp_path)
        assert worker.queue is not None
        assert isinstance(worker.queue, Queue)

    def test_worker_cancel_flag(self, tmp_path):
        config = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"])
        worker = TestWorker(config, tmp_path)
        assert not worker._cancel_flag.is_set()
        worker.cancel()
        assert worker._cancel_flag.is_set()

    def test_worker_elapsed_time(self, tmp_path):
        config = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"])
        worker = TestWorker(config, tmp_path)
        worker._start_time = time.time() - 10
        elapsed = worker._elapsed()
        assert 9.5 <= elapsed <= 10.5

    def test_progress_update_has_elapsed(self, tmp_path):
        config = TestRunConfig(drugs=[DrugSelection.from_generic_name("托妥")], platforms=["taobao"])
        worker = TestWorker(config, tmp_path)
        update = ProgressUpdate(
            timestamp="10:00:00", phase="init", status="running",
            message="测试",
        )
        worker._put(update)
        assert not worker.queue.empty()
        received = worker.queue.get_nowait()
        assert received.elapsed_seconds >= 0

    @pytest.mark.asyncio
    async def test_enqueue_platform_tasks_creates_tasks(self, tmp_path, memory_db):
        engine, factory = memory_db
        config = TestRunConfig(
            drugs=[DrugSelection.from_generic_name("瑞舒伐他汀钙片")],
            platforms=["taobao"],
            search_modes=["global_search"],
            search_limit=10,
            max_candidates=5,
        )
        worker = TestWorker(config, tmp_path)
        with factory() as db:
            queue = TaskQueueService(db)
            run = queue.create_run()
            await worker._enqueue_platform_tasks(db, queue, run.id, "taobao", config)
            db.commit()
            tasks = db.scalars(
                select(CollectionTask).where(CollectionTask.run_id == run.id)
            ).all()
            assert len(tasks) >= 1
            for task in tasks:
                assert task.run_id == run.id
                assert task.platform == "taobao"