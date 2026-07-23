from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .catalog import BRAND_TO_GENERIC, ControlPriceEntry
from .enums import CalculationStatus, PriceStatus, StoreSelectionMode, TaskStatus
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

    def cancel_pending_tasks(self, run_id: str) -> int:
        """Mark all pending/leased tasks in a run as cancelled.

        Returns the number of tasks that were cancelled.
        """
        now = datetime.now()
        count = self.session.execute(
            update(CollectionTask)
            .where(
                CollectionTask.run_id == run_id,
                CollectionTask.status.in_([TaskStatus.PENDING.value, TaskStatus.LEASED.value]),
            )
            .values(status=TaskStatus.CANCELLED.value, completed_at=now)
        ).rowcount
        self.session.flush()
        return count

    def mark_run_cancelled(self, run_id: str) -> None:
        """Mark a CollectionRun as cancelled with finished_at set."""
        run = self.session.get(CollectionRun, run_id)
        if run:
            run.status = "cancelled"
            run.finished_at = datetime.now()
            self.session.flush()

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
            min_purchase_box_count=result.min_purchase_box_count,
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


class StoreDiscoveryService:
    """Persist store identities found by GLOBAL_SEARCH detail verification.

    Discovery is deliberately separate from responsibility and monitoring
    activation. A discovered store must be reviewed/verified before it can
    receive STORE_SEARCH tasks.
    """

    ACTIVE_STATUSES = {"verified", "active"}

    def __init__(self, session: Session):
        self.session = session

    def record_from_detail(self, *, task: CollectionTask, spec: CollectionTaskSpec,
                           result: CollectionResult) -> StoreResponsibility | None:
        if task.task_type != "inspect_candidate" or result.collection_status.value != "success":
            return None
        raw = result.evidence.raw_fields or {}
        provider_id = str(spec.metadata.get("provider_id") or raw.get("provider_id") or "").strip()
        home_url = str(spec.metadata.get("shop_home_url") or raw.get("shop_home_url") or "").strip() or None
        shop_name = str(result.page_shop or raw.get("provider_name") or spec.shop_name or "").strip()
        if not shop_name or (not provider_id and not home_url):
            return None
        query = select(StoreResponsibility).where(StoreResponsibility.platform == task.platform)
        if provider_id:
            query = query.where(StoreResponsibility.platform_store_key == provider_id)
        else:
            query = query.where(StoreResponsibility.shop_home_url == home_url)
        store = self.session.scalar(query.limit(1))
        now = datetime.now()
        evidence = {
            "source": "global_search_detail",
            "run_id": task.run_id,
            "task_id": task.id,
            "drug": spec.drug_name,
            "product_id": spec.metadata.get("candidate_product_id"),
            "url": result.final_url,
            "page_title": result.page_title,
            "provider_id": provider_id or None,
            "provider_name": shop_name,
            "captured_at": now.isoformat(),
        }
        if store is None:
            store = StoreResponsibility(
                internal_store_id=f"discovered-{task.platform}-{provider_id or abs(hash(home_url))}",
                platform=task.platform, platform_store_key=provider_id or None,
                shop_home_url=home_url, shop_name=shop_name, shop_status="待核验",
                fixed_tier="observation_only", identity_status="discovered",
                first_discovered_at=now, last_seen_at=now, discovery_count=1,
                identity_evidence=evidence,
            )
            self.session.add(store)
        else:
            if store.identity_status not in self.ACTIVE_STATUSES:
                store.identity_status = "discovered"
            store.platform_store_key = store.platform_store_key or provider_id or None
            store.shop_home_url = store.shop_home_url or home_url
            store.last_seen_at = now
            store.discovery_count = (store.discovery_count or 0) + 1
            store.identity_evidence = {**(store.identity_evidence or {}), "latest": evidence}
        self.session.flush()
        return store


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
        candidate_limit = int(spec.metadata.get("candidate_limit", 0) or 0)
        classifier = self._classifier()
        saved: list[ClassifiedCandidate] = []
        for hit in hits:
            if candidate_limit > 0 and len(saved) >= candidate_limit:
                break
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
                ControlPriceVersion.effective_from <= date.today(),
                (ControlPriceVersion.effective_to.is_(None) | (ControlPriceVersion.effective_to >= date.today())),
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
            source_file=row.source,
            source_line_number=row.source_line_number,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            active=row.active,
            business_confirmed=row.business_confirmed,
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
            approval_reference=row.approval_reference,
            authority_basis=row.authority_basis,
            source_sha256=row.source_sha256,
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


