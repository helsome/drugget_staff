import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from price_specialist.catalog import CONTROL_PRICE_RULE_FIELDNAMES, import_control_price_rules, parse_control_price_rules, parse_control_prices
from price_specialist.decisions import BELOW_CONTROL, PriceDecisionService
from price_specialist.models import Base, CollectionRun, CollectionTask, DrugProduct, PackageMaster, PriceObservation
from price_specialist.bootstrap import sync_control_price_rules


ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("control_workflow_builder", ROOT / "scripts/build_knowledge_base.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); sys.modules["control_workflow_builder"] = module
    spec.loader.exec_module(module)
    return module


def write_rules(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTROL_PRICE_RULE_FIELDNAMES, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def approved_getai_box_rule() -> dict[str, str]:
    return {
        "brand": "葛泰", "generic_name": "地奥司明片", "spec_key": "0.45g*20片",
        "control_price_value": "23.00", "control_price_basis": "per_box",
        "control_price_per_min_unit": "", "min_unit": "片", "effective_from": "2026-07-01",
        "effective_to": "", "active": "True", "source_file": "葛泰审批单.pdf",
        "source_line": "葛泰 0.45g*20片 每盒23元", "business_confirmed": "True",
        "confirmed_by": "业务审批人", "confirmed_at": "2026-07-20", "approval_reference": "GT-APP-001",
    }


def test_rebuild_import_and_judge_preserves_approved_box_rule(tmp_path: Path):
    builder = load_builder()
    knowledge_base = tmp_path / "knowledge-base"
    knowledge_base.mkdir()
    write_rules(knowledge_base / "control_price_rules.csv", [])
    incoming = tmp_path / "approved.csv"
    write_rules(incoming, [approved_getai_box_rule()])
    import_control_price_rules(input_path=incoming, target_path=knowledge_base / "control_price_rules.csv")
    preserved = builder.existing_control_metadata(knowledge_base)
    rebuilt = builder.rebuilt_control_records(parse_control_prices(ROOT / "data/raw/价格标准表.md"), preserved, "价格标准表.md")
    rebuilt_path = tmp_path / "rebuilt.csv"
    write_rules(rebuilt_path, rebuilt)
    rules = [rule for rule in parse_control_price_rules(rebuilt_path) if rule.approval_reference == "GT-APP-001"]
    assert len(rules) == 1
    assert rules[0].price == Decimal("1.15")
    assert rules[0].effective_to is None

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    drug = DrugProduct(brand_name="葛泰", generic_name="地奥司明片")
    run = CollectionRun(); session.add_all([drug, run]); session.flush()
    task = CollectionTask(run_id=run.id, platform="yaoshibang", task_type="inspect_candidate", status="succeeded", session_alias="test", payload={"drug_name": "葛泰", "metadata": {"drug_id": drug.id}})
    session.add(task); session.flush()
    session.add(PackageMaster(drug_id=drug.id, spec_raw="0.45g*20片", spec_normalized="0.45g*20片", units_per_box=Decimal("20"), min_unit="片", source="test", verified=True))
    observation = PriceObservation(run_id=run.id, task_id=task.id, channel="detail", captured_at=datetime(2026, 7, 20), selected_spec="0.45g*20片", page_price_value=Decimal("16.17"), single_unit_price=Decimal("0.8085"), min_unit="片", collection_status="success", calculation_status="success", price_status="not_evaluated")
    session.add(observation); session.flush()
    rule = rules[0]
    from price_specialist.models import ControlPriceVersion
    session.add(ControlPriceVersion(drug_id=drug.id, spec_key=rule.spec_key, price_per_min_unit=rule.price, min_unit=rule.min_unit, effective_from=rule.effective_from, effective_to=rule.effective_to, source=rule.source_file or "", source_line=rule.source_line, source_line_number=rule.source_line_number, active=rule.active, business_confirmed=rule.business_confirmed, confirmed_by=rule.confirmed_by, confirmed_at=rule.confirmed_at, approval_reference=rule.approval_reference))
    session.flush()
    decision = PriceDecisionService(session).evaluate_observation(observation.id)
    assert decision.verdict == BELOW_CONTROL
    assert decision.rule_snapshot["approval_reference"] == "GT-APP-001"


def test_control_rule_sync_uses_validated_csv_not_manual_sqlite(tmp_path: Path):
    source = tmp_path / "approved.csv"
    write_rules(source, [approved_getai_box_rule()])
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(DrugProduct(brand_name="葛泰", generic_name="地奥司明片")); session.flush()
    assert sync_control_price_rules(session, control_path=source) == {"input": 1, "added": 1, "updated": 0}
    assert sync_control_price_rules(session, control_path=source) == {"input": 1, "added": 0, "updated": 1}
