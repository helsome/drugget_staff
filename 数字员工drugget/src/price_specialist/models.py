from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class DrugProduct(Base):
    __tablename__ = "drug_products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    brand_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    generic_name: Mapped[str] = mapped_column(String(200), nullable=False)
    history_covered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    coverage_status: Mapped[str] = mapped_column(String(40), nullable=False, default="search_cold_start")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


class PackageMaster(Base):
    __tablename__ = "package_master"
    __table_args__ = (UniqueConstraint("drug_id", "spec_normalized", name="uq_package_drug_spec"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    drug_id: Mapped[str] = mapped_column(ForeignKey("drug_products.id"), nullable=False)
    spec_raw: Mapped[str] = mapped_column(String(200), nullable=False)
    spec_normalized: Mapped[str] = mapped_column(String(200), nullable=False)
    units_per_box: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    min_unit: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ControlPriceVersion(Base):
    __tablename__ = "control_price_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    drug_id: Mapped[str] = mapped_column(ForeignKey("drug_products.id"), nullable=False)
    spec_key: Mapped[str | None] = mapped_column(String(100))
    price_per_min_unit: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    min_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    source_line: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StoreResponsibility(Base):
    __tablename__ = "store_responsibilities"
    __table_args__ = (UniqueConstraint("platform", "shop_name", name="uq_store_platform_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    internal_store_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    platform_store_key: Mapped[str | None] = mapped_column(String(300))
    shop_home_url: Mapped[str | None] = mapped_column(Text)
    shop_name: Mapped[str] = mapped_column(String(300), nullable=False)
    shop_status: Mapped[str] = mapped_column(String(40), nullable=False)
    responsible_unit: Mapped[str | None] = mapped_column(String(200))
    responsible_person: Mapped[str | None] = mapped_column(String(100))
    contact: Mapped[str | None] = mapped_column(String(200))
    involved_products: Mapped[str | None] = mapped_column(Text)
    fixed_tier: Mapped[str] = mapped_column(String(40), nullable=False)


class MonitorTarget(Base):
    __tablename__ = "monitor_targets"
    __table_args__ = (UniqueConstraint("platform", "product_id", "drug_id", name="uq_target_platform_product_drug"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    drug_id: Mapped[str] = mapped_column(ForeignKey("drug_products.id"), nullable=False)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("store_responsibilities.id"))
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    product_id: Mapped[str] = mapped_column(String(100), nullable=False)
    variant_id: Mapped[str | None] = mapped_column(String(100))
    spec_raw: Mapped[str] = mapped_column(String(200), nullable=False)
    spec_normalized: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    fixed_tier: Mapped[str] = mapped_column(String(40), nullable=False)
    stable_link: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stable_link_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    fixed_status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    search_status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CollectionTask(Base):
    __tablename__ = "collection_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    target_id: Mapped[str | None] = mapped_column(ForeignKey("monitor_targets.id"))
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    task_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    query: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    session_alias: Mapped[str] = mapped_column(String(100), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PriceObservation(Base):
    __tablename__ = "price_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("collection_tasks.id"), nullable=False)
    target_id: Mapped[str | None] = mapped_column(ForeignKey("monitor_targets.id"))
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    final_url: Mapped[str | None] = mapped_column(Text)
    page_title: Mapped[str | None] = mapped_column(Text)
    page_shop: Mapped[str | None] = mapped_column(String(300))
    selected_spec: Mapped[str | None] = mapped_column(String(200))
    page_price_raw: Mapped[str | None] = mapped_column(String(100))
    page_price_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    sale_box_count: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    units_per_box: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    min_unit: Mapped[str | None] = mapped_column(String(20))
    single_box_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    single_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    control_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    comparison_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    break_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    collection_status: Mapped[str] = mapped_column(String(40), nullable=False)
    calculation_status: Mapped[str] = mapped_column(String(40), nullable=False)
    price_status: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    evidence_path: Mapped[str | None] = mapped_column(Text)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    collector_version: Mapped[str | None] = mapped_column(String(80))
    raw_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SearchCandidate(Base):
    __tablename__ = "search_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "drug_id", "platform", "product_id", name="uq_candidate_run_drug_platform_product"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    drug_id: Mapped[str] = mapped_column(ForeignKey("drug_products.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    search_rank: Mapped[int | None] = mapped_column(Integer)
    product_id: Mapped[str | None] = mapped_column(String(100))
    shop_name: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    list_price_raw: Mapped[str | None] = mapped_column(String(100))
    candidate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    sku_verification_status: Mapped[str | None] = mapped_column(String(40))
    responsibility_match_status: Mapped[str | None] = mapped_column(String(40))
    is_formal_price: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("collection_tasks.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    incident_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending_human")
    session_alias: Mapped[str] = mapped_column(String(100), nullable=False)
    current_url: Mapped[str | None] = mapped_column(Text)
    page_title: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    resume_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operator_note: Mapped[str | None] = mapped_column(Text)


class PriceBreakEvent(Base):
    __tablename__ = "price_break_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    observation_id: Mapped[str] = mapped_column(ForeignKey("price_observations.id"), nullable=False)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("store_responsibilities.id"))
    routing_status: Mapped[str] = mapped_column(String(40), nullable=False)
    event_status: Mapped[str] = mapped_column(String(40), nullable=False, default="dry_run")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("price_break_events.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="dry_run")
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


class SourceDataset(Base):
    __tablename__ = "source_datasets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recognized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unrecognized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("source_datasets.id"), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    row_number: Mapped[int | None] = mapped_column(Integer)
    business_key: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
