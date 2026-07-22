"""Tests for StoreTaskPlanner and store search task generation.

These tests verify that store search tasks are generated with explicit
boundaries, preventing the unbounded Cartesian product of
``all drugs x all stores``.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from price_specialist.database import create_db_engine, init_database, make_session_factory
from price_specialist.enums import StoreSelectionMode
from price_specialist.models import DrugProduct, MonitorTarget, StoreResponsibility
from price_specialist.services import DrugSelection, PlannedStoreResult, StoreTaskPlanner


@pytest.fixture
def memory_db():
    engine = create_db_engine("sqlite:///:memory:")
    init_database(engine)
    factory = make_session_factory(engine)
    return engine, factory


def _create_drug(db, *, brand_name: str = "托妥", generic_name: str = "瑞舒伐他汀钙片") -> DrugProduct:
    drug = DrugProduct(brand_name=brand_name, generic_name=generic_name)
    db.add(drug)
    db.flush()
    return drug


def _create_store(
    db, *,
    platform: str = "taobao",
    shop_name: str = "测试店铺",
    internal_store_id: str | None = None,
    shop_home_url: str | None = "https://shop.taobao.com/test",
    platform_store_key: str | None = None,
    identity_status: str = "active",
    involved_products: str | None = None,
) -> StoreResponsibility:
    store = StoreResponsibility(
        internal_store_id=internal_store_id or f"store-{shop_name}",
        platform=platform,
        shop_name=shop_name,
        shop_home_url=shop_home_url,
        platform_store_key=platform_store_key,
        shop_status="正常",
        fixed_tier="observation_only",
        identity_status=identity_status,
        involved_products=involved_products,
    )
    db.add(store)
    db.flush()
    return store


def _link_drug_store(db, drug: DrugProduct, store: StoreResponsibility) -> MonitorTarget:
    target = MonitorTarget(
        drug_id=drug.id,
        store_id=store.id,
        platform=store.platform,
        product_id=f"prod-{store.internal_store_id}",
        spec_raw="10mg*28片",
        spec_normalized="10mg*28片",
        url="https://example.com/item",
        fixed_tier="observation_only",
    )
    db.add(target)
    db.flush()
    return target


# ---------------------------------------------------------------------------
# Test 1: Default does NOT create Cartesian product
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_does_not_create_cartesian_product(memory_db) -> None:
    """RESPONSIBILITY_ONLY mode should only return stores with drug relationships."""
    engine, factory = memory_db
    with factory() as db:
        drug_a = _create_drug(db, brand_name="托妥", generic_name="瑞舒伐他汀钙片")
        drug_b = _create_drug(db, brand_name="晴诺舒", generic_name="米拉贝隆缓释片")

        store_a = _create_store(db, platform="taobao", shop_name="店铺A",
                                internal_store_id="taobao-store-a")
        store_b = _create_store(db, platform="taobao", shop_name="店铺B",
                                internal_store_id="taobao-store-b",
                                shop_home_url=None)  # No shop_home_url

        # Only drug_a is linked to store_a
        _link_drug_store(db, drug_a, store_a)

        planner = StoreTaskPlanner(db)

        # drug_a should have store_a eligible (linked, has shop_home_url)
        results_a = planner.eligible_stores(
            platform="taobao", drug=drug_a,
            selection=StoreSelectionMode.RESPONSIBILITY_ONLY,
        )
        eligible_a = [r for r in results_a if r.eligible]
        assert len(eligible_a) == 1, (
            f"drug_a 应只有 1 个可用店铺 (store_a)，实际 {len(eligible_a)}"
        )
        assert eligible_a[0].store is not None
        assert eligible_a[0].store.shop_name == "店铺A"

        # drug_b should have zero eligible stores (no relationship)
        results_b = planner.eligible_stores(
            platform="taobao", drug=drug_b,
            selection=StoreSelectionMode.RESPONSIBILITY_ONLY,
        )
        eligible_b = [r for r in results_b if r.eligible]
        assert len(eligible_b) == 0, (
            f"drug_b 应没有可用店铺，实际 {len(eligible_b)}"
        )


# ---------------------------------------------------------------------------
# Test 2: Only drug-related stores
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_drug_related_stores(memory_db) -> None:
    """Only stores with MonitorTarget links to the drug should be eligible."""
    engine, factory = memory_db
    with factory() as db:
        drug = _create_drug(db)
        store_related = _create_store(db, platform="taobao", shop_name="相关店铺",
                                      internal_store_id="related")
        store_unrelated = _create_store(db, platform="taobao", shop_name="无关店铺",
                                        internal_store_id="unrelated")

        _link_drug_store(db, drug, store_related)

        planner = StoreTaskPlanner(db)
        results = planner.eligible_stores(
            platform="taobao", drug=drug,
            selection=StoreSelectionMode.RESPONSIBILITY_ONLY,
        )
        eligible = [r for r in results if r.eligible]
        store_names = {r.store.shop_name for r in eligible if r.store}
        assert "相关店铺" in store_names
        assert "无关店铺" not in store_names
        assert len(eligible) == 1


# ---------------------------------------------------------------------------
# Test 3: Taobao without shop_home_url is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_taobao_no_shop_home_url_skipped(memory_db) -> None:
    """Taobao stores without shop_home_url should be skipped."""
    engine, factory = memory_db
    with factory() as db:
        drug = _create_drug(db)
        store_with_url = _create_store(db, platform="taobao", shop_name="有主页店铺",
                                       internal_store_id="with-url",
                                       shop_home_url="https://shop.taobao.com/valid")
        store_without_url = _create_store(db, platform="taobao", shop_name="无主页店铺",
                                          internal_store_id="without-url",
                                          shop_home_url=None)

        _link_drug_store(db, drug, store_with_url)
        _link_drug_store(db, drug, store_without_url)

        planner = StoreTaskPlanner(db)
        results = planner.eligible_stores(
            platform="taobao", drug=drug,
            selection=StoreSelectionMode.RESPONSIBILITY_ONLY,
        )
        eligible = [r for r in results if r.eligible]
        skipped = [r for r in results if not r.eligible]
        store_names = {r.store.shop_name for r in eligible if r.store}

        assert "有主页店铺" in store_names
        assert "无主页店铺" not in store_names
        assert len(eligible) == 1
        assert any(r.reason == "missing_shop_home_url" for r in skipped)
        # Verify the skipped store is the one without URL
        skipped_without_url = [r for r in skipped if r.reason == "missing_shop_home_url"]
        assert len(skipped_without_url) == 1
        assert skipped_without_url[0].store is not None
        assert skipped_without_url[0].store.shop_name == "无主页店铺"


# ---------------------------------------------------------------------------
# Test 4: Yaoshibang without trusted provider_id is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_yaoshibang_no_provider_id_skipped(memory_db) -> None:
    """Yaoshibang stores without trusted provider_id should be skipped."""
    engine, factory = memory_db
    with factory() as db:
        drug = _create_drug(db)
        store_with_provider = _create_store(db, platform="yaoshibang", shop_name="有ID店铺",
                                            internal_store_id="ysb-with-id",
                                            shop_home_url=None,
                                            platform_store_key="provider-real-123")
        store_without_provider = _create_store(db, platform="yaoshibang", shop_name="无ID店铺",
                                               internal_store_id="ysb-without-id",
                                               shop_home_url=None,
                                               platform_store_key=None)
        store_fake_provider = _create_store(db, platform="yaoshibang", shop_name="假ID店铺",
                                            internal_store_id="W00010",
                                            shop_home_url=None,
                                            platform_store_key="W00010")

        _link_drug_store(db, drug, store_with_provider)
        _link_drug_store(db, drug, store_without_provider)
        _link_drug_store(db, drug, store_fake_provider)

        # Run sanitize first to clear fake provider_ids
        StoreTaskPlanner.sanitize_fake_provider_ids(db)

        planner = StoreTaskPlanner(db)
        results = planner.eligible_stores(
            platform="yaoshibang", drug=drug,
            selection=StoreSelectionMode.RESPONSIBILITY_ONLY,
        )
        eligible = [r for r in results if r.eligible]
        skipped = [r for r in results if not r.eligible]
        eligible_names = {r.store.shop_name for r in eligible if r.store}

        assert "有ID店铺" in eligible_names
        assert "无ID店铺" not in eligible_names
        assert "假ID店铺" not in eligible_names
        assert len(eligible) == 1

        # Check that skipped stores have correct reasons
        missing_provider = [r for r in skipped if r.reason == "missing_provider_id"]
        assert len(missing_provider) == 2, "应有 2 个缺少 provider_id 的店铺"
        assert all(r.need_identity_resolution for r in missing_provider)


# ---------------------------------------------------------------------------
# Test 5: Manual store selection only creates selected stores
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_selection_only_selected(memory_db) -> None:
    """MANUAL mode should only include explicitly selected stores."""
    engine, factory = memory_db
    with factory() as db:
        drug = _create_drug(db)
        store_a = _create_store(db, platform="taobao", shop_name="店铺A",
                                internal_store_id="manual-a")
        store_b = _create_store(db, platform="taobao", shop_name="店铺B",
                                internal_store_id="manual-b")
        store_c = _create_store(db, platform="taobao", shop_name="店铺C",
                                internal_store_id="manual-c")

        _link_drug_store(db, drug, store_a)
        _link_drug_store(db, drug, store_b)
        _link_drug_store(db, drug, store_c)

        planner = StoreTaskPlanner(db)
        # Only select manual-a and manual-b
        results = planner.eligible_stores(
            platform="taobao", drug=drug,
            selection=StoreSelectionMode.MANUAL,
            manual_store_ids=["manual-a", "manual-b"],
        )
        eligible = [r for r in results if r.eligible]
        skipped = [r for r in results if not r.eligible]
        eligible_names = {r.store.shop_name for r in eligible if r.store}

        assert "店铺A" in eligible_names
        assert "店铺B" in eligible_names
        assert "店铺C" not in eligible_names
        assert len(eligible) == 2
        assert any(r.reason == "not_selected" for r in skipped)


# ---------------------------------------------------------------------------
# Test 6: Empty store collection produces explicit status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_store_collection_produces_explicit_status(memory_db) -> None:
    """When no stores exist on a platform, the planner should return empty."""
    engine, factory = memory_db
    with factory() as db:
        drug = _create_drug(db)
        # No stores at all

        planner = StoreTaskPlanner(db)
        results = planner.eligible_stores(
            platform="taobao", drug=drug,
            selection=StoreSelectionMode.RESPONSIBILITY_ONLY,
        )
        # Should return empty list, not crash
        assert isinstance(results, list)
        assert len(results) == 0, "无店铺时应返回空列表"


# ---------------------------------------------------------------------------
# Test 7: ALL_DANGER mode includes all stores
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_danger_returns_all_stores(memory_db) -> None:
    """ALL_DANGER mode should include all stores regardless of relationship."""
    engine, factory = memory_db
    with factory() as db:
        drug = _create_drug(db)
        store_a = _create_store(db, platform="taobao", shop_name="店铺A",
                                internal_store_id="danger-a")
        store_b = _create_store(db, platform="taobao", shop_name="店铺B",
                                internal_store_id="danger-b")

        # Only link drug to store_a
        _link_drug_store(db, drug, store_a)

        planner = StoreTaskPlanner(db)
        results = planner.eligible_stores(
            platform="taobao", drug=drug,
            selection=StoreSelectionMode.ALL_DANGER,
        )
        eligible = [r for r in results if r.eligible]
        assert len(eligible) == 2, "ALL_DANGER 应返回所有店铺"
        store_names = {r.store.shop_name for r in eligible if r.store}
        assert "店铺A" in store_names
        assert "店铺B" in store_names


# ---------------------------------------------------------------------------
# Test 8: EXECUTABLE_ONLY mode filters by identity status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executable_only_filters_by_identity(memory_db) -> None:
    """EXECUTABLE_ONLY mode should only return stores with executable identity."""
    engine, factory = memory_db
    with factory() as db:
        drug = _create_drug(db)
        store_active = _create_store(db, platform="taobao", shop_name="活跃店铺",
                                     internal_store_id="exec-active",
                                     identity_status="active")
        store_discovered = _create_store(db, platform="taobao", shop_name="已发现店铺",
                                         internal_store_id="exec-discovered",
                                         identity_status="discovered")
        store_retired = _create_store(db, platform="taobao", shop_name="已退休店铺",
                                      internal_store_id="exec-retired",
                                      identity_status="retired")

        _link_drug_store(db, drug, store_active)
        _link_drug_store(db, drug, store_discovered)
        _link_drug_store(db, drug, store_retired)

        planner = StoreTaskPlanner(db)
        results = planner.eligible_stores(
            platform="taobao", drug=drug,
            selection=StoreSelectionMode.EXECUTABLE_ONLY,
        )
        eligible = [r for r in results if r.eligible]
        skipped = [r for r in results if not r.eligible]
        eligible_names = {r.store.shop_name for r in eligible if r.store}

        # active and legacy are executable, discovered and retired are not
        assert "活跃店铺" in eligible_names
        assert "已发现店铺" not in eligible_names, "discovered 状态不可执行"
        assert "已退休店铺" not in eligible_names
        assert len(eligible) == 1, f"预期 1 个可执行店铺，实际 {len(eligible)}"
        assert any(r.reason == "not_executable" for r in skipped)


# ---------------------------------------------------------------------------
# Test 9: Sanitize fake provider_ids
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sanitize_fake_provider_ids(memory_db) -> None:
    """sanitize_fake_provider_ids should clear known fake provider_ids."""
    engine, factory = memory_db
    with factory() as db:
        store_fake = _create_store(db, platform="yaoshibang", shop_name="假ID店铺",
                                   internal_store_id="W00010",
                                   shop_home_url=None,
                                   platform_store_key="W00010")
        store_real = _create_store(db, platform="yaoshibang", shop_name="真实ID店铺",
                                   internal_store_id="ysb-real",
                                   shop_home_url=None,
                                   platform_store_key="provider-real-456")

        cleaned = StoreTaskPlanner.sanitize_fake_provider_ids(db)
        assert cleaned == 1, "应清理 1 个假 provider_id"

        db.flush()
        db.expire_all()

        fake = db.get(StoreResponsibility, store_fake.id)
        assert fake is not None
        assert fake.platform_store_key is None, "假 provider_id 应被清空"

        real = db.get(StoreResponsibility, store_real.id)
        assert real is not None
        assert real.platform_store_key == "provider-real-456", "真实 provider_id 应保留"


# ---------------------------------------------------------------------------
# Test 10: DrugSelection resolve creates drug if not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drug_selection_resolve_creates_drug(memory_db) -> None:
    """DrugSelection.resolve() should create a DrugProduct if not found."""
    engine, factory = memory_db
    with factory() as db:
        selection = DrugSelection.from_generic_name("米拉贝隆缓释片")
        drug = selection.resolve(db)
        assert drug is not None
        assert drug.brand_name == "晴诺舒"
        assert drug.generic_name == "米拉贝隆缓释片"
        assert selection.drug_id == drug.id


# ---------------------------------------------------------------------------
# Test 11: DrugSelection resolve from brand_name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drug_selection_resolve_from_brand(memory_db) -> None:
    """DrugSelection.resolve() should work with brand_name."""
    engine, factory = memory_db
    with factory() as db:
        # First create the drug
        drug = _create_drug(db, brand_name="托妥", generic_name="瑞舒伐他汀钙片")
        db.flush()

        selection = DrugSelection.from_brand_name("托妥")
        resolved = selection.resolve(db)
        assert resolved is not None
        assert resolved.id == drug.id
        assert selection.drug_id == drug.id