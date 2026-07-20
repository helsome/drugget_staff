from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import openpyxl

from .catalog import BRAND_TO_GENERIC, find_target_brand, normalize_brand, normalize_spec, parse_package_units


ANTUO_FILE = "安托监控数据2026年4-6月.xlsx"
QUWEI_FILE = "趣维1-3月总数据.xlsx"
STORE_FILE = "网络店铺档案明细表_2026.xlsx"
STORE_SNAPSHOT_FILE = "7.14抓取结果/store_archive_full.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_value(value: object) -> bool:
    return value is not None and str(value).strip() not in {"", "/", "待定", "无", "nan"}


def excel_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def as_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class QualityIssue:
    source: str
    issue_type: str
    severity: str
    row_number: int | None
    business_key: str | None
    details: dict[str, Any] = field(default_factory=dict)
    quarantined: bool = True


@dataclass
class DataAuditReport:
    generated_at: str
    source_rows: dict[str, int]
    source_hashes: dict[str, str]
    recognized_rows: int
    unrecognized_rows: int
    covered_brands: list[str]
    uncovered_brands: list[str]
    quwei_exact_duplicates: int
    quwei_business_key_duplicates: int
    quwei_price_formula_mismatches: int
    store_stats: dict[str, Any]
    packages: list[dict[str, Any]]
    issues: list[QualityIssue]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def iter_sheet(path: Path):
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    # Two source workbooks contain incorrect worksheet dimensions (A1 and XFB).
    # Streaming mode otherwise silently drops the store archive or allocates 16k
    # empty columns per row. Recalculate from actual cells without editing source.
    sheet.reset_dimensions()
    rows = sheet.iter_rows(values_only=True)
    headers = list(next(rows))
    index = {header: pos for pos, header in enumerate(headers) if header is not None}
    try:
        for row_number, row in enumerate(rows, start=2):
            yield row_number, index, row
    finally:
        workbook.close()


def load_store_records(path: Path) -> list[dict[str, Any]]:
    """Load the responsibility archive from the workbook itself.

    The supplied workbook advertises an incorrect A1 worksheet dimension; the
    iterator resets dimensions before reading, so the JSON archive is never the
    source of responsibility facts. It remains only a legacy fallback when a
    future workbook genuinely contains no data rows.
    """
    records: list[dict[str, Any]] = []
    for _, index, row in iter_sheet(path):
        records.append({header: row[position] if position < len(row) else None for header, position in index.items()})
    return records


