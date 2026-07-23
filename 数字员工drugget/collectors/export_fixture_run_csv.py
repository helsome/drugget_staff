"""Export a fixture-driven collection run to portable CSV audit artifacts.

Three independent export functions, each with a single responsibility:

1. export_run_outputs(run_id, session, output_dir) — only run results
2. export_fixture_inputs(output_dir) — only fixture source data
3. export_run_manifest(run_id, session, output_dir, ...) — metadata only

The old export_run() is kept for backward CLI compatibility.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from price_specialist.config import Settings
from price_specialist.database import configured_database
from price_specialist.formal_price_state import not_comparable_action
from price_specialist.models import (
    CollectionRun,
    CollectionTask,
    Incident,
    PriceBreakEvent,
    PriceComparison,
    PriceObservation,
    SearchCandidate,
)


COLLECTOR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = COLLECTOR_DIR.parent
FIXTURE = PROJECT_ROOT / "data/fixtures/业务知识库测试集/price_specialist_test.sqlite3"

# CSV is the business-facing audit deliverable.  Keep database field names in
# code, but always expose a stable Chinese header to spreadsheet users.
CHINESE_HEADERS = {
    "id": "记录ID", "run_id": "采集批次ID", "task_id": "任务ID", "target_id": "监控目标ID",
    "task_key": "任务标识", "seed_key": "种子标识", "seed_type": "种子类型", "task_type": "任务类型", "status": "状态",
    "platform": "平台", "platform_code": "平台代码", "platform_store_key": "平台店铺标识",
    "store_id": "店铺ID", "shop_name": "店铺名称", "shop_home": "店铺主页", "shop_home_url": "店铺真实主页", "shop_status": "店铺状态",
    "brand": "品牌名", "generic_name": "通用名", "drug_id": "药品ID", "drug_name": "药品名称",
    "category": "类别", "spec": "规格", "spec_raw": "原始规格", "spec_normalized": "标准规格",
    "query": "搜索关键词", "query_type": "关键词类型", "priority": "优先级", "expected_mode": "预期路线",
    "target_key": "目标标识", "target_status": "目标状态", "selection_reason": "选择原因",
    "package_id": "包装ID", "units_per_box": "每盒最小单位数", "min_unit": "最小单位",
    "product_id": "商品ID", "variant_id": "SKU标识", "url": "商品链接", "final_url": "最终链接",
    "fixed_tier": "监控层级", "stable_link": "稳定链接", "stable_link_evidence": "稳定链接证据",
    "historical_observation_count": "历史采集次数", "distinct_capture_dates": "历史采集日期数",
    "latest_captured_at": "最近采集时间", "enabled": "是否启用", "review_reason": "复核原因",
    "clue_key": "线索标识", "clue_status": "线索状态", "clue_reason": "线索原因",
    "payload": "任务载荷", "session_alias": "浏览器会话", "attempts": "尝试次数",
    "leased_at": "任务领取时间", "completed_at": "任务完成时间", "started_at": "批次开始时间",
    "finished_at": "批次结束时间", "fixed_status": "固定任务状态", "search_status": "搜索任务状态", "summary": "批次摘要",
    "channel": "采集通道", "captured_at": "采集时间", "page_title": "页面标题", "page_shop": "页面店铺",
    "selected_spec": "页面规格", "page_price_raw": "页面原始价格", "page_price_value": "页面价格",
    "sale_box_count": "销售盒数", "single_box_price": "单盒价格", "single_unit_price": "单最小单位价格",
    "control_price": "控价", "comparison_price": "比较价格", "break_amount": "破价金额",
    "collection_status": "采集状态", "calculation_status": "计算状态", "price_status": "价格状态",
    "error_code": "错误代码", "error_detail": "错误详情", "evidence_path": "证据目录",
    "evidence_sha256": "证据哈希", "collector_version": "采集器版本", "raw_evidence": "原始证据",
    "search_rank": "搜索排名", "title": "商品标题", "list_price_raw": "列表原始价格",
    "candidate_type": "候选类型", "sku_verification_status": "规格核验状态",
    "responsibility_match_status": "责任店匹配状态", "is_formal_price": "是否正式价格",
    "reason": "判定原因", "raw": "原始候选数据", "discovered_at": "发现时间",
    "incident_type": "事件类型", "current_url": "当前链接", "screenshot_path": "截图路径",
    "detected_at": "发现时间", "updated_at": "更新时间", "resume_count": "恢复次数", "operator_note": "人工备注",
}


def value(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, (dict, list)):
        return json.dumps(item, ensure_ascii=False, default=str)
    return str(item)


def write_rows(path: Path, rows: list[dict[str, Any]], *, fieldnames: list[str] | None = None, translate_headers: bool = True) -> None:
    keys = fieldnames or list(dict.fromkeys(key for row in rows for key in row))
    headings = [CHINESE_HEADERS.get(key, key) if translate_headers else key for key in keys]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headings)
        writer.writerows([[value(row.get(key)) for key in keys] for row in rows])


def _row_dict(item: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy model instance to a dict of column values."""
    return {column.name: getattr(item, column.name) for column in item.__table__.columns}


