from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from price_specialist.alerts import AlertDryRunService
from price_specialist.decisions import BELOW_CONTROL, NOT_BELOW_CONTROL, NOT_COMPARABLE, PriceDecisionService
from price_specialist.models import Base, CollectionRun, CollectionTask, ControlPriceVersion, DrugProduct, PackageMaster, PriceBreakEvent, PriceObservation, StoreResponsibility


def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def detail(s, *, price="0.8085", shop="药实在"):
    drug = DrugProduct(brand_name="葛泰", generic_name="地奥司明片")
    run = CollectionRun()
    s.add_all([drug, run]); s.flush()
    task = CollectionTask(run_id=run.id, platform="yaoshibang", task_type="inspect_candidate", status="succeeded", session_alias="test", payload={"drug_name": "葛泰", "metadata": {"drug_id": drug.id}})
    s.add(task); s.flush()
    observation = PriceObservation(run_id=run.id, task_id=task.id, channel="detail", captured_at=datetime(2026, 7, 20), page_shop=shop, selected_spec="0.45g*20片", page_price_value=Decimal("16.17"), single_box_price=Decimal("16.17"), single_unit_price=Decimal(price), min_unit="片", collection_status="success", calculation_status="success", price_status="not_evaluated", evidence_path="/evidence/detail.json", evidence_sha256="abc")
    s.add_all([PackageMaster(drug_id=drug.id, spec_raw="0.45g*20片", spec_normalized="0.45g*20片", units_per_box=Decimal("20"), min_unit="片", source="test", verified=True), observation]); s.flush()
    return drug, observation


def test_current_incomplete_getai_rule_is_not_comparable():
    s = session(); drug, observation = detail(s)
    s.add(ControlPriceVersion(drug_id=drug.id, spec_key=None, price_per_min_unit=Decimal("1.15"), min_unit="片", effective_from=date(2026, 4, 1), source="价格标准表.md", source_line="地奥司明片葛泰1.15", active=True, business_confirmed=False))
    s.flush()
    comparison = PriceDecisionService(s).evaluate_observation(observation.id)
    assert comparison.verdict == NOT_COMPARABLE
    assert comparison.reason_code == "exact_confirmed_control_rule_missing"
    assert AlertDryRunService().ensure_event(s, comparison=comparison) is None


def test_below_case_and_preview_are_idempotent():
    s = session(); drug, observation = detail(s, shop="责任店")
    store = StoreResponsibility(internal_store_id="S1", platform="yaoshibang", shop_name="责任店", shop_status="正常", responsible_unit="销售", responsible_person="张三", contact="zhangsan", fixed_tier="responsibility_core")
    rule = ControlPriceVersion(drug_id=drug.id, spec_key="0.45g*20片", price_per_min_unit=Decimal("1.15"), min_unit="片", effective_from=date(2026, 4, 1), source="审批表", source_line="葛泰 0.45g*20片 1.15", active=True, business_confirmed=True, confirmed_by="业务", confirmed_at=date(2026, 7, 20), approval_reference="APP-1")
    s.add_all([store, rule]); s.flush()
    service = PriceDecisionService(s)
    comparison = service.evaluate_observation(observation.id)
    assert comparison.verdict == BELOW_CONTROL
    alerts = AlertDryRunService()
    first = alerts.ensure_event(s, comparison=comparison)
    assert first is not None
    assert alerts.ensure_event(s, comparison=comparison).id == first.id
    preview = alerts.route_preview(s, event=first)
    assert preview["send"] is False
    assert preview["routing_status"] == "routed_dry_run"
    assert len(list(s.scalars(select(PriceBreakEvent)))) == 1


def test_exact_rule_at_or_above_is_not_below_without_event():
    s = session(); drug, observation = detail(s, price="1.1500")
    s.add(ControlPriceVersion(drug_id=drug.id, spec_key="0.45g*20片", price_per_min_unit=Decimal("1.15"), min_unit="片", effective_from=date(2026, 4, 1), source="审批表", source_line="葛泰", active=True, business_confirmed=True))
    s.flush()
    comparison = PriceDecisionService(s).evaluate_observation(observation.id)
    assert comparison.verdict == NOT_BELOW_CONTROL
    assert AlertDryRunService().ensure_event(s, comparison=comparison) is None
