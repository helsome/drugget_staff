from decimal import Decimal

import pytest

from price_specialist.collector import ComputerUseCollector
from price_specialist.enums import CollectionStatus, TaskType
from price_specialist.evidence import EvidenceStore
from price_specialist.orchestrator import DualRouteRunner, FixedWork, SearchWork
from price_specialist.orchestrator import BatchOrchestrator, RatePolicy
from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.models import CollectionRun, Incident, PriceObservation
from price_specialist.services import TaskQueueService
from price_specialist.schemas import BrowserSession, CollectionResult, CollectionTaskSpec, EvidenceBundle, SearchHit
from price_specialist.search import SearchClassifier


class FakeCollector(ComputerUseCollector):
    def __init__(self) -> None:
        self.fixed_calls = 0

    async def health_check(self, session):
        return CollectionResult(collection_status=CollectionStatus.SUCCESS)

    async def collect_fixed(self, task, session):
        self.fixed_calls += 1
        if self.fixed_calls == 1:
            return CollectionResult(collection_status=CollectionStatus.NETWORK_ERROR)
        return CollectionResult(
            collection_status=CollectionStatus.CHALLENGE_DETECTED,
            final_url=task.url,
            evidence=EvidenceBundle(raw_fields={"cookie": "secret"}),
        )

    async def search(self, query, session):
        return [
            SearchHit(
                platform=session.platform,
                query=query,
                rank=1,
                title="新托妥 10mg*28片/盒 瑞舒伐他汀钙片",
                product_id="1",
                url="https://item.taobao.com/item.htm?id=1",
                shop_name="新店",
            )
        ]

    async def inspect_candidate(self, task, session):
        raise NotImplementedError

    async def resume_incident(self, incident_id, session):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_routes_are_independent_and_network_retry_is_bounded(tmp_path) -> None:
    collector = FakeCollector()
    runner = DualRouteRunner(collector, EvidenceStore(tmp_path), network_retry_limit=2)
    session = BrowserSession(platform="taobao", alias="persistent")
    task = CollectionTaskSpec(
        task_id="task-1",
        run_id="run-1",
        platform="taobao",
        task_type=TaskType.FIXED_CORE,
        session_alias="persistent",
        product_id="1",
        url="https://item.taobao.com/item.htm?id=1",
    )
    classifier = SearchClassifier(fixed_product_ids=set(), fixed_urls=set(), fixed_stores={}, known_stores={})
    outcome = await runner.run(
        fixed=[FixedWork(task, session, Decimal("1"), Decimal("28"), "片", Decimal("0.8"))],
        search=[SearchWork("托妥 瑞舒伐他汀钙片", "托妥", "10mg*28片", session, classifier)],
    )
    assert collector.fixed_calls == 2
    assert outcome.fixed.status == "partial"
    assert outcome.fixed.incidents[0]["incident_type"] == "challenge_detected"
    assert outcome.search.status == "completed"
    assert len(outcome.search.results) == 1


class IsolatedCollector(FakeCollector):
    async def collect_fixed(self, task, session):
        if session.platform == "jd":
            return CollectionResult(
                collection_status=CollectionStatus.CHALLENGE_DETECTED,
                final_url=task.url,
                page_title="安全验证",
            )
        return CollectionResult(
            collection_status=CollectionStatus.SUCCESS,
            final_url=task.url,
            page_title="正常商品",
            page_price_raw="10.00",
        )


@pytest.mark.asyncio
async def test_challenge_pauses_only_one_platform_session(tmp_path) -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        run = CollectionRun(id="run-batch")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        for platform in ("jd", "taobao"):
            queue.enqueue(CollectionTaskSpec(
                task_id=f"task-{platform}", run_id=run.id, platform=platform,
                task_type=TaskType.FIXED_CORE, session_alias=f"{platform}-p0",
                product_id="1", url=f"https://example.test/{platform}/1",
            ))
        db.commit()
        runner = BatchOrchestrator(
            session=db,
            collector=IsolatedCollector(),
            evidence_store=EvidenceStore(tmp_path / "evidence"),
            sleep=lambda _: _no_sleep(),
            rate_policies={
                "jd": RatePolicy(0, 0, 99, 0),
                "taobao": RatePolicy(0, 0, 99, 0),
            },
        )
        outcomes = await runner.execute_all({"jd": "jd-p0", "taobao": "taobao-p0"})
        assert outcomes[0]["reason"] == "challenge_detected"
        assert outcomes[1]["completed"] == 1
        assert db.query(Incident).count() == 1
        assert db.query(PriceObservation).filter_by(channel="fixed").count() == 1
        again = await runner.execute_platform("jd", "jd-p0")
        assert again["reason"] == "unresolved_incident"


async def _no_sleep() -> None:
    return None


def test_yaoshibang_rate_policy_is_bounded_and_more_conservative() -> None:
    policy = RatePolicy(32, 45, 4, 240, interval_jitter_seconds=8, cooldown_jitter_seconds=45)
    assert 24 <= policy.delay_for(TaskType.FIXED_CORE, batch_complete=False) <= 40
    assert 37 <= policy.delay_for(TaskType.SEARCH, batch_complete=False) <= 53
    assert 195 <= policy.delay_for(TaskType.SEARCH, batch_complete=True) <= 285