@dataclass
class DrugSelection:
    """Strongly typed drug selection for test run configuration.

    Use ``from_generic_name()`` or ``from_brand_name()`` for construction.
    Once resolved, ``drug_id`` is populated from the database.
    """

    drug_id: str | None = None
    generic_name: str = ""
    brand_name: str = ""

    @classmethod
    def from_generic_name(cls, generic_name: str, brand_name: str = "") -> DrugSelection:
        return cls(generic_name=generic_name, brand_name=brand_name)

    @classmethod
    def from_brand_name(cls, brand_name: str, generic_name: str = "") -> DrugSelection:
        return cls(generic_name=generic_name, brand_name=brand_name)

    def resolve(self, session: Session) -> DrugProduct | None:
        """Resolve drug_id from the database, creating if not found."""
        if self.drug_id:
            drug = session.get(DrugProduct, self.drug_id)
            if drug:
                return drug
        if self.brand_name:
            drug = session.scalar(
                select(DrugProduct).where(DrugProduct.brand_name == self.brand_name)
            )
            if drug:
                self.drug_id = drug.id
                return drug
        if self.generic_name:
            from .catalog import DRUG_MAP
            brand = DRUG_MAP.get(self.generic_name, "")
            if brand:
                drug = session.scalar(
                    select(DrugProduct).where(DrugProduct.brand_name == brand)
                )
                if drug is None:
                    drug = DrugProduct(brand_name=brand, generic_name=self.generic_name)
                    session.add(drug)
                    session.flush()
                self.drug_id = drug.id
                self.brand_name = brand
                return drug
        return None


@dataclass
class PlannedStoreResult:
    """One store's eligibility result for a specific drug during task planning.

    Fields
    ------
    store:
        The store responsibility record, or None if no store matched.
    drug:
        The drug product being planned for.
    eligible:
        True if this store should receive a STORE_SEARCH task.
    reason:
        Machine-readable reason string (e.g. ``"eligible"``,
        ``"missing_shop_home_url"``, ``"missing_provider_id"``,
        ``"no_responsibility"``, ``"not_executable"``, ``"not_selected"``).
    need_identity_resolution:
        True if the store needs identity resolution before it can be used
        (e.g. yaoshibang with no trusted provider_id).
    """

    store: StoreResponsibility | None
    drug: DrugProduct
    eligible: bool
    reason: str
    need_identity_resolution: bool = False


