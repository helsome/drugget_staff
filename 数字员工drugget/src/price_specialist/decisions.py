from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import ControlPriceEntry, normalize_spec, parse_package_units
from .errors import AmbiguousControlPrice
from .models import CollectionTask, ControlPriceVersion, DrugProduct, PackageMaster, PriceComparison, PriceObservation
from .pricing import resolve_control_price


NOT_COMPARABLE = "not_comparable"
BELOW_CONTROL = "below_control"
NOT_BELOW_CONTROL = "not_below_control"


def _snapshot(observation: PriceObservation, task: CollectionTask | None, brand: str | None) -> dict[str, Any]:
    return {
        "observation_id": observation.id,
        "task_id": observation.task_id,
        "run_id": observation.run_id,
        "platform": task.platform if task else None,
        "brand": brand,
        "page_shop": observation.page_shop,
        "final_url": observation.final_url,
        "page_title": observation.page_title,
        "selected_spec": observation.selected_spec,
        "page_price": str(observation.page_price_value) if observation.page_price_value is not None else observation.page_price_raw,
        "single_box_price": str(observation.single_box_price) if observation.single_box_price is not None else None,
        "single_unit_price": str(observation.single_unit_price) if observation.single_unit_price is not None else None,
        "evidence_path": observation.evidence_path,
        "evidence_sha256": observation.evidence_sha256,
    }


class PriceDecisionService:
    """Persist strict, evidence-linked decisions without relaxing package rules."""

    def __init__(self, session: Session):
        self.session = session

    def _replace(self, comparison: PriceComparison, **values: Any) -> PriceComparison:
        for name, value in values.items():
            setattr(comparison, name, value)
        self.session.flush()
        return comparison

    def evaluate_observation(self, observation_id: str, *, as_of: date | None = None) -> PriceComparison:
        observation = self.session.get(PriceObservation, observation_id)
        if observation is None:
            raise ValueError(f"price observation not found: {observation_id}")
        existing = self.session.scalar(select(PriceComparison).where(PriceComparison.observation_id == observation_id))
        comparison = existing or PriceComparison(observation_id=observation_id, verdict=NOT_COMPARABLE, reason_code="pending")
        if existing is None:
            self.session.add(comparison)

        task = self.session.get(CollectionTask, observation.task_id)
        payload = task.payload if task else {}
        metadata = payload.get("metadata") or {}
        brand = payload.get("drug_name") or metadata.get("target_brand")
        drug_id = metadata.get("drug_id")
        drug = self.session.get(DrugProduct, drug_id) if drug_id else None
        if drug is None and brand:
            drug = self.session.scalar(select(DrugProduct).where(DrugProduct.brand_name == brand))
        brand = drug.brand_name if drug else brand
        evidence = _snapshot(observation, task, brand)
        common = {"detail_evidence_snapshot": evidence, "rule_snapshot": {}, "control_price_version_id": None}
        if observation.collection_status != "success" or observation.single_unit_price is None:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="formal_detail_price_missing", reason_detail="详情正式价格或最小单位价格缺失", comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=observation.min_unit, difference=None, **common)
        if drug is None or not brand:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="drug_identity_missing", reason_detail="详情任务缺少可审计药品身份", comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=observation.min_unit, difference=None, **common)
        spec = normalize_spec(observation.selected_spec)
        if not spec:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="detail_spec_missing", reason_detail="详情未确认完整规格", comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=observation.min_unit, difference=None, **common)
        package = self.session.scalar(select(PackageMaster).where(PackageMaster.drug_id == drug.id, PackageMaster.spec_normalized == spec, PackageMaster.verified.is_(True)))
        # A confirmed detail normalization is itself packaging evidence. This
        # fallback is deliberately narrow: it never invents a box count/unit
        # and is only used when the observation already persisted both values.
        package_min_unit = package.min_unit if package else observation.min_unit
        package_units = package.units_per_box if package else observation.units_per_box
        spec_units, spec_unit = parse_package_units(spec)
        if package_min_unit is None or package_units is None or spec_units is None or spec_unit != package_min_unit:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="package_unverified", reason_detail=f"未验证包装规格：{spec}", comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=observation.min_unit, difference=None, **common)
        if observation.min_unit != package_min_unit:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="package_unit_mismatch", reason_detail="详情最小单位与包装主数据不一致", comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=observation.min_unit, difference=None, **common)
        rows = list(self.session.scalars(select(ControlPriceVersion).where(ControlPriceVersion.drug_id == drug.id, ControlPriceVersion.active.is_(True))))
        when = as_of or observation.captured_at.date()
        entries = [ControlPriceEntry(brand=brand, generic_name=drug.generic_name, spec_key=row.spec_key, price=Decimal(row.price_per_min_unit), min_unit=row.min_unit, source_line=row.source_line, effective_from=row.effective_from, effective_to=row.effective_to, active=row.active, business_confirmed=row.business_confirmed, source_file=row.source, source_line_number=row.source_line_number, confirmed_by=row.confirmed_by, confirmed_at=row.confirmed_at, approval_reference=row.approval_reference, authority_basis=row.authority_basis, source_sha256=row.source_sha256) for row in rows]
        try:
            matched = resolve_control_price(entries, brand=brand, spec=spec, on_date=when)
        except AmbiguousControlPrice as exc:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="control_rule_ambiguous", reason_detail=exc.message, comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=package_min_unit, difference=None, **common)
        if matched is None:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="exact_confirmed_control_rule_missing", reason_detail=f"{brand} {spec} 在 {when.isoformat()} 没有唯一、有效的指导价", comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=package_min_unit, difference=None, **common)
        rule = next(row for row in rows if row.spec_key == matched.spec_key and Decimal(row.price_per_min_unit) == matched.price and row.min_unit == matched.min_unit and row.source_line == matched.source_line and row.source_sha256 == matched.source_sha256)
        if rule.min_unit != package_min_unit:
            return self._replace(comparison, verdict=NOT_COMPARABLE, reason_code="control_rule_unit_mismatch", reason_detail="控价最小单位与包装主数据不一致", comparison_unit_price=observation.single_unit_price, control_price=None, min_unit=package_min_unit, difference=None, **common)
        difference = (Decimal(rule.price_per_min_unit) - Decimal(observation.single_unit_price)).quantize(Decimal("0.0001"))
        verdict = BELOW_CONTROL if difference > 0 else NOT_BELOW_CONTROL
        rule_snapshot = {"id": rule.id, "spec_key": rule.spec_key, "price_per_min_unit": str(rule.price_per_min_unit), "min_unit": rule.min_unit, "effective_from": rule.effective_from.isoformat(), "effective_to": rule.effective_to.isoformat() if rule.effective_to else None, "source_file": rule.source, "source_line_number": rule.source_line_number, "source_line": rule.source_line, "business_confirmed": rule.business_confirmed, "confirmed_by": rule.confirmed_by, "confirmed_at": rule.confirmed_at.isoformat() if rule.confirmed_at else None, "approval_reference": rule.approval_reference, "authority_basis": rule.authority_basis, "source_sha256": rule.source_sha256}
        return self._replace(comparison, verdict=verdict, reason_code="exact_rule_matched", reason_detail=None, comparison_unit_price=observation.single_unit_price, control_price=rule.price_per_min_unit, min_unit=package_min_unit, difference=difference, control_price_version_id=rule.id, rule_snapshot=rule_snapshot, detail_evidence_snapshot=evidence)
