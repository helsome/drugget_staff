from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    CalculationStatus,
    CandidateType,
    CollectionStatus,
    FixedTier,
    IncidentStatus,
    PriceStatus,
    TaskType,
)


class BrowserSession(BaseModel):
    platform: str
    alias: str
    persistent: bool = True


class CollectionTaskSpec(BaseModel):
    task_id: str
    run_id: str
    platform: str
    task_type: TaskType
    session_alias: str
    target_id: str | None = None
    drug_name: str | None = None
    generic_name: str | None = None
    spec: str | None = None
    shop_name: str | None = None
    product_id: str | None = None
    url: str | None = None
    query: str | None = None
    fixed_tier: FixedTier | None = None
    priority: int = 100
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceBundle(BaseModel):
    final_url: str | None = None
    page_title: str | None = None
    screenshot_bytes_b64: str | None = None
    raw_fields: dict[str, Any] = Field(default_factory=dict)
    collector_version: str | None = None
    login_status: str | None = None
    parser_version: str | None = None
    captured_at: datetime = Field(default_factory=datetime.now)


PriceType = Literal[
    "base_price",
    "promotion_price",
    "member_price",
    "coupon_price",
    "tier_price",
    "range_price",
    "package_total_price",
    "unknown",
]


class ProductEvidence(BaseModel):
    """Product identity as observed on the page; empty means not observed."""

    model_config = ConfigDict(extra="allow")
    product_id: str = ""
    title: str = ""
    manufacturer: str = ""
    provider_id: str = ""


class SKUOptionEvidence(BaseModel):
    """One page-declared SKU option without inventing absent identifiers."""

    model_config = ConfigDict(extra="allow")
    sku_id: str = ""
    raw_spec: str = ""
    normalized_spec: str = ""
    selected: bool = False
    available: bool | None = None


class PriceQuoteEvidence(BaseModel):
    """A quote tied to one SKU and one visible pricing condition."""

    model_config = ConfigDict(extra="allow")
    sku_id: str = ""
    price_type: PriceType = "unknown"
    amount: str = ""
    min_quantity: int | None = None
    membership_required: bool | None = None
    promotion_required: bool | None = None
    # The page-declared package scope for ``amount``.  These are deliberately
    # optional: a collector must not infer them from a title when absent.
    price_box_count: int | None = None
    units_per_box: int | None = None
    raw_text: str = ""
    evidence_pointer: str = ""


class SKUEvidence(BaseModel):
    """Canonical detail evidence stored inside ``PriceObservation.raw_evidence``.

    Extra top-level keys are allowed so older adapter fields remain available
    for audit and replay.
    """

    model_config = ConfigDict(extra="allow")
    product: ProductEvidence = Field(default_factory=ProductEvidence)
    sku_options: list[SKUOptionEvidence] = Field(default_factory=list)
    price_quotes: list[PriceQuoteEvidence] = Field(default_factory=list)
    selected_sku: dict[str, Any] = Field(default_factory=dict)
    page_context: dict[str, Any] = Field(default_factory=dict)
    parser: dict[str, str] = Field(default_factory=dict)


class CollectionResult(BaseModel):
    collection_status: CollectionStatus
    calculation_status: CalculationStatus = CalculationStatus.NOT_APPLICABLE
    price_status: PriceStatus = PriceStatus.NOT_EVALUATED
    page_title: str | None = None
    final_url: str | None = None
    page_shop: str | None = None
    selected_spec: str | None = None
    page_price_raw: str | None = None
    page_price_value: Decimal | None = None
    sale_box_count: Decimal | None = None
    min_purchase_box_count: Decimal | None = None
    units_per_box: Decimal | None = None
    min_unit: str | None = None
    single_box_price: Decimal | None = None
    single_unit_price: Decimal | None = None
    control_price: Decimal | None = None
    comparison_price: Decimal | None = None
    break_amount: Decimal | None = None
    error_code: str | None = None
    error_detail: str | None = None
    evidence: EvidenceBundle = Field(default_factory=EvidenceBundle)


class SearchHit(BaseModel):
    platform: str
    query: str
    rank: int | None = None
    title: str
    url: str | None = None
    product_id: str | None = None
    shop_name: str | None = None
    list_price_raw: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ClassifiedCandidate(SearchHit):
    candidate_type: CandidateType
    target_brand: str
    target_spec: str | None = None
    matched_brand: str | None = None
    matched_store_id: str | None = None
    is_formal_price: bool = False
    reason: str


class IncidentAction(BaseModel):
    action: IncidentStatus
    operator_note: str | None = None


class WorkerResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: CollectionResult


class RouteDecision(BaseModel):
    routing_status: str
    recipient: str | None = None
    channel: str | None = None
    dry_run: bool = True
    reason: str


class IncidentView(BaseModel):
    id: str
    task_id: str
    platform: str
    incident_type: str
    status: str
    current_url: str | None = None
    page_title: str | None = None
    screenshot_path: str | None = None
    detected_at: datetime
    operator_note: str | None = None