class StoreTaskPlanner:
    """Plan which stores are eligible for STORE_SEARCH tasks.

    Controls the boundary of store search task generation, preventing the
    unbounded Cartesian product of ``all drugs x all stores``.

    Usage::

        planner = StoreTaskPlanner(session)
        results = planner.eligible_stores(
            platform="taobao",
            drug=drug,
            selection=StoreSelectionMode.RESPONSIBILITY_ONLY,
        )
        eligible = [r for r in results if r.eligible]
        skipped = [r for r in results if not r.eligible]
    """

    # Identity statuses that qualify a store as executable.
    EXECUTABLE_IDENTITY_STATUSES = {"verified", "active", "legacy"}

    # Known fake provider_ids that were historically hand-filled and never
    # appeared in any real search or observation.  Clearing them forces the
    # collector to resolve the real provider_id at runtime.
    FAKE_PROVIDER_IDS = {"W00010", "W00019", "W06410"}

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def sanitize_fake_provider_ids(session: Session) -> int:
        """Clear known fake provider_ids from StoreResponsibility.

        These were historically hand-filled and never appeared in any real
        search or observation.  Clearing them forces the collector to resolve
        the real provider_id at runtime.

        Returns the number of rows updated.

        Previously this logic lived only in the fixture runner entry point
        (``run_fixture_live_smoke.py``).  It is now a public service so that
        every task generation path benefits from the same safeguard.
        """
        result = session.execute(
            update(StoreResponsibility)
            .where(StoreResponsibility.platform == "yaoshibang")
            .where(StoreResponsibility.internal_store_id.in_(StoreTaskPlanner.FAKE_PROVIDER_IDS))
            .values(platform_store_key=None)
        )
        session.flush()
        return result.rowcount

    def eligible_stores(
        self,
        *,
        platform: str,
        drug: DrugProduct,
        selection: StoreSelectionMode,
        manual_store_ids: list[str] | None = None,
    ) -> list[PlannedStoreResult]:
        """Return planned store results for *drug* on *platform*.

        Parameters
        ----------
        platform:
            Platform code (e.g. ``"taobao"``, ``"yaoshibang"``).
        drug:
            The drug product to plan stores for.
        selection:
            Store selection mode (see ``StoreSelectionMode``).
        manual_store_ids:
            Required when *selection* is ``MANUAL``.  List of
            ``internal_store_id`` values to include.

        Returns
        -------
        list[PlannedStoreResult]
            One entry per store on the platform.  Callers should filter by
            ``.eligible`` for task creation and inspect ``.reason`` for
            skipped stores.
        """
        # Get all stores for this platform
        all_stores: list[StoreResponsibility] = list(
            self.session.scalars(
                select(StoreResponsibility).where(
                    StoreResponsibility.platform == platform,
                )
            )
        )

        if not all_stores:
            return []

        results: list[PlannedStoreResult] = []

        # Pre-compute drug-store relationships via MonitorTarget
        related_store_ids: set[str] = set(
            str(item)
            for item in self.session.scalars(
                select(MonitorTarget.store_id).where(
                    MonitorTarget.drug_id == drug.id,
                    MonitorTarget.store_id.isnot(None),
                )
            )
        )

        # Also check StoreResponsibility.involved_products for text match
        for store in all_stores:
            if store.id not in related_store_ids and store.involved_products:
                if drug.brand_name and drug.brand_name in (store.involved_products or ""):
                    related_store_ids.add(store.id)
                elif drug.generic_name and drug.generic_name in (store.involved_products or ""):
                    related_store_ids.add(store.id)

        for store in all_stores:
            # Check responsibility relationship
            has_responsibility = store.id in related_store_ids

            # Check executable identity
            has_executable_identity = (
                store.identity_status in self.EXECUTABLE_IDENTITY_STATUSES
            )

            # Platform-specific checks
            if platform == "taobao":
                has_shop_home = bool(store.shop_home_url)
                if not has_shop_home:
                    results.append(PlannedStoreResult(
                        store=store, drug=drug,
                        eligible=False, reason="missing_shop_home_url",
                    ))
                    continue

            if platform == "yaoshibang":
                has_provider_id = bool(store.platform_store_key) and store.platform_store_key not in self.FAKE_PROVIDER_IDS
                if not has_provider_id:
                    results.append(PlannedStoreResult(
                        store=store, drug=drug,
                        eligible=False, reason="missing_provider_id",
                        need_identity_resolution=True,
                    ))
                    continue

            # Apply selection mode
            if selection == StoreSelectionMode.RESPONSIBILITY_ONLY:
                if not has_responsibility:
                    results.append(PlannedStoreResult(
                        store=store, drug=drug,
                        eligible=False, reason="no_responsibility",
                    ))
                    continue
                if not has_executable_identity:
                    results.append(PlannedStoreResult(
                        store=store, drug=drug,
                        eligible=False, reason="not_executable",
                    ))
                    continue

            elif selection == StoreSelectionMode.EXECUTABLE_ONLY:
                if not has_executable_identity:
                    results.append(PlannedStoreResult(
                        store=store, drug=drug,
                        eligible=False, reason="not_executable",
                    ))
                    continue

            elif selection == StoreSelectionMode.MANUAL:
                if not manual_store_ids or store.internal_store_id not in manual_store_ids:
                    results.append(PlannedStoreResult(
                        store=store, drug=drug,
                        eligible=False, reason="not_selected",
                    ))
                    continue

            # ALL_DANGER: no additional filtering

            results.append(PlannedStoreResult(
                store=store, drug=drug,
                eligible=True, reason="eligible",
            ))

        return results
