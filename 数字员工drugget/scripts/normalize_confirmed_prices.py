"""Normalize verified detail prices without creating alerts or break-price events."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re

from sqlalchemy import select

from price_specialist.catalog import BUSINESS_CONFIRMED_BRAND_ALIASES, find_target_brand, normalize_spec, parse_package_units
from price_specialist.config import Settings
from price_specialist.database import configured_database
from price_specialist.models import CollectionTask, PriceObservation

ROOT = Path(__file__).resolve().parent.parent
RUN_IDS = ("c0809f5f-042d-41d6-a82a-4afdcb0b8344", "f0604593-9c56-4280-a991-1df6a0aa69e6")
FOUR_PLACES = Decimal("0.0001")


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def _brand(task: CollectionTask, observation: PriceObservation) -> str | None:
    expected = str((task.payload or {}).get("drug_name") or "")
    text = " ".join(filter(None, (observation.page_title, observation.selected_spec)))
    if expected and expected in text:
        return expected
    return next((canonical for alias, canonical in BUSINESS_CONFIRMED_BRAND_ALIASES.items() if canonical == expected and alias in text), None)


def _spec(observation: PriceObservation, raw: dict) -> str | None:
    value = observation.selected_spec or raw.get("规格")
    if value:
        return normalize_spec(value)
    match = re.search(r"\d+(?:\.\d+)?\s*(?:mg|g|μg|ug)\s*[*×xX]\s*\d+\s*(?:片|粒|袋|支|丸|胶囊)", observation.page_title or "")
    return normalize_spec(match.group(0)) if match else None


def normalize(session, run_ids: tuple[str, ...] = RUN_IDS) -> list[dict[str, str]]:
    rows = session.execute(
        select(PriceObservation, CollectionTask).join(CollectionTask, PriceObservation.task_id == CollectionTask.id).where(
            PriceObservation.run_id.in_(run_ids), PriceObservation.channel == "detail", PriceObservation.collection_status == "success"
        )
    )
    report = []
    for observation, task in rows:
        brand = _brand(task, observation)
        raw = observation.raw_evidence or {}
        spec = _spec(observation, raw)
        units, unit = parse_package_units(spec)
        price = observation.page_price_value
        purchase = _decimal(raw.get("起购数量"))
        observation.min_purchase_box_count = purchase
        if not brand or not spec or not units or not unit or price is None:
            observation.calculation_status = "missing_pack"
            observation.price_status = "not_evaluated"
            observation.error_code = "missing_verified_spec_or_price"
            observation.error_detail = "暂不可比较：缺少经页面确认的品牌、规格、包装或价格"
        else:
            # 页面以“盒”为展示单位；起购数量是订单门槛，不是销售包装数量。
            observation.selected_spec = spec
            observation.sale_box_count = Decimal("1")
            observation.units_per_box = units
            observation.min_unit = unit
            observation.single_box_price = price.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
            observation.single_unit_price = (price / units).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
            observation.control_price = None
            observation.comparison_price = None
            observation.break_amount = None
            observation.calculation_status = "success"
            observation.price_status = "not_evaluated"
            observation.error_code = "exact_control_rule_missing"
            observation.error_detail = f"暂不可比较：{brand} {spec} 没有规格精确匹配的控价规则"
        report.append({"run_id": observation.run_id, "brand": brand or "", "spec": spec or "", "price": str(price or ""), "status": observation.error_code or ""})
    session.commit()
    return report


if __name__ == "__main__":
    engine, factory = configured_database(Settings.from_env(ROOT))
    with factory() as session:
        for row in normalize(session):
            print(row)
