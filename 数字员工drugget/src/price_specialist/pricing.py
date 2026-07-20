from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from .catalog import ControlPriceEntry, normalize_spec
from .enums import CalculationStatus, CollectionStatus, PriceStatus
from .errors import AmbiguousControlPrice
from .schemas import CollectionResult


FOUR_PLACES = Decimal("0.0001")


def parse_price(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    values = re.findall(r"\d+(?:\.\d+)?", str(raw).replace(",", ""))
    if len(values) != 1:
        return None
    try:
        return Decimal(values[0])
    except InvalidOperation:
        return None


def resolve_control_price(
    entries: Iterable[ControlPriceEntry],
    *,
    brand: str,
    spec: str | None,
) -> ControlPriceEntry | None:
    """Resolve a control price without converting units or guessing a specification.

    A control price is eligible only when its full specification exactly matches
    the verified page specification. General (spec-less) rules are retained as
    reference data but must not produce a break-price conclusion.
    """
    brand_entries = [entry for entry in entries if entry.brand == brand]
    if not brand_entries:
        return None
    specific = [entry for entry in brand_entries if entry.spec_key]
    normalized = (normalize_spec(spec) or "").replace(" ", "").lower()
    matches = [
        entry
        for entry in specific
        if (normalize_spec(entry.spec_key) or "").replace(" ", "").lower() == normalized
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousControlPrice(
            f"{brand} {spec or '未提供规格'}命中多个控价",
            details={"source_lines": [entry.source_line for entry in matches]},
        )
    if specific:
        return None
    return None


def evaluate_price(
    result: CollectionResult,
    *,
    expected_box_count: Decimal | None,
    units_per_box: Decimal | None,
    min_unit: str | None,
    control_price: Decimal | None,
) -> CollectionResult:
    if result.collection_status != CollectionStatus.SUCCESS:
        result.calculation_status = CalculationStatus.NOT_APPLICABLE
        result.price_status = PriceStatus.NOT_EVALUATED
        return result

    page_price = result.page_price_value or parse_price(result.page_price_raw)
    if page_price is None:
        result.collection_status = CollectionStatus.PRICE_AMBIGUOUS
        result.calculation_status = CalculationStatus.NOT_APPLICABLE
        result.error_code = "price_missing_or_ambiguous"
        return result

    current_boxes = result.sale_box_count
    if current_boxes is None or expected_box_count is None or units_per_box is None:
        result.calculation_status = CalculationStatus.MISSING_PACK
        result.price_status = PriceStatus.NOT_EVALUATED
        result.error_code = "missing_cross_validated_package"
        return result
    if current_boxes <= 0 or expected_box_count <= 0 or units_per_box <= 0:
        result.calculation_status = CalculationStatus.MISSING_PACK
        result.price_status = PriceStatus.NOT_EVALUATED
        result.error_code = "invalid_package_basis"
        return result
    if current_boxes != expected_box_count:
        result.calculation_status = CalculationStatus.PACK_MISMATCH
        result.price_status = PriceStatus.NOT_EVALUATED
        result.error_code = "pack_mismatch"
        result.error_detail = f"页面盒数{current_boxes}与包装基线{expected_box_count}不一致"
        return result

    single_box = (page_price / current_boxes).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    single_unit = (single_box / units_per_box).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    result.page_price_value = page_price
    result.units_per_box = units_per_box
    result.min_unit = min_unit
    result.single_box_price = single_box
    result.single_unit_price = single_unit
    result.control_price = control_price
    result.calculation_status = CalculationStatus.SUCCESS
    if control_price is None:
        result.price_status = PriceStatus.NOT_EVALUATED
        return result
    result.comparison_price = single_unit
    if single_unit < control_price:
        result.price_status = PriceStatus.BELOW_CONTROL
        result.break_amount = (control_price - single_unit).quantize(FOUR_PLACES)
    elif single_unit == control_price:
        result.price_status = PriceStatus.AT_CONTROL
        result.break_amount = Decimal("0")
    else:
        result.price_status = PriceStatus.ABOVE_CONTROL
        result.break_amount = Decimal("0")
    return result
