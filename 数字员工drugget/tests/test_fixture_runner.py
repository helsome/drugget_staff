from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from price_specialist.enums import TaskType
from price_specialist.models import CollectionTask, PriceObservation
from price_specialist.schemas import CollectionTaskSpec


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_DIR = PROJECT_ROOT / "采集器"


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
