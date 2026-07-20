from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

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