def audit_sources(source_dir: Path) -> DataAuditReport:
    antuo_path = source_dir / ANTUO_FILE
    quwei_path = source_dir / QUWEI_FILE
    store_path = source_dir / STORE_FILE
    issues: list[QualityIssue] = []
    covered: set[str] = set()
    package_samples: dict[tuple[str, str], dict[str, Any]] = {}
    antuo_rows = 0
    quwei_rows = 0
    recognized = 0
    unrecognized = 0

    for row_number, idx, row in iter_sheet(antuo_path):
        antuo_rows += 1
        brand = normalize_brand(row[idx["品牌"]])
        if brand not in BRAND_TO_GENERIC:
            unrecognized += 1
            issues.append(
                QualityIssue(
                    source=ANTUO_FILE,
                    issue_type="unrecognized_drug",
                    severity="high",
                    row_number=row_number,
                    business_key=str(row[idx.get("商品ID")]),
                    details={"brand": brand, "spec": row[idx.get("规格")]},
                )
            )
            continue
        recognized += 1
        covered.add(brand)
        spec_raw = row[idx["规格"]]
        spec = normalize_spec(spec_raw) or "UNKNOWN"
        units, min_unit = parse_package_units(spec)
        key = (brand, spec)
        sample = package_samples.setdefault(
            key,
            {
                "brand": brand,
                "generic_name": BRAND_TO_GENERIC[brand],
                "spec_raw_examples": set(),
                "spec_normalized": spec,
                "units_per_box": str(units) if units is not None else None,
                "min_unit": min_unit,
                "sources": set(),
                "record_count": 0,
            },
        )
        sample["spec_raw_examples"].add(str(spec_raw))
        sample["sources"].add(ANTUO_FILE)
        sample["record_count"] += 1

    exact_seen: Counter[tuple[Any, ...]] = Counter()
    business_seen: Counter[tuple[Any, ...]] = Counter()
    mismatch_count = 0
    for row_number, idx, row in iter_sheet(quwei_path):
        quwei_rows += 1
        brand = find_target_brand(row[idx["商品关键字"]], row[idx["商品标题"]], row[idx["规格"]])
        exact_key = tuple(row[pos] for pos in range(len(row)))
        business_key = (
            row[idx["采集时间"]],
            row[idx["平台"]],
            row[idx["商品链接"]],
            row[idx["规格"]],
            row[idx["店铺名称"]],
        )
        exact_seen[exact_key] += 1
        business_seen[business_key] += 1
        price = as_decimal(row[idx["当前价格"]])
        boxes = as_decimal(row[idx["盒数"]])
        single_box = as_decimal(row[idx["单盒价"]])
        if price is not None and boxes and single_box is not None and abs(price / boxes - single_box) > Decimal("0.01"):
            mismatch_count += 1
            issues.append(
                QualityIssue(
                    source=QUWEI_FILE,
                    issue_type="single_box_formula_mismatch",
                    severity="high",
                    row_number=row_number,
                    business_key=str(row[idx["商品链接"]]),
                    details={
                        "captured_date": str(excel_date(row[idx["采集时间"]])),
                        "current_price": str(price),
                        "box_count": str(boxes),
                        "single_box_price": str(single_box),
                    },
                )
            )
        if not brand:
            unrecognized += 1
            issues.append(
                QualityIssue(
                    source=QUWEI_FILE,
                    issue_type="unrecognized_drug",
                    severity="medium",
                    row_number=row_number,
                    business_key=str(row[idx["商品链接"]]),
                    details={
                        "keyword": row[idx["商品关键字"]],
                        "spec": row[idx["规格"]],
                    },
                )
            )
            continue
        recognized += 1
        covered.add(brand)
        spec_raw = row[idx["规格"]]
        spec = normalize_spec(spec_raw) or "UNKNOWN"
        units, min_unit = parse_package_units(spec)
        key = (brand, spec)
        sample = package_samples.setdefault(
            key,
            {
                "brand": brand,
                "generic_name": BRAND_TO_GENERIC[brand],
                "spec_raw_examples": set(),
                "spec_normalized": spec,
                "units_per_box": str(units) if units is not None else None,
                "min_unit": min_unit,
                "sources": set(),
                "record_count": 0,
            },
        )
        sample["spec_raw_examples"].add(str(spec_raw))
        sample["sources"].add(QUWEI_FILE)
        sample["record_count"] += 1

    exact_duplicates = sum(count - 1 for count in exact_seen.values() if count > 1)
    business_duplicates = sum(count - 1 for count in business_seen.values() if count > 1)

    status_count: Counter[str] = Counter()
    platform_count: Counter[str] = Counter()
    platform_stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    store_rows = 0
    excel_store_records = load_store_records(store_path)
    store_records = excel_store_records
    if not store_records:
        snapshot_path = source_dir.parent / STORE_SNAPSHOT_FILE
        store_records = json.loads(snapshot_path.read_text(encoding="utf-8"))["stores"]
        issues.append(
            QualityIssue(
                source=STORE_FILE,
                issue_type="workbook_empty_fallback_snapshot",
                severity="high",
                row_number=None,
                business_key=None,
                details={"fallback": STORE_SNAPSHOT_FILE},
            )
        )
    for record in store_records:
        store_rows += 1
        platform = str(record.get("平台") or "")
        status = str(record.get("店铺状态") or "")
        status_count[status] += 1
        platform_count[platform] += 1
        platform_stats[platform]["total"] += 1
        if status == "正常":
            platform_stats[platform]["normal"] += 1
            if valid_value(record.get("涉及品种")):
                platform_stats[platform]["normal_involved"] += 1
            if any(
                valid_value(record.get(field))
                for field in ("责任单位", "责任人", "联系人")
            ):
                platform_stats[platform]["normal_responsibility"] += 1

    packages = []
    for sample in package_samples.values():
        sample["spec_raw_examples"] = sorted(sample["spec_raw_examples"])
        sample["sources"] = sorted(sample["sources"])
        packages.append(sample)
    packages.sort(key=lambda item: (item["brand"], item["spec_normalized"]))

    all_brands = set(BRAND_TO_GENERIC)
    return DataAuditReport(
        generated_at=datetime.now().isoformat(),
        source_rows={
            ANTUO_FILE: antuo_rows,
            QUWEI_FILE: quwei_rows,
            STORE_FILE: len(excel_store_records),
            # The fallback snapshot is historical evidence, not a current
            # responsibility source for real notifications.
            STORE_SNAPSHOT_FILE: len(store_records),
        },
        source_hashes={
            ANTUO_FILE: sha256_file(antuo_path),
            QUWEI_FILE: sha256_file(quwei_path),
            STORE_FILE: sha256_file(store_path),
            STORE_SNAPSHOT_FILE: sha256_file(source_dir.parent / STORE_SNAPSHOT_FILE),
        },
        recognized_rows=recognized,
        unrecognized_rows=unrecognized,
        covered_brands=sorted(covered),
        uncovered_brands=sorted(all_brands - covered),
        quwei_exact_duplicates=exact_duplicates,
        quwei_business_key_duplicates=business_duplicates,
        quwei_price_formula_mismatches=mismatch_count,
        store_stats={
            "status_distribution": dict(status_count),
            "platform_distribution": dict(platform_count),
            "by_platform": {key: dict(value) for key, value in platform_stats.items()},
        },
        packages=packages,
        issues=issues,
    )


def write_audit_report(report: DataAuditReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "data_quality_report.json"
    md_path = output_dir / "data_quality_report.md"
    json_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# 历史数据质量审计",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 已识别记录：{report.recognized_rows:,}",
        f"- 未识别记录：{report.unrecognized_rows:,}",
        f"- 有历史覆盖药品：{len(report.covered_brands)} 款（{', '.join(report.covered_brands)}）",
        f"- Search 冷启动药品：{len(report.uncovered_brands)} 款（{', '.join(report.uncovered_brands)}）",
        f"- 趣维完全重复：{report.quwei_exact_duplicates} 条",
        f"- 趣维业务键重复：{report.quwei_business_key_duplicates} 条",
        f"- 趣维单盒价公式不一致：{report.quwei_price_formula_mismatches} 条（已隔离）",
        f"- 店铺档案Excel数据行：{report.source_rows[STORE_FILE]:,}；历史JSON快照：{report.source_rows[STORE_SNAPSHOT_FILE]:,} 家",
        "",
        "原始文件未修改。Excel是责任事实源；JSON仅作一致性对照或Excel为空时的显式回退。真实通知必须每轮重读当前Excel。",
        "明细、行号、文件哈希和隔离原因见同目录 JSON。",
    ]
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return json_path, md_path
