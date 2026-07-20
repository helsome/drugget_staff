from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import BRAND_TO_GENERIC, ControlPriceEntry
from .enums import CalculationStatus, PriceStatus, TaskStatus
from .errors import AmbiguousControlPrice
from .models import (
    CollectionRun,
    CollectionTask,
    MonitorTarget,
    ControlPriceVersion,
    DrugProduct,
    PackageMaster,
    PriceObservation,
    SearchCandidate,
    StoreResponsibility,
)
from .schemas import ClassifiedCandidate, CollectionResult, CollectionTaskSpec, SearchHit
from .pricing import evaluate_price, resolve_control_price
from .search import SearchClassifier, canonical_url


class TaskQueueService:
    def __init__(self, session: Session):
        self.session = session

    def create_run(self) -> CollectionRun:
        run = CollectionRun(status="pending")
        self.session.add(run)
        self.session.flush()
        return run

    def enqueue(self, spec: CollectionTaskSpec) -> CollectionTask:
        task = CollectionTask(
            id=spec.task_id,
            run_id=spec.run_id,
            target_id=spec.target_id,
            platform=spec.platform,
            task_type=spec.task_type.value,
            status=TaskStatus.PENDING.value,
            query=spec.query,
            payload=spec.model_dump(mode="json"),
            session_alias=spec.session_alias,
            priority=spec.priority,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def lease(
        self,
        *,
        platform: str,
        session_alias: str,
        run_id: str | None = None,
        task_types: set[str] | None = None,
    ) -> CollectionTask | None:
        predicates = [
            CollectionTask.platform == platform,
            CollectionTask.session_alias == session_alias,
            CollectionTask.status == TaskStatus.PENDING.value,
        ]
        if run_id:
            predicates.append(CollectionTask.run_id == run_id)
        if task_types:
            predicates.append(CollectionTask.task_type.in_(task_types))
        statement = (
            select(CollectionTask)
            .where(*predicates)
            .order_by(CollectionTask.priority, CollectionTask.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        task = self.session.scalar(statement)
        if task:
            task.status = TaskStatus.LEASED.value
            task.leased_at = datetime.now()
            task.attempts += 1
            self.session.flush()
        return task

    def record_result(self, task: CollectionTask, result: CollectionResult, evidence_path: str | None) -> PriceObservation:
        channel = "detail" if task.task_type == "inspect_candidate" else "search" if task.task_type in {"search", "store_search"} else "fixed"
        observation = PriceObservation(
            run_id=task.run_id,
            task_id=task.id,
            target_id=task.target_id,
            channel=channel,
            captured_at=result.evidence.captured_at,
            final_url=result.final_url,
            page_title=result.page_title,
            page_shop=result.page_shop,
            selected_spec=result.selected_spec,
            page_price_raw=result.page_price_raw,
            page_price_value=result.page_price_value,
            sale_box_count=result.sale_box_count,
            units_per_box=result.units_per_box,
            min_unit=result.min_unit,
            single_box_price=result.single_box_price,
            single_unit_price=result.single_unit_price,
            control_price=result.control_price,
            comparison_price=result.comparison_price,
            break_amount=result.break_amount,
            collection_status=result.collection_status.value,
            calculation_status=result.calculation_status.value,
            price_status=result.price_status.value,
            error_code=result.error_code,
            error_detail=result.error_detail,
            evidence_path=evidence_path,
        )
        task.status = (
            TaskStatus.SUCCEEDED.value
            if result.collection_status.value == "success"
            else TaskStatus.FAILED.value
        )
        task.completed_at = datetime.now()
        self.session.add(observation)
        self.session.flush()
        return observation


class SearchCandidateService:
    """Classify and persist Search hits without treating list prices as evidence."""

    VALID_TYPES = {
        "known_target",
        "new_link_same_store",
        "known_non_fixed_store",
        "new_store",
        "possible_match",
    }

    def __init__(self, session: Session):
        self.session = session

    def _classifier(self) -> SearchClassifier:
        rows = list(
            self.session.execute(
                select(MonitorTarget, StoreResponsibility).outerjoin(
                    StoreResponsibility,
                    MonitorTarget.store_id == StoreResponsibility.id,
                )
            )
        )
        return SearchClassifier(
            fixed_product_ids={target.product_id for target, _ in rows},
            fixed_urls={target.url for target, _ in rows},
            fixed_stores={
                store.shop_name: store.internal_store_id
                for _, store in rows
                if store is not None
            },
            known_stores={
                store.shop_name: store.internal_store_id
                for store in self.session.scalars(select(StoreResponsibility))
            },
        )

    def has_valid_candidate(self, *, run_id: str, drug_id: str, platform: str) -> bool:
        return self.session.scalar(
            select(SearchCandidate.id)
            .where(
                SearchCandidate.run_id == run_id,
                SearchCandidate.drug_id == drug_id,
                SearchCandidate.platform == platform,
                SearchCandidate.candidate_type.in_(self.VALID_TYPES),
            )
            .limit(1)
        ) is not None

    def classify_and_save(
        self,
        *,
        task: CollectionTask,
        spec: CollectionTaskSpec,
        hits: list[SearchHit],
    ) -> list[ClassifiedCandidate]:
        drug_id = str(spec.metadata.get("drug_id") or "")
        target_brand = str(spec.metadata.get("target_brand") or spec.drug_name or "")
        if not drug_id or not target_brand:
            return []
        classifier = self._classifier()
        saved: list[ClassifiedCandidate] = []
        for hit in hits:
            item = classifier.classify(
                hit,
                target_brand=target_brand,
                target_spec=spec.metadata.get("target_spec") or spec.spec,
            )
            url_key = canonical_url(item.url)
            query = select(SearchCandidate).where(
                SearchCandidate.run_id == task.run_id,
                SearchCandidate.drug_id == drug_id,
                SearchCandidate.platform == task.platform,
            )
            if item.product_id:
                query = query.where(SearchCandidate.product_id == item.product_id)
            elif url_key:
                query = query.where(SearchCandidate.url == url_key)
            else:
                query = query.where(SearchCandidate.title == item.title)
            if self.session.scalar(query.limit(1)):
                continue
            self.session.add(
                SearchCandidate(
                    run_id=task.run_id,
                    drug_id=drug_id,
                    platform=task.platform,
                    query=item.query,
                    search_rank=item.rank,
                    product_id=item.product_id,
                    shop_name=item.shop_name,
                    url=url_key,
                    title=item.title,
                    list_price_raw=item.list_price_raw,
                    candidate_type=item.candidate_type.value,
                    sku_verification_status="pending_detail_verification",
                    responsibility_match_status="matched" if item.matched_store_id else "pending_assignment",
                    is_formal_price=False,
                    reason=item.reason,
                    raw=item.raw,
                )
            )
            saved.append(item)
        self.session.flush()
        return saved


def evaluate_fixed_result(session: Session, spec: CollectionTaskSpec, result: CollectionResult) -> CollectionResult:
    """Apply only verified package and exact control-price references to a detail result."""
    if not spec.target_id or result.collection_status.value != "success":
        return result
    target = session.get(MonitorTarget, spec.target_id)
    if target is None:
        result.calculation_status = CalculationStatus.MISSING_PACK
        result.error_code = "target_not_found"
        return result
    package = session.scalar(
        select(PackageMaster).where(
            PackageMaster.drug_id == target.drug_id,
            PackageMaster.spec_normalized == target.spec_normalized,
            PackageMaster.verified.is_(True),
        )
    )
    if package is None or package.units_per_box is None or package.min_unit is None:
        result.calculation_status = CalculationStatus.MISSING_PACK
        result.price_status = PriceStatus.NOT_EVALUATED
        result.error_code = "package_master_unverified"
        return result
    drug = session.get(DrugProduct, target.drug_id)
    rows = list(
        session.scalars(
            select(ControlPriceVersion).where(
                ControlPriceVersion.drug_id == target.drug_id,
                ControlPriceVersion.active.is_(True),
            )
        )
    )
    entries = [
        ControlPriceEntry(
            brand=drug.brand_name if drug else spec.drug_name or "",
            generic_name=drug.generic_name if drug else BRAND_TO_GENERIC.get(spec.drug_name or "", ""),
            spec_key=row.spec_key,
            price=Decimal(row.price_per_min_unit),
            min_unit=row.min_unit,
            source_line=row.source_line,
        )
        for row in rows
    ]
    try:
        control = resolve_control_price(
            entries,
            brand=drug.brand_name if drug else spec.drug_name or "",
            spec=target.spec_normalized,
        )
    except AmbiguousControlPrice as exc:
        result.calculation_status = CalculationStatus.CONTROL_PRICE_AMBIGUOUS
        result.price_status = PriceStatus.NOT_EVALUATED
        result.error_code = exc.code
        result.error_detail = exc.message
        return result
    if control is not None and control.min_unit != package.min_unit:
        result.calculation_status = CalculationStatus.CONTROL_PRICE_AMBIGUOUS
        result.price_status = PriceStatus.NOT_EVALUATED
        result.error_code = "control_package_unit_mismatch"
        return result
    expected_raw = spec.metadata.get("expected_box_count")
    expected_box_count = Decimal(str(expected_raw)) if expected_raw not in (None, "") else None
    return evaluate_price(
        result,
        expected_box_count=expected_box_count,
        units_per_box=Decimal(package.units_per_box),
        min_unit=package.min_unit,
        control_price=control.price if control else None,
    )


def model_to_dict(model: Any) -> dict[str, Any]:
    return {column.name: getattr(model, column.name) for column in model.__table__.columns}
