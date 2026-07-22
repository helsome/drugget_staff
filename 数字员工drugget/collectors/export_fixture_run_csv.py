"""Export a fixture-driven collection run to portable CSV audit artifacts."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import select

from price_specialist.config import Settings
from price_specialist.database import configured_database
from price_specialist.models import CollectionRun, CollectionTask, Incident, PriceObservation, SearchCandidate


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


def write_rows(path: Path, rows: list[dict[str, Any]], *, fieldnames: list[str] | None = None) -> None:
    keys = fieldnames or list(dict.fromkeys(key for row in rows for key in row))
    headings = [CHINESE_HEADERS.get(key, key) for key in keys]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headings)
        writer.writerows([[value(row.get(key)) for key in keys] for row in rows])


def fixture_table(table: str) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        return [dict(row) for row in source.execute(f'SELECT * FROM "{table}"')]


def export_run(
    run_id: str,
    output_dir: Path,
    *,
    test_mode: bool = False,
    include_fixture_inputs: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if include_fixture_inputs:
        for table in ("store_drug_targets", "task_seeds", "historical_product_clues"):
            write_rows(output_dir / f"fixture_{table}.csv", fixture_table(table))

    engine, factory = configured_database(Settings.from_env(PROJECT_ROOT, test_mode=test_mode))
    with factory() as db:
        run = db.get(CollectionRun, run_id)
        if run is None:
            raise ValueError(f"run不存在: {run_id}")
        write_rows(
            output_dir / "collection_runs.csv",
            [{column.name: getattr(run, column.name) for column in run.__table__.columns}],
            fieldnames=[column.name for column in CollectionRun.__table__.columns],
        )
        write_rows(output_dir / "collection_tasks.csv", [
            {column.name: getattr(item, column.name) for column in item.__table__.columns}
            for item in db.scalars(select(CollectionTask).where(CollectionTask.run_id == run_id))
        ], fieldnames=[column.name for column in CollectionTask.__table__.columns])
        write_rows(output_dir / "price_observations.csv", [
            {column.name: getattr(item, column.name) for column in item.__table__.columns}
            for item in db.scalars(select(PriceObservation).where(PriceObservation.run_id == run_id))
        ], fieldnames=[column.name for column in PriceObservation.__table__.columns])
        write_rows(output_dir / "search_candidates.csv", [
            {column.name: getattr(item, column.name) for column in item.__table__.columns}
            for item in db.scalars(select(SearchCandidate).where(SearchCandidate.run_id == run_id))
        ], fieldnames=[column.name for column in SearchCandidate.__table__.columns])
        task_ids = select(CollectionTask.id).where(CollectionTask.run_id == run_id)
        write_rows(output_dir / "incidents.csv", [
            {column.name: getattr(item, column.name) for column in item.__table__.columns}
            for item in db.scalars(select(Incident).where(Incident.task_id.in_(task_ids)))
        ], fieldnames=[column.name for column in Incident.__table__.columns])
    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--include-fixture-inputs", action="store_true")
    args = parser.parse_args()
    destination = args.output or PROJECT_ROOT / "artifacts/runs/current" / args.run_id
    print(export_run(args.run_id, destination.resolve(), include_fixture_inputs=args.include_fixture_inputs))