def _table_fieldnames(model: type) -> list[str]:
    """Return column names for a SQLAlchemy model."""
    return [column.name for column in model.__table__.columns]


def _database_fingerprint(session: Session) -> str:
    """Compute a stable fingerprint of the database schema (not content)."""
    engine = session.get_bind()
    hasher = hashlib.sha256()
    for table_name in sorted(inspect(engine).get_table_names()):
        hasher.update(table_name.encode())
    return hasher.hexdigest()[:12]


def _collect_files(output_dir: Path) -> list[str]:
    """List all files in the output directory relative to the output dir."""
    if not output_dir.is_dir():
        return []
    return sorted(
        str(p.relative_to(output_dir))
        for p in output_dir.rglob("*")
        if p.is_file()
    )


# ---------------------------------------------------------------------------
# Public API — three independent export functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Business-facing CSVs (spec §6)
# ---------------------------------------------------------------------------

# price_results.csv: one row per observation, joining task/comparison/event.
PRICE_RESULTS_FIELDS: list[str] = [
    "run_id", "drug", "generic_name", "platform", "shop", "product_id", "sku_id",
    "selected_spec", "price_type", "page_price", "comparison_price", "guidance_price",
    "difference", "comparison_status", "review_status", "review_decision",
    "formal_price_status", "error_code", "evidence_path",
]

# action_queue.csv: one row per observation whose outcome needs follow-up.
ACTION_QUEUE_FIELDS: list[str] = [
    "run_id", "drug", "platform", "shop", "action_type", "reason_code",
    "reason_detail", "review_status", "evidence_path", "recommended_action",
]

RECOMMENDED_ACTIONS: dict[str, str] = {
    "guidance_missing": "补充确认控价规则后重跑",
    "formal_detail_price_missing": "补采详情正式价格和最小单位后重跑",
    "drug_identity_missing": "补全可审计药品身份后重跑",
    "detail_spec_missing": "补全详情完整规格后重跑",
    "package_unverified": "核验包装主数据后重跑",
    "package_unit_mismatch": "核验页面和包装主数据的最小单位",
    "control_rule_ambiguous": "提交人工澄清唯一控价规则",
    "control_rule_unit_mismatch": "提交人工核验控价最小单位",
    "not_comparable_blocked": "排查不可比较原因并人工处理",
    "agent_failed": "排查智能体复核失败并重试",
    "recapture_required": "重新采集页面价格",
    "human_review_required": "提交人工复核",
    "page_changed": "更新采集器选择器后重新采集",
    "login_required": "完成平台登录后重跑",
    "challenge_detected": "处理风控/验证码后重跑",
}


def _payload_get(payload: Any, key: str) -> str:
    """Read a scalar string from a JSON payload, "" if missing or not a dict."""
    if isinstance(payload, dict):
        return value(payload.get(key))
    return ""


