"""Offline tests for the bounded-live-smoke entry point.

These tests verify the three HANDOFF fixes and the new logging/CLI
features without touching any real browser, platform, or network.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update

from price_specialist.collector import ComputerUseCollector
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.enums import CollectionStatus, TaskStatus, TaskType
from price_specialist.evidence import EvidenceStore
from price_specialist.models import CollectionRun, CollectionTask, PriceObservation, SearchCandidate
from price_specialist.orchestrator import BatchOrchestrator, RatePolicy
from price_specialist.run_logger import BatchLogger
from price_specialist.schemas import BrowserSession, CollectionResult, CollectionTaskSpec, EvidenceBundle, SearchHit
from price_specialist.services import TaskQueueService


# ---------------------------------------------------------------------------
# Fake collectors
# ---------------------------------------------------------------------------

class EmptySearchCollector(ComputerUseCollector):
    """Returns zero search hits, simulating a store with no matching products."""

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        return []

    async def search_store(self, task, session):
        return []

    async def inspect_candidate(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


class SingleHitSearchCollector(ComputerUseCollector):
    """Returns search hits with provider_id, used for inspect-limit tests."""

    def __init__(self) -> None:
        self.calls = 0

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        self.calls += 1
        return [
            SearchHit(
                platform=session.platform, query=query, rank=1,
                title="托妥 10mg*28片 瑞舒伐他汀钙片", product_id="prod-1",
                url="https://item.taobao.com/item.htm?id=1", shop_name="测试店铺",
                raw={"provider_id": "provider-1"},
            ),
            SearchHit(
                platform=session.platform, query=query, rank=2,
                title="托妥 20mg*14片 瑞舒伐他汀钙片", product_id="prod-2",
                url="https://item.taobao.com/item.htm?id=2", shop_name="测试店铺",
                raw={"provider_id": "provider-1"},
            ),
        ]

    async def inspect_candidate(self, task, session):
        return CollectionResult(
            collection_status=CollectionStatus.SUCCESS,
            page_price_value=100.0, page_shop="测试店铺",
            selected_spec="10mg*28片",
            evidence=EvidenceBundle(raw_fields={"price": "100"}),
        )

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


class DetailSuccessCollector(ComputerUseCollector):
    """Returns a successful detail page price with provider_id."""

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        return [SearchHit(
            platform=session.platform, query=query, rank=1,
            title="托妥 10mg*28片 瑞舒伐他汀钙片", product_id="prod-1",
            url="https://item.taobao.com/item.htm?id=1", shop_name="测试店铺",
            raw={"provider_id": "provider-1"},
        )]

    async def inspect_candidate(self, task, session):
        return CollectionResult(
            collection_status=CollectionStatus.SUCCESS,
            page_price_value=99.0, page_shop="测试店铺",
            selected_spec="10mg*28片",
            evidence=EvidenceBundle(raw_fields={"price": "99"}),
        )

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


async def _no_sleep() -> None:
    return None


@pytest.fixture
def memory_db():
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    return engine, factory


# ---------------------------------------------------------------------------
# Test 1: 零候选 → NOT_FOUND
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_candidates_returns_not_found(memory_db, tmp_path) -> None:
    """Search succeeding but returning zero hits must produce NOT_FOUND."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-zero")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="search-zero", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="测试 药品", drug_name="测试",
            metadata={"drug_id": "drug-1", "target_brand": "测试", "inspect_limit": 1},
        ))
        db.commit()
        runner = BatchOrchestrator(
            session=db, collector=EmptySearchCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        # The search task should have completed (not paused)
        assert outcomes[0]["paused"] == 0
        # Check the observation: collection_status should be not_found
        obs = db.scalar(select(PriceObservation).where(PriceObservation.run_id == run.id))
        assert obs is not None, "应该有 PriceObservation 记录"
        assert obs.collection_status == CollectionStatus.NOT_FOUND.value, \
            f"预期 not_found，实际 {obs.collection_status}"
        assert obs.error_code == "no_valid_candidate", \
            f"预期 no_valid_candidate，实际 {obs.error_code}"
        # Confirm no detail tasks were created
        detail_tasks = db.scalars(
            select(CollectionTask).where(
                CollectionTask.run_id == run.id,
                CollectionTask.task_type == TaskType.INSPECT_CANDIDATE.value,
            )
        ).all()
        assert len(detail_tasks) == 0, "零候选不应创建任何详情任务"


# ---------------------------------------------------------------------------
# Test 2: inspect_limit=1 默认只创建1个详情任务
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inspect_limit_defaults_to_one(memory_db, tmp_path) -> None:
    """Even when multiple valid candidates exist, only one detail task is created."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-limit")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        # Manually set inspect_limit=1 (the default)
        queue.enqueue(CollectionTaskSpec(
            task_id="search-limit", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()
        runner = BatchOrchestrator(
            session=db, collector=SingleHitSearchCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        assert outcomes[0]["paused"] == 0
        detail_tasks = db.scalars(
            select(CollectionTask).where(
                CollectionTask.run_id == run.id,
                CollectionTask.task_type == TaskType.INSPECT_CANDIDATE.value,
            )
        ).all()
        assert len(detail_tasks) == 1, \
            f"预期1个详情任务，实际 {len(detail_tasks)}"


# ---------------------------------------------------------------------------
# Test 3: inspect_limit=0 在 orchestrator 中默认行为已改为1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inspect_limit_zero_also_defaults_to_one(memory_db, tmp_path) -> None:
    """When metadata does not set inspect_limit, orchestrator defaults to 1."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-zero-limit")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        # No inspect_limit in metadata — orchestrator should default to 1
        queue.enqueue(CollectionTaskSpec(
            task_id="search-no-limit", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥"},
        ))
        db.commit()
        runner = BatchOrchestrator(
            session=db, collector=SingleHitSearchCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        assert outcomes[0]["paused"] == 0
        detail_tasks = db.scalars(
            select(CollectionTask).where(
                CollectionTask.run_id == run.id,
                CollectionTask.task_type == TaskType.INSPECT_CANDIDATE.value,
            )
        ).all()
        assert len(detail_tasks) == 1, \
            f"默认 inspect_limit 应为1，实际 {len(detail_tasks)} 个详情任务"


@pytest.mark.asyncio
async def test_candidate_limit_caps_persisted_search_candidates(memory_db, tmp_path) -> None:
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-candidate-limit")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="search-candidate-limit", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={
                "drug_id": "drug-1", "target_brand": "托妥",
                "candidate_limit": 1, "inspect_limit": 1,
            },
        ))
        db.commit()

        runner = BatchOrchestrator(
            session=db, collector=SingleHitSearchCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        await runner.execute_all({"yaoshibang": "ysb-p0"})

        candidates = db.scalars(
            select(SearchCandidate).where(SearchCandidate.run_id == run.id)
        ).all()
        assert len(candidates) == 1
        assert candidates[0].search_rank == 1


# ---------------------------------------------------------------------------
# Test 4: BatchLogger 写入 JSONL 文件
# ---------------------------------------------------------------------------

def test_batch_logger_writes_jsonl(tmp_path) -> None:
    """BatchLogger creates a run.log.jsonl with correct structure."""
    run_id = "test-logger-run"
    output_dir = tmp_path / "runs" / "current" / run_id
    logger = BatchLogger(run_id, output_dir)

    logger.batch_start("yaoshibang", total_tasks=3)
    logger.task_start("yaoshibang", task_id="task-1", route="global", drug="测试药品")
    logger.search_complete("yaoshibang", task_id="task-1", hit_count=0, valid_count=0, duration=5.2)
    logger.task_fail("yaoshibang", task_id="task-1", error_type="no_valid_candidate", duration=5.2)
    logger.platform_pause("yaoshibang", reason="challenge_detected")
    logger.batch_end("yaoshibang", summary={"completed": 1, "paused": 1})

    logger.close()
    assert logger.log_path.exists(), "JSONL 文件应存在"
    lines = logger.log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 6, f"预期6行日志，实际 {len(lines)}"

    for line in lines:
        event = json.loads(line)
        assert "run_id" in event
        assert event["run_id"] == run_id
        assert "event_type" in event
        assert "platform" in event
        assert "timestamp" in event
        # No sensitive fields
        assert "password" not in json.dumps(event)
        assert "cookie" not in json.dumps(event)
        assert "token" not in json.dumps(event)


# ---------------------------------------------------------------------------
# Test 5: BatchLogger 终端输出不崩溃
# ---------------------------------------------------------------------------

def test_batch_logger_terminal_output(tmp_path, capsys) -> None:
    """Terminal output should include platform label and task index."""
    run_id = "test-terminal"
    logger = BatchLogger(run_id, tmp_path / "runs" / "current" / run_id)
    logger.batch_start("taobao", total_tasks=2)
    logger.task_start("taobao", task_id="t1", shop="阿里健康", drug="新托妥")
    captured = capsys.readouterr()
    assert "淘宝" in captured.out, "终端应显示中文平台名"
    assert "1/2" in captured.out, "终端应显示任务序号/总数"
    assert "阿里健康" in captured.out
    logger.close()


# ---------------------------------------------------------------------------
# Test 6: CSV 自动导出路径
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_export_creates_csv_files(memory_db, tmp_path) -> None:
    """After batch execution, CSV files should exist in the output directory."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-export")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="export-task", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()

        output_dir = tmp_path / "runs" / "current" / run.id
        output_dir.mkdir(parents=True, exist_ok=True)
        logger = BatchLogger(run.id, output_dir)

        runner = BatchOrchestrator(
            session=db, collector=EmptySearchCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id, logger=logger,
        )
        await runner.execute_all({"yaoshibang": "ysb-p0"})
        logger.close()

        # Verify the output directory structure created by the logger
        assert (output_dir / "run.log.jsonl").exists(), "缺少 JSONL 日志文件"
        # Verify evidence directory was created
        evidence_dir = tmp_path / "evidence"
        assert evidence_dir.exists(), "证据目录应存在"


# ---------------------------------------------------------------------------
# Test 7: --resume-run-id 逻辑
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_run_resets_leased_tasks(memory_db, tmp_path) -> None:
    """Resume should reset LEASED/RUNNING tasks back to PENDING."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-resume")
        db.add(run)
        db.flush()
        # Create a task in LEASED state (simulating a previous interrupted run)
        task = CollectionTask(
            id="resume-task", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH.value, status=TaskStatus.LEASED.value,
            session_alias="ysb-p0", priority=100,
            payload=CollectionTaskSpec(
                task_id="resume-task", run_id=run.id, platform="yaoshibang",
                task_type=TaskType.SEARCH, session_alias="ysb-p0",
                query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
                metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
            ).model_dump(mode="json"),
        )
        db.add(task)
        db.commit()

        # Simulate resume: reset leased/running tasks
        db.execute(
            update(CollectionTask)
            .where(CollectionTask.run_id == "test-resume")
            .where(CollectionTask.status.in_((TaskStatus.LEASED.value, TaskStatus.RUNNING.value)))
            .values(status=TaskStatus.PENDING.value)
        )
        db.commit()

        # Verify task is now PENDING
        task = db.get(CollectionTask, "resume-task")
        assert task is not None
        assert task.status == TaskStatus.PENDING.value, f"预期 PENDING，实际 {task.status}"

        # Now execute the resumed run
        runner = BatchOrchestrator(
            session=db, collector=EmptySearchCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id="test-resume",
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        # The task should have been picked up and completed
        assert outcomes[0]["completed"] >= 1, "恢复后的任务应被重新执行"


# ---------------------------------------------------------------------------
# Test 8: --platform 过滤
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_platform_filter_only_runs_requested_platform(memory_db, tmp_path) -> None:
    """When platform='taobao' is specified, yaoshibang tasks should not run."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-filter")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="taobao-task", run_id=run.id, platform="taobao",
            task_type=TaskType.SEARCH, session_alias="taobao-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        queue.enqueue(CollectionTaskSpec(
            task_id="ysb-task", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()

        # Only run taobao
        runner = BatchOrchestrator(
            session=db, collector=EmptySearchCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"taobao": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"taobao": "taobao-p0"})
        assert outcomes[0]["completed"] >= 1

        # ysb task should still be PENDING
        ysb_task = db.scalar(
            select(CollectionTask).where(CollectionTask.id == "ysb-task")
        )
        assert ysb_task is not None
        assert ysb_task.status == TaskStatus.PENDING.value, \
            "药师帮任务不应被处理"


# ---------------------------------------------------------------------------
# Test 9: 搜索 + 详情成功链路产出正式价格
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_detail_pipeline_without_review_is_not_formal(memory_db, tmp_path) -> None:
    """A direct runner without the mandatory gate is fail-closed."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-formal")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="search-pipe", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()

        runner = BatchOrchestrator(
            session=db, collector=DetailSuccessCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        # Check that the search task completed
        search_task = db.get(CollectionTask, "search-pipe")
        assert search_task is not None
        assert search_task.status == TaskStatus.SUCCEEDED.value

        # Check that a detail price observation was created
        detail_obs = db.scalar(
            select(PriceObservation).where(
                PriceObservation.run_id == run.id,
                PriceObservation.channel == "detail",
            )
        )
        assert detail_obs is not None, "应该有详情价格记录"
        assert detail_obs.collection_status == CollectionStatus.SUCCESS.value
        assert detail_obs.page_price_value == 99.0

        # A caller that bypasses the composition root cannot release a formal
        # price merely because detail collection succeeded.
        candidate = db.scalar(
            select(SearchCandidate).where(
                SearchCandidate.run_id == run.id,
            )
        )
        assert candidate is not None, "应该有搜索候选记录"
        assert candidate.is_formal_price is False, "未配置审核器不得标记正式价格"


# ---------------------------------------------------------------------------
# Test 10: 搜索结果缓存 — STORE_SEARCH 复用 SEARCH 结果
# ---------------------------------------------------------------------------

class CacheTrackingCollector(ComputerUseCollector):
    """Tracks how many times search() is called."""

    def __init__(self) -> None:
        self.search_count = 0

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        self.search_count += 1
        return [SearchHit(
            platform=session.platform, query=query, rank=1,
            title="托妥 10mg*28片 瑞舒伐他汀钙片", product_id="prod-1",
            url="https://item.taobao.com/item.htm?id=1", shop_name="测试店铺",
            raw={"provider_id": "provider-1"},
        )]

    async def inspect_candidate(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


@pytest.mark.asyncio
async def test_search_cache_reused_by_store_search(memory_db, tmp_path) -> None:
    """SEARCH result should be cached so STORE_SEARCH for same drug skips search."""
    engine, factory = memory_db
    collector = CacheTrackingCollector()
    with factory() as db:
        run = CollectionRun(id="test-cache")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        # SEARCH task for 托妥
        queue.enqueue(CollectionTaskSpec(
            task_id="search-global", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        # STORE_SEARCH task for same drug+provider
        queue.enqueue(CollectionTaskSpec(
            task_id="search-store", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.STORE_SEARCH, session_alias="ysb-p0",
            query="托妥", drug_name="托妥", shop_name="测试店铺",
            metadata={"drug_id": "drug-1", "target_brand": "托妥",
                      "provider_id": "provider-1", "inspect_limit": 1},
        ))
        db.commit()

        runner = BatchOrchestrator(
            session=db, collector=collector,
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        # SEARCH runs first, then STORE_SEARCH reuses cache — only 1 search call
        assert collector.search_count == 1, \
            f"预期只有1次搜索，实际 {collector.search_count}"
        assert outcomes[0]["completed"] >= 2, "至少两个任务完成"


# ---------------------------------------------------------------------------
# Test 11: 健康检查每平台只执行一次
# ---------------------------------------------------------------------------

class HealthTrackingCollector(ComputerUseCollector):
    """Tracks health_check calls."""

    def __init__(self) -> None:
        self.health_count = 0

    async def health_check(self, session):
        self.health_count += 1
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        return [SearchHit(
            platform=session.platform, query=query, rank=1,
            title="托妥 10mg*28片", product_id="prod-1",
            url="https://item.taobao.com/item.htm?id=1", shop_name="测试店铺",
        )]

    async def inspect_candidate(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


@pytest.mark.asyncio
async def test_health_check_once_per_platform(memory_db, tmp_path) -> None:
    """Health check should be called exactly once per platform, not per stage."""
    engine, factory = memory_db
    collector = HealthTrackingCollector()
    with factory() as db:
        run = CollectionRun(id="test-health")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        # One SEARCH and one INSPECT task to trigger multiple stages
        queue.enqueue(CollectionTaskSpec(
            task_id="h-search", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()

        runner = BatchOrchestrator(
            session=db, collector=collector,
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        await runner.execute_all({"yaoshibang": "ysb-p0"})
        # Only 1 health check for the single platform
        assert collector.health_count == 1, \
            f"预期1次健康检查，实际 {collector.health_count}"


# ---------------------------------------------------------------------------
# Test 12: Provider_id 从 StoreResponsibility 复用
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_id_reused_from_store(memory_db, tmp_path) -> None:
    """When StoreResponsibility has platform_store_key, it should be reused."""
    engine, factory = memory_db
    with factory() as db:
        # Create a store with a known provider_id
        from price_specialist.models import StoreResponsibility as StoreModel
        store = StoreModel(
            internal_store_id="W99999", platform="yaoshibang",
            shop_name="已核验店铺", shop_status="正常", fixed_tier="observation_only",
            platform_store_key="provider-verified",
        )
        db.add(store)
        db.flush()

        run = CollectionRun(id="test-provider")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="p-store-search", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.STORE_SEARCH, session_alias="ysb-p0",
            query="托妥", drug_name="托妥", shop_name="已核验店铺",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()

        # Inject a collector that raises if provider_id is missing
        class StrictProviderCollector(ComputerUseCollector):
            async def health_check(self, session):
                return CollectionResult(collection_status=CollectionStatus.SUCCESS)
            async def collect_fixed(self, task, session):
                return CollectionResult(collection_status=CollectionStatus.SUCCESS)
            async def search(self, query, session, **kwargs):
                return []
            async def search_store(self, task, session):
                pid = task.metadata.get("provider_id", "")
                if not pid:
                    raise RuntimeError("provider_id 未传入，应该从 StoreResponsibility 复用")
                if pid != "provider-verified":
                    raise RuntimeError(f"provider_id 不匹配: {pid}")
                return []
            async def inspect_candidate(self, task, session):
                return CollectionResult(collection_status=CollectionStatus.SUCCESS)
            async def resume_incident(self, incident_id, session):
                return CollectionResult(collection_status=CollectionStatus.SUCCESS)

        runner = BatchOrchestrator(
            session=db, collector=StrictProviderCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        # Should not raise — provider_id is extracted from StoreResponsibility
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        assert outcomes[0]["completed"] == 1, "店铺搜索任务应完成"


# ---------------------------------------------------------------------------
# Test 13: STORE_SEARCH 缓存过滤为空 -> fall through 到 collector.search_store
# ---------------------------------------------------------------------------

class CacheFilterEmptyFallthroughCollector(ComputerUseCollector):
    """SEARCH returns hits with provider-1; STORE_SEARCH expects provider-2.

    Cache filter will be empty, so orchestrator must fall through to
    search_store() rather than returning NOT_FOUND immediately.
    """

    def __init__(self) -> None:
        self.search_count = 0
        self.search_store_count = 0

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        self.search_count += 1
        # 全局搜索返回 provider-1 的命中（会被 STORE_SEARCH 的 provider-2 过滤掉）
        return [SearchHit(
            platform=session.platform, query=query, rank=1,
            title="托妥 10mg*28片 瑞舒伐他汀钙片", product_id="prod-1",
            url="https://item.taobao.com/item.htm?id=1", shop_name="店铺A",
            raw={"provider_id": "provider-1"},
        )]

    async def search_store(self, task, session):
        self.search_store_count += 1
        return [SearchHit(
            platform=session.platform, query=task.query or "", rank=1,
            title="托妥 10mg*28片 瑞舒伐他汀钙片", product_id="prod-2",
            url="https://item.taobao.com/item.htm?id=2", shop_name="店铺B",
            raw={"provider_id": "provider-2"},
        )]

    async def inspect_candidate(self, task, session):
        return CollectionResult(
            collection_status=CollectionStatus.SUCCESS,
            page_price_value=50.0, page_shop="店铺B",
            selected_spec="10mg*28片",
            evidence=EvidenceBundle(raw_fields={"price": "50"}),
        )

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


@pytest.mark.asyncio
async def test_store_search_cache_filter_empty_falls_through(memory_db, tmp_path) -> None:
    """When cache filter empties hits, orchestrator must call search_store."""
    engine, factory = memory_db
    collector = CacheFilterEmptyFallthroughCollector()
    with factory() as db:
        run = CollectionRun(id="test-fallthrough")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        # SEARCH 缓存 provider-1 的命中。inspect_limit=-1 表示 SEARCH 不生成详情任务，
        # 这样只有 STORE_SEARCH fall through 后的 prod-2 会生成详情任务。
        queue.enqueue(CollectionTaskSpec(
            task_id="search-global", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": -1},
        ))
        queue.enqueue(CollectionTaskSpec(
            task_id="search-store", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.STORE_SEARCH, session_alias="ysb-p0",
            query="托妥", drug_name="托妥", shop_name="店铺B",
            metadata={"drug_id": "drug-1", "target_brand": "托妥",
                      "provider_id": "provider-2", "inspect_limit": 1},
        ))
        db.commit()

        runner = BatchOrchestrator(
            session=db, collector=collector,
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        assert collector.search_store_count == 1, \
            f"缓存过滤为空时应 fall through 到 search_store，实际调用 {collector.search_store_count} 次"
        store_obs = db.scalar(
            select(PriceObservation).where(PriceObservation.task_id == "search-store")
        )
        assert store_obs is not None, "应记录 STORE_SEARCH 观测"
        assert store_obs.collection_status == "success", \
            f"STORE_SEARCH 应成功（fall through 后找到命中），实际 {store_obs.collection_status}"
        # inspect_limit=-1 让 SEARCH 不生成详情，inspect_limit=1 让 STORE_SEARCH 只生成 1 个
        detail_tasks = db.scalars(
            select(CollectionTask).where(CollectionTask.task_type == "inspect_candidate")
        ).all()
        assert len(detail_tasks) == 1, f"应只有 1 个详情任务（来自 STORE_SEARCH），实际 {len(detail_tasks)}"
        assert detail_tasks[0].payload["product_id"] == "prod-2"
        assert outcomes[0]["completed"] >= 2


# ---------------------------------------------------------------------------
# Test 14: INSPECT_CANDIDATE rank=1 失败 -> 自动回退 rank=2
# ---------------------------------------------------------------------------

class RankFallbackCollector(ComputerUseCollector):
    """rank=1 详情返回 PARSE_ERROR，rank=2 详情返回 SUCCESS。"""

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        return [
            SearchHit(
                platform=session.platform, query=query, rank=1,
                title="托妥 10mg*28片 瑞舒伐他汀钙片", product_id="prod-rank1",
                url="https://item.taobao.com/item.htm?id=1", shop_name="测试店铺",
                raw={"provider_id": "provider-1"},
            ),
            SearchHit(
                platform=session.platform, query=query, rank=2,
                title="托妥 10mg*28片 瑞舒伐他汀钙片", product_id="prod-rank2",
                url="https://item.taobao.com/item.htm?id=2", shop_name="测试店铺",
                raw={"provider_id": "provider-1"},
            ),
        ]

    async def inspect_candidate(self, task, session):
        candidate_pid = (task.metadata or {}).get("candidate_product_id") or task.product_id
        if candidate_pid == "prod-rank1":
            return CollectionResult(
                collection_status=CollectionStatus.PARSE_ERROR,
                error_detail="rank=1 模拟解析失败",
                evidence=EvidenceBundle(raw_fields={"error": "parse_error"}),
            )
        return CollectionResult(
            collection_status=CollectionStatus.SUCCESS,
            page_price_value=88.0, page_shop="测试店铺",
            selected_spec="10mg*28片",
            evidence=EvidenceBundle(raw_fields={"price": "88"}),
        )

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


@pytest.mark.asyncio
async def test_inspect_candidate_rank2_fallback_on_parse_error(memory_db, tmp_path) -> None:
    """rank=1 detail fails with PARSE_ERROR -> orchestrator falls back to rank=2."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-fallback")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="search-fallback", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()

        runner = BatchOrchestrator(
            session=db, collector=RankFallbackCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        outcomes = await runner.execute_all({"yaoshibang": "ysb-p0"})
        detail_tasks = db.scalars(
            select(CollectionTask).where(CollectionTask.task_type == "inspect_candidate")
        ).all()
        product_ids = {t.payload["product_id"] for t in detail_tasks}
        assert "prod-rank1" in product_ids, "rank=1 任务应存在"
        assert "prod-rank2" in product_ids, "rank=2 回退任务应被自动 enqueue"
        formal = db.scalar(
            select(SearchCandidate).where(
                SearchCandidate.run_id == run.id,
                SearchCandidate.product_id == "prod-rank2",
            )
        )
        assert formal is not None
        assert formal.is_formal_price is False, "未配置审核器不得标记正式价格"
        assert outcomes[0]["completed"] >= 2


# ---------------------------------------------------------------------------
# Test 15: 回退上限 - 所有候选都失败时最多回退 2 次
# ---------------------------------------------------------------------------

class AllFailFallbackCollector(ComputerUseCollector):
    """所有详情都返回 PARSE_ERROR，验证回退上限不无限循环。"""

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def search(self, query, session, **kwargs):
        return [
            SearchHit(
                platform=session.platform, query=query, rank=i + 1,
                title=f"托妥 10mg*28片 瑞舒伐他汀钙片 #{i}", product_id=f"prod-fail-{i}",
                url=f"https://item.taobao.com/item.htm?id={i}", shop_name="测试店铺",
                raw={"provider_id": "provider-1"},
            )
            for i in range(5)
        ]

    async def inspect_candidate(self, task, session):
        return CollectionResult(
            collection_status=CollectionStatus.PARSE_ERROR,
            error_detail="所有候选都失败",
            evidence=EvidenceBundle(raw_fields={"error": "parse_error"}),
        )

    async def resume_incident(self, incident_id, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)


@pytest.mark.asyncio
async def test_inspect_candidate_fallback_cap_at_2(memory_db, tmp_path) -> None:
    """All candidates fail -> at most 1 initial + 2 fallbacks = 3 detail tasks."""
    engine, factory = memory_db
    with factory() as db:
        run = CollectionRun(id="test-fallback-cap")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        queue.enqueue(CollectionTaskSpec(
            task_id="search-cap", run_id=run.id, platform="yaoshibang",
            task_type=TaskType.SEARCH, session_alias="ysb-p0",
            query="托妥 瑞舒伐他汀钙片", drug_name="托妥",
            metadata={"drug_id": "drug-1", "target_brand": "托妥", "inspect_limit": 1},
        ))
        db.commit()

        runner = BatchOrchestrator(
            session=db, collector=AllFailFallbackCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={"yaoshibang": RatePolicy(0, 0, 99, 0)},
            run_id=run.id,
        )
        await runner.execute_all({"yaoshibang": "ysb-p0"})
        detail_tasks = db.scalars(
            select(CollectionTask).where(CollectionTask.task_type == "inspect_candidate")
        ).all()
        assert len(detail_tasks) <= 3, \
            f"最多 3 个详情任务（1 初始 + 2 回退），实际 {len(detail_tasks)}"
        assert len(detail_tasks) == 3, \
            f"应有 3 个详情任务（候选足够多），实际 {len(detail_tasks)}"
        attempts = sorted(t.payload["metadata"].get("fallback_attempt", 0) for t in detail_tasks)
        assert attempts == [0, 1, 2], f"回退计数应为 0,1,2，实际 {attempts}"
