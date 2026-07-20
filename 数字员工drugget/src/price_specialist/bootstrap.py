from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import BRAND_TO_GENERIC, parse_control_prices, parse_package_units
from .data_quality import ANTUO_FILE, QUWEI_FILE, STORE_FILE, STORE_SNAPSHOT_FILE, load_store_records, valid_value
from .models import (
    ControlPriceVersion,
    DataQualityIssue,
    DrugProduct,
    MonitorTarget,
    PackageMaster,
    SourceDataset,
    StoreResponsibility,
)

if TYPE_CHECKING:
    from .data_quality import DataAuditReport


def bootstrap_reference_data(
    session: Session,
    *,
    source_dir: Path,
    smoke_plan_path: Path | None = None,
    smoke_plan: dict[str, Any] | None = None,
    audit_report: "DataAuditReport | None" = None,
) -> dict[str, int]:
    """Load curated reference data; source workbooks and legacy JSON remain read-only."""
    if smoke_plan is not None:
        plan = smoke_plan
    elif smoke_plan_path is not None:
        plan = json.loads(smoke_plan_path.read_text(encoding="utf-8"))
    else:
        raise ValueError("smoke_plan_path or smoke_plan is required")
    targets = [*plan.get("jd_targets", []), *plan.get("taobao_targets", [])]
    covered = (
        set(audit_report.covered_brands)
        if audit_report is not None
        else {target.get("brand") for target in targets if target.get("brand")}
    )

    drugs: dict[str, DrugProduct] = {}
    for brand, generic in BRAND_TO_GENERIC.items():
        drug = session.scalar(select(DrugProduct).where(DrugProduct.brand_name == brand))
        if drug is None:
            drug = DrugProduct(
                brand_name=brand,
                generic_name=generic,
                history_covered=brand in covered,
                coverage_status="history_covered" if brand in covered else "search_cold_start",
            )
            session.add(drug)
            session.flush()
        else:
            drug.history_covered = brand in covered
            drug.coverage_status = "history_covered" if brand in covered else "search_cold_start"
        drugs[brand] = drug

    source_datasets: dict[str, SourceDataset] = {}
    if audit_report is not None:
        active_source_names = set(audit_report.source_rows) - {STORE_SNAPSHOT_FILE}
        for stale in list(session.scalars(select(SourceDataset))):
            if stale.name not in active_source_names:
                # The previous implementation recorded a derived JSON snapshot
                # as a source. Remove that generated-only registry row once the
                # actual workbook has been parsed successfully.
                session.delete(stale)
        session.flush()
        for name, row_count in audit_report.source_rows.items():
            if name == STORE_SNAPSHOT_FILE:
                continue
            dataset = session.scalar(select(SourceDataset).where(SourceDataset.name == name))
            if dataset is None:
                dataset_path = source_dir.parent / name if "/" in name else source_dir / name
                dataset = SourceDataset(
                    name=name,
                    path=str(dataset_path),
                    sha256=audit_report.source_hashes[name],
                    row_count=row_count,
                    recognized_count=0,
                    unrecognized_count=0,
                )
                session.add(dataset)
                session.flush()
            dataset.sha256 = audit_report.source_hashes[name]
            dataset.row_count = row_count
            if name == ANTUO_FILE:
                dataset.recognized_count = row_count
                dataset.unrecognized_count = 0
            elif name == QUWEI_FILE:
                dataset.unrecognized_count = audit_report.unrecognized_rows
                dataset.recognized_count = row_count - audit_report.unrecognized_rows
            source_datasets[name] = dataset
        for issue in audit_report.issues:
            dataset = source_datasets.get(issue.source)
            if dataset is None:
                continue
            exists = session.scalar(
                select(DataQualityIssue.id).where(
                    DataQualityIssue.dataset_id == dataset.id,
                    DataQualityIssue.issue_type == issue.issue_type,
                    DataQualityIssue.row_number == issue.row_number,
                ).limit(1)
            )
            if exists is None:
                session.add(
                    DataQualityIssue(
                        dataset_id=dataset.id,
                        issue_type=issue.issue_type,
                        severity=issue.severity,
                        row_number=issue.row_number,
                        business_key=issue.business_key,
                        details=issue.details,
                        quarantined=issue.quarantined,
                    )
                )

        for sample in audit_report.packages:
            drug = drugs[sample["brand"]]
            existing_package = session.scalar(
                select(PackageMaster).where(
                    PackageMaster.drug_id == drug.id,
                    PackageMaster.spec_normalized == sample["spec_normalized"],
                )
            )
            if existing_package is None:
                sources = list(sample.get("sources") or [])
                session.add(
                    PackageMaster(
                        drug_id=drug.id,
                        spec_raw=str((sample.get("spec_raw_examples") or [sample["spec_normalized"]])[0]),
                        spec_normalized=sample["spec_normalized"],
                        units_per_box=sample.get("units_per_box"),
                        min_unit=sample.get("min_unit"),
                        source=";".join(sources),
                        evidence={
                            "sources": sources,
                            "record_count": sample.get("record_count"),
                            "spec_raw_examples": sample.get("spec_raw_examples"),
                        },
                        verified=len(sources) >= 2,
                    )
                )

    control_entries = parse_control_prices(source_dir / "价格标准表.md")
    for entry in control_entries:
        drug = drugs[entry.brand]
        existing = session.scalar(
            select(ControlPriceVersion).where(
                ControlPriceVersion.drug_id == drug.id,
                ControlPriceVersion.spec_key == entry.spec_key,
                ControlPriceVersion.active.is_(True),
            )
        )
        if existing is None:
            session.add(
                ControlPriceVersion(
                    drug_id=drug.id,
                    spec_key=entry.spec_key,
                    price_per_min_unit=entry.price,
                    min_unit=entry.min_unit,
                    effective_from=date.today(),
                    source=str(source_dir / "价格标准表.md"),
                    source_line=entry.source_line,
                    active=True,
                )
            )

    stores: dict[tuple[str, str], StoreResponsibility] = {}
    stores_by_internal_id: dict[str, StoreResponsibility] = {}
    store_records = load_store_records(source_dir / STORE_FILE)
    if not store_records:
        store_records = json.loads(
            (source_dir.parent / STORE_SNAPSHOT_FILE).read_text(encoding="utf-8")
        )["stores"]
    for row in store_records:
        internal_id = str(row["店铺ID"])
        raw_platform = str(row.get("平台") or "")
        platform = {
            "京东": "jd",
            "天猫": "taobao",
            "淘宝": "taobao",
            "美团": "meituan",
            "拼多多": "pinduoduo",
            "药师帮": "yaoshibang",
            "1药城": "yiyaocheng",
            "京东/O2O": "jd_o2o",
        }.get(raw_platform, raw_platform)
        shop_name = str(row.get("店铺名称") or "")
        store = session.scalar(
            select(StoreResponsibility).where(StoreResponsibility.internal_store_id == internal_id)
        )
        has_responsibility = any(valid_value(row.get(key)) for key in ("责任单位", "责任人", "联系人"))
        fixed_tier = (
            "responsibility_core"
            if row.get("店铺状态") == "正常" and valid_value(row.get("涉及品种")) and has_responsibility
            else "observation_only"
        )
        if store is None:
            store = StoreResponsibility(
                internal_store_id=internal_id,
                platform=platform,
                shop_name=shop_name,
                shop_status=str(row.get("店铺状态") or ""),
                fixed_tier=fixed_tier,
            )
            session.add(store)
        store.platform_store_key=str(row.get("平台&店铺") or "") or None
        store.responsible_unit=str(row.get("责任单位") or "") or None
        store.responsible_person=str(row.get("责任人") or "") or None
        store.contact=str(row.get("联系人") or "") or None
        store.involved_products=str(row.get("涉及品种") or "") or None
        store.fixed_tier=fixed_tier
        stores[(platform, shop_name)] = store
        stores_by_internal_id[internal_id] = store
    session.flush()

    package_count = 0
    target_count = 0
    for target in targets:
        platform = target["platform"]
        shop_name = target["shop_name"]
        store_key = (platform, shop_name)
        store = stores_by_internal_id.get(str(target["internal_store_id"])) or stores.get(store_key)
        if store is None:
            store = StoreResponsibility(
                internal_store_id=str(target["internal_store_id"]),
                platform=platform,
                platform_store_key=None,
                shop_name=shop_name,
                shop_status="正常",
                responsible_unit=target.get("responsible_unit"),
                responsible_person=target.get("responsible_person"),
                contact=target.get("contact"),
                involved_products=target.get("involved_products_raw"),
                fixed_tier=str(target["fixed_tier"]),
            )
            session.add(store)
            session.flush()
        else:
            # A selected target can qualify through stable historical drug
            # evidence even when the archive's “涉及品种” cell is blank.
            store.fixed_tier = str(target["fixed_tier"])
        stores[store_key] = store

        brand = target.get("brand")
        product_id = target.get("product_id")
        spec = target.get("spec_normalized")
        if not brand or brand not in drugs or not product_id or not spec or not target.get("url"):
            continue
        drug = drugs[brand]
        package = session.scalar(
            select(PackageMaster).where(
                PackageMaster.drug_id == drug.id,
                PackageMaster.spec_normalized == spec,
            )
        )
        if package is None:
            units, min_unit = parse_package_units(spec)
            session.add(
                PackageMaster(
                    drug_id=drug.id,
                    spec_raw=str(target.get("spec_raw") or spec),
                    spec_normalized=spec,
                    units_per_box=units,
                    min_unit=min_unit,
                    source="historical_smoke_plan",
                    evidence={"target_url": target["url"], "eligibility_basis": target.get("eligibility_basis")},
                    verified=False,
                )
            )
            package_count += 1
        existing_target = session.scalar(
            select(MonitorTarget).where(
                MonitorTarget.platform == platform,
                MonitorTarget.product_id == str(product_id),
                MonitorTarget.drug_id == drug.id,
            )
        )
        if existing_target is None:
            existing_target = MonitorTarget(
                drug_id=drug.id,
                platform=platform,
                product_id=str(product_id),
                spec_raw=str(target.get("spec_raw") or spec),
                spec_normalized=spec,
                url=target["url"],
                fixed_tier=str(target["fixed_tier"]),
            )
            session.add(existing_target)
            target_count += 1
        existing_target.store_id = store.id
        existing_target.url = target["url"]
        existing_target.spec_raw = str(target.get("spec_raw") or spec)
        existing_target.spec_normalized = spec
        existing_target.fixed_tier = str(target["fixed_tier"])
        existing_target.stable_link = bool(target.get("stable_link"))
        existing_target.stable_link_evidence = {
            **(target.get("stable_link_evidence") or {}),
            "historical_box_count": target.get("history_box_count"),
            "eligibility_basis": target.get("eligibility_basis"),
        }
        existing_target.enabled = bool(target.get("enabled"))
    session.flush()
    return {
        "drugs": len(drugs),
        "control_prices": len(control_entries),
        "stores": len(stores),
        "new_packages": package_count,
        "new_targets": target_count,
        "source_datasets": len(source_datasets),
        "quality_issues": len(audit_report.issues) if audit_report is not None else 0,
    }
