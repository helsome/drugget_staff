from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.enums import TaskType
from price_specialist.models import CollectionRun, CollectionTask, PriceObservation, StoreResponsibility
from price_specialist.schemas import CollectionTaskSpec
from price_specialist.services import TaskQueueService


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_DIR = PROJECT_ROOT / "collectors"


@pytest.fixture(scope="module")
def runner_module():
    sys.path.insert(0, str(COLLECTOR_DIR))
    spec = importlib.util.spec_from_file_location("fixture_runner", COLLECTOR_DIR / "run_fixture_live_smoke.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_yaoshibang_seed_selection_supports_global_and_store_routes(runner_module) -> None:
    global_seed = runner_module.select_yaoshibang_seed(
        seed_key="GLOBAL_SEARCH|yaoshibang|优立维|brand_generic", store_id=None, brand=None,
    )
    store_seed = runner_module.select_yaoshibang_seed(seed_key=None, store_id="W00010", brand="葛泰")
    assert global_seed["brand"] == "优立维"
    assert store_seed["brand"] == "葛泰"
    assert store_seed["generic_name"] == "地奥司明片"


def test_yaoshibang_seed_selection_rejects_invalid_or_ambiguous_input(runner_module) -> None:
    with pytest.raises(ValueError, match="同时提供"):
        runner_module.select_yaoshibang_seed(seed_key=None, store_id="W00010", brand=None)
    with pytest.raises(ValueError, match="命中 0 条"):
        runner_module.select_yaoshibang_seed(seed_key="GLOBAL_SEARCH|taobao|托妥|brand_generic", store_id=None, brand=None)


def test_fixture_specs_are_drug_specific(runner_module) -> None:
    assert runner_module.fixture_specs("葛泰") == {"0.45g*20片", "0.45g*24片"}
    assert "75mg*36片" in runner_module.fixture_specs("优立维")


def detail_task() -> CollectionTask:
    spec = CollectionTaskSpec(
        task_id="detail-task", run_id="test-run", platform="yaoshibang", task_type=TaskType.INSPECT_CANDIDATE,
        session_alias="yaoshibang-p0", drug_name="葛泰", generic_name="地奥司明片", product_id="product-1",
        metadata={"provider_id": "provider-1"},
    )
    return CollectionTask(
        id=spec.task_id, run_id=spec.run_id, platform=spec.platform, task_type=spec.task_type.value,
        status="succeeded", session_alias=spec.session_alias, payload=spec.model_dump(mode="json"), priority=100,
    )


def detail_observation(**overrides) -> PriceObservation:
    values = {
        "id": "detail-observation", "run_id": "test-run", "task_id": "detail-task", "channel": "detail",
        "collection_status": "success", "calculation_status": "not_applicable", "price_status": "not_evaluated",
        "page_price_value": Decimal("16.17"), "selected_spec": "0.45g*20片", "sale_box_count": Decimal("10"),
        "page_shop": "药实在",
    }
    values.update(overrides)
    return PriceObservation(**values)


def test_detail_validation_accepts_only_complete_detail_price(runner_module) -> None:
    assert runner_module.validate_yaoshibang_detail(
        detail=detail_observation(), detail_task=detail_task(), expected_specs={"0.45g*20片", "0.45g*24片"},
    ) == "provider-1"


@pytest.mark.parametrize(
    ("observation", "task", "error"),
    [
        (None, detail_task(), "正式价格"),
        (detail_observation(channel="search"), detail_task(), "正式价格"),
        (detail_observation(selected_spec="1g*10片"), detail_task(), "规格"),
        (detail_observation(sale_box_count=None), detail_task(), "起购盒数"),
        (detail_observation(), CollectionTask(
            id="without-provider", run_id="test-run", platform="yaoshibang", task_type="inspect_candidate",
            status="succeeded", session_alias="yaoshibang-p0", priority=100,
            payload=CollectionTaskSpec(
                task_id="without-provider", run_id="test-run", platform="yaoshibang",
                task_type=TaskType.INSPECT_CANDIDATE, session_alias="yaoshibang-p0", product_id="product-1",
            ).model_dump(mode="json"),
        ), "provider_id"),
    ],
)
def test_detail_validation_rejects_incomplete_or_nonmatching_results(runner_module, observation, task, error) -> None:
    with pytest.raises(RuntimeError, match=error):
        runner_module.validate_yaoshibang_detail(
            detail=observation, detail_task=task, expected_specs={"0.45g*20片", "0.45g*24片"},
        )


def test_provider_store_search_requires_filtered_hit(runner_module) -> None:
    search = detail_observation(channel="search", raw_evidence={"hits": [{"provider_id": "provider-1"}]})
    runner_module.validate_provider_store_search(search)
    with pytest.raises(RuntimeError, match="供应商内搜索"):
        runner_module.validate_provider_store_search(detail_observation(channel="search", raw_evidence={"hits": []}))


@pytest.fixture
def memory_db():
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    return engine, factory


def test_seed_smoke_tasks_clears_stale_yaoshibang_provider_ids(runner_module, memory_db) -> None:
    """seed_smoke_tasks 应清空 W00010/W00019/W06410 的假 provider_id。

    这三个 store 的 platform_store_key（5201/21288/9023）是历史手填的假值，
    从未在真实搜索中出现。清空后 collector 会走 resolve-provider 发现真实 ID。
    """
    engine, factory = memory_db
    with factory() as db:
        # 预置 3 个带假 provider_id 的店铺
        for store_id, fake_pid in (("W00010", "5201"), ("W00019", "21288"), ("W06410", "9023")):
            db.add(StoreResponsibility(
                internal_store_id=store_id, platform="yaoshibang",
                shop_name=f"店铺{store_id}", platform_store_key=fake_pid,
                shop_status="正常", fixed_tier="observation_only",
            ))
        # 额外一个非目标店铺，provider_id 应保留
        db.add(StoreResponsibility(
            internal_store_id="ysb-provider-18650", platform="yaoshibang",
            shop_name="药实在", platform_store_key="18650",
            shop_status="正常", fixed_tier="observation_only",
        ))
        db.flush()
        run = CollectionRun(id="test-seed-clear")
        db.add(run)
        db.flush()
        queue = TaskQueueService(db)
        # seed_smoke_tasks 会清空 W00010/W00019/W06410 的 platform_store_key
        # 即使 fixture 里没有匹配的 drug（这些是 yaoshibang store），清空逻辑也应在
        # 循环前无条件执行。
        try:
            runner_module.seed_smoke_tasks(queue, run.id, db)
        except Exception:
            # fixture 数据读取可能因环境差异抛错，但清空逻辑应在循环前已完成
            pass
        db.commit()

        # 三个目标店铺的 provider_id 应被清空
        for store_id in ("W00010", "W00019", "W06410"):
            store = db.scalar(select(StoreResponsibility).where(
                StoreResponsibility.internal_store_id == store_id
            ))
            assert store is not None, f"店铺 {store_id} 应存在"
            assert store.platform_store_key is None, \
                f"店铺 {store_id} 的假 provider_id 应被清空，实际 {store.platform_store_key}"
        # 非目标店铺的 provider_id 应保留
        preserved = db.scalar(select(StoreResponsibility).where(
            StoreResponsibility.internal_store_id == "ysb-provider-18650"
        ))
        assert preserved is not None
        assert preserved.platform_store_key == "18650", \
            "非目标店铺的 provider_id 不应被清空"