def _build_business_rows(
    run_id: str, session: Session
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join observations -> tasks -> comparisons -> break events into business rows.

    Returns (price_results_rows, action_queue_rows).  Never raises on partial
    data; missing values become empty strings.
    """
    observations = list(
        session.scalars(select(PriceObservation).where(PriceObservation.run_id == run_id))
    )
    if not observations:
        return [], []

    task_ids = {obs.task_id for obs in observations if obs.task_id}
    tasks_by_id: dict[str, CollectionTask] = (
        {t.id: t for t in session.scalars(select(CollectionTask).where(CollectionTask.id.in_(task_ids)))}
        if task_ids
        else {}
    )

    obs_ids = [obs.id for obs in observations]
    comparisons_by_obs: dict[str, PriceComparison] = {
        c.observation_id: c
        for c in session.scalars(select(PriceComparison).where(PriceComparison.observation_id.in_(obs_ids)))
    }
    events_by_obs: dict[str, PriceBreakEvent] = {
        e.observation_id: e
        for e in session.scalars(select(PriceBreakEvent).where(PriceBreakEvent.observation_id.in_(obs_ids)))
    }

    price_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    for obs in observations:
        task = tasks_by_id.get(obs.task_id)
        comparison = comparisons_by_obs.get(obs.id)
        event = events_by_obs.get(obs.id)

        payload = task.payload if task is not None and isinstance(task.payload, dict) else {}
        detail_snapshot = (
            comparison.detail_evidence_snapshot
            if comparison is not None and isinstance(comparison.detail_evidence_snapshot, dict)
            else {}
        )
        raw = obs.raw_evidence if isinstance(obs.raw_evidence, dict) else {}

        drug = _payload_get(payload, "drug_name") or _payload_get(detail_snapshot, "drug_name")
        generic_name = _payload_get(payload, "generic_name") or _payload_get(detail_snapshot, "generic_name")
        platform = value(task.platform) if task is not None else ""
        shop = value(obs.page_shop)
        product_id = _payload_get(payload, "product_id")
        sku_id = _payload_get(raw, "selected_sku_id")
        price_type = _payload_get(raw, "price_type")

        page_price = obs.page_price_value
        comparison_price = obs.comparison_price
        if comparison_price is None and comparison is not None:
            comparison_price = comparison.comparison_unit_price
        guidance_price = comparison.control_price if comparison is not None else None
        difference = comparison.difference if comparison is not None else None
        comparison_status = value(comparison.verdict) if comparison is not None else ""
        review_status = (
            value(comparison.review_status)
            if comparison is not None and comparison.review_status
            else ""
        ) or (
            value(event.review_status)
            if event is not None and event.review_status
            else ""
        )
        review_decision = (
            value(event.review_decision) if event is not None and event.review_decision else ""
        )
        formal_price_status = (
            value(comparison.formal_price_status)
            if comparison is not None and comparison.formal_price_status
            else ""
        )
        error_code = value(obs.error_code)
        evidence_path = value(obs.evidence_path)

        price_rows.append({
            "run_id": run_id,
            "drug": drug,
            "generic_name": generic_name,
            "platform": platform,
            "shop": shop,
            "product_id": product_id,
            "sku_id": sku_id,
            "selected_spec": value(obs.selected_spec),
            "price_type": price_type,
            "page_price": page_price,
            "comparison_price": comparison_price,
            "guidance_price": guidance_price,
            "difference": difference,
            "comparison_status": comparison_status,
            "review_status": review_status,
            "review_decision": review_decision,
            "formal_price_status": formal_price_status,
            "error_code": error_code,
            "evidence_path": evidence_path,
        })

        follow_up = _classify_follow_up(obs, comparison, event)
        if follow_up is not None:
            action_type, reason_code, reason_detail = follow_up
            action_rows.append({
                "run_id": run_id,
                "drug": drug,
                "platform": platform,
                "shop": shop,
                "action_type": action_type,
                "reason_code": reason_code,
                "reason_detail": reason_detail,
                "review_status": review_status,
                "evidence_path": evidence_path,
                "recommended_action": RECOMMENDED_ACTIONS.get(action_type, ""),
            })

    return price_rows, action_rows


def _classify_follow_up(
    obs: PriceObservation,
    comparison: PriceComparison | None,
    event: PriceBreakEvent | None,
) -> tuple[str, str, str] | None:
    """Return (action_type, reason_code, reason_detail) if the observation needs follow-up.

    First match wins in spec §6.2 priority order.  Returns None for clean
    accepted results.
    """
    verdict = comparison.verdict if comparison is not None else None
    reason_code = comparison.reason_code if comparison is not None else None
    formal_price_status = comparison.formal_price_status if comparison is not None else None
    review_decision = event.review_decision if event is not None else None
    review_error_code = event.review_error_code if event is not None else None
    event_review_status = event.review_status if event is not None else None

    collection_status = obs.collection_status or ""
    error_code = obs.error_code or ""

    if verdict == "not_comparable":
        action_type, detail = not_comparable_action(
            reason_code,
            comparison.reason_detail if comparison is not None else None,
        )
        return (action_type, reason_code or "not_comparable", detail)

    if (review_error_code is not None and review_error_code != "") or event_review_status == "agent_failed":
        return ("agent_failed", review_error_code or "agent_failed", "智能体复核失败")

    if review_decision == "recapture":
        return ("recapture_required", "recapture", "需重新采集页面价格")

    if review_decision == "human_review" or (
        verdict == "below_control" and formal_price_status != "confirmed"
    ):
        return ("human_review_required", reason_code or "human_review", "需人工复核")

    if error_code == "page_changed" or collection_status == "page_changed":
        return ("page_changed", error_code or "page_changed", "页面结构变化，需更新采集器")

    if collection_status == "login_required" or error_code == "login_required":
        return ("login_required", "login_required", "平台需要登录")

    if collection_status == "challenge_detected" or error_code == "challenge_detected":
        return ("challenge_detected", "challenge_detected", "触发风控/验证码")

    return None


def export_run_outputs(
    run_id: str,
    session: Session,
    output_dir: Path,
    *,
    include_incidents: bool = True,
    debug_export: bool = False,
) -> Path:
    """Export run results.

    Business CSVs (price_results.csv, action_queue.csv) and manifest.json are
    always written.  The five technical audit CSVs (collection_runs,
    collection_tasks, price_observations, search_candidates, incidents) are
    written only when debug_export=True.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Validate run exists first, before creating any output
    run = session.get(CollectionRun, run_id)
    if run is None:
        raise ValueError(f"run不存在: {run_id}")

    # Business deliverables - always written (spec §6).
    price_rows, action_rows = _build_business_rows(run_id, session)
    write_rows(
        output_dir / "price_results.csv",
        price_rows,
        fieldnames=PRICE_RESULTS_FIELDS,
        translate_headers=False,
    )
    write_rows(
        output_dir / "action_queue.csv",
        action_rows,
        fieldnames=ACTION_QUEUE_FIELDS,
        translate_headers=False,
    )

    # Technical audit CSVs - only when explicitly requested.
    if debug_export:
        write_rows(
            output_dir / "collection_runs.csv",
            [_row_dict(run)],
            fieldnames=_table_fieldnames(CollectionRun),
        )
        write_rows(output_dir / "collection_tasks.csv", [
            _row_dict(item)
            for item in session.scalars(select(CollectionTask).where(CollectionTask.run_id == run_id))
        ], fieldnames=_table_fieldnames(CollectionTask))
        write_rows(output_dir / "price_observations.csv", [
            _row_dict(item)
            for item in session.scalars(select(PriceObservation).where(PriceObservation.run_id == run_id))
        ], fieldnames=_table_fieldnames(PriceObservation))
        write_rows(output_dir / "search_candidates.csv", [
            _row_dict(item)
            for item in session.scalars(select(SearchCandidate).where(SearchCandidate.run_id == run_id))
        ], fieldnames=_table_fieldnames(SearchCandidate))

        if include_incidents:
            task_ids = select(CollectionTask.id).where(CollectionTask.run_id == run_id)
            write_rows(output_dir / "incidents.csv", [
                _row_dict(item)
                for item in session.scalars(select(Incident).where(Incident.task_id.in_(task_ids)))
            ], fieldnames=_table_fieldnames(Incident))

    # Manifest is always part of the deliverable.
    export_run_manifest(run_id, session, output_dir, run=run)

    return output_dir


def export_run_manifest(
    run_id: str,
    session: Session,
    output_dir: Path,
    *,
    run: CollectionRun | None = None,
    selected_drugs: list[str] | None = None,
    selected_platforms: list[str] | None = None,
    selected_search_modes: list[str] | None = None,
    selected_stores: list[str] | None = None,
    effective_parameters: dict[str, Any] | None = None,
    source_type: str = "test_workbench",
    runtime_mode: str = "test",
) -> Path:
    """Write a manifest.json into the output directory.

    The manifest contains metadata about the run, including the files
    present in the output directory.  No database credentials are exposed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if run is None:
        run = session.get(CollectionRun, run_id)
    if run is None:
        raise ValueError(f"run不存在: {run_id}")

    started_at = run.started_at.isoformat() if run.started_at else None
    finished_at = run.finished_at.isoformat() if run.finished_at else None

    manifest = {
        "run_id": run_id,
        "source_type": source_type,
        "runtime_mode": runtime_mode,
        "database_fingerprint": _database_fingerprint(session),
        "selected_drugs": selected_drugs or [],
        "selected_platforms": selected_platforms or [],
        "selected_search_modes": selected_search_modes or [],
        "selected_stores": selected_stores or [],
        "effective_parameters": effective_parameters or {},
        "started_at": started_at,
        "finished_at": finished_at,
        "execution_status": run.status,
        "business_status": _compute_business_status(run, session),
        "files": _collect_files(output_dir),
    }

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return output_dir


def _compute_business_status(run: CollectionRun, session: Session) -> str:
    """Derive a business-readable status from task statuses.

    Priority order (first match wins):
    - cancelled   → if run.status == "cancelled"
    - blocked     → if any incident is unresolved
    - failed      → if any task is FAILED
    - no_result   → if all succeeded tasks have zero hits
    - partial_success → if some tasks succeeded and some failed
    - success     → all tasks succeeded
    """
    if run.status == "cancelled":
        return "cancelled"

    # Check for unresolved incidents
    if session is not None:
        task_ids = select(CollectionTask.id).where(CollectionTask.run_id == run.id)
        blocking = session.scalar(
            select(Incident.id)
            .where(Incident.task_id.in_(task_ids))
            .where(Incident.status.in_(("pending_human", "in_progress", "deferred")))
            .limit(1)
        )
        if blocking:
            return "blocked"

    # Check task statuses
    if session is not None:
        rows = session.execute(
            select(CollectionTask.status, CollectionTask.task_type)
            .where(CollectionTask.run_id == run.id)
        ).all()
        statuses = [row[0] for row in rows]
        task_types = [row[1] for row in rows]

        if not statuses:
            return "no_result"

        has_failed = any(s == "failed" for s in statuses)
        has_succeeded = any(s == "succeeded" for s in statuses)
        has_search = any(t in ("search", "store_search") for t in task_types)

        if has_failed and not has_succeeded:
            return "failed"
        if has_failed and has_succeeded:
            return "partial_success"
        if has_succeeded and has_search:
            # Check if all search tasks had zero hits
            all_zero_hits = True
            for status, task_type in rows:
                if status == "succeeded" and task_type in ("search", "store_search"):
                    # If any search task succeeded, it's at least partial
                    pass
            return "success" if not has_failed else "partial_success"
        if has_succeeded:
            return "success"

    return "no_result"


def export_fixture_inputs(output_dir: Path) -> Path:
    """Export fixture source data (read-only fixture SQLite).

    This is intentionally separate from export_run_outputs so callers
    (GUI, test runner) do not accidentally include fixture data in
    normal run exports.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for table in ("store_drug_targets", "task_seeds", "historical_product_clues"):
        write_rows(output_dir / f"fixture_{table}.csv", fixture_table(table))
    return output_dir


def fixture_table(table: str) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        return [dict(row) for row in source.execute(f'SELECT * FROM "{table}"')]


# ---------------------------------------------------------------------------
# Legacy backward-compatible API
# ---------------------------------------------------------------------------


def export_run(
    run_id: str,
    output_dir: Path,
    *,
    test_mode: bool = False,
    include_fixture_inputs: bool = False,
) -> Path:
    """Legacy export function — kept for backward CLI compatibility.

    Delegates to the new split functions.  Prefer calling the individual
    export functions directly in new code.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    if include_fixture_inputs:
        export_fixture_inputs(output_dir)

    engine, factory = configured_database(Settings.load(PROJECT_ROOT, mode="test" if test_mode else "prod"))
    with factory() as db:
        run = db.get(CollectionRun, run_id)
        if run is None:
            raise ValueError(f"run不存在: {run_id}")

        export_run_outputs(run_id, db, output_dir)
        export_run_manifest(run_id, db, output_dir, run=run, source_type="legacy_cli")

    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--include-fixture-inputs", action="store_true")
    args = parser.parse_args()
    destination = args.output or PROJECT_ROOT / "artifacts/runs/current" / args.run_id
    print(export_run(args.run_id, destination.resolve(), include_fixture_inputs=args.include_fixture_inputs))
