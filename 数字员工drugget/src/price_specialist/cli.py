from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from pathlib import Path

import typer
from sqlalchemy import select

from .api import create_app
from .bootstrap import bootstrap_reference_data, sync_control_price_rules
from .catalog import BRAND_TO_GENERIC, import_control_price_rules
from .collector import OpenCLIComputerUseCollector
from .config import Settings
from .data_quality import audit_sources, write_audit_report
from .database import configured_database, init_database
from .enums import FixedTier, TaskType
from .evidence import EvidenceStore
from .logging_config import configure_logging
from .models import CollectionRun, CollectionTask, DrugProduct, MonitorTarget, PackageMaster, PriceComparison, StoreResponsibility
from .offline_search import classify_existing_search
from .orchestrator import BatchOrchestrator
from .replay import audit_legacy_smoke, write_replay_report
from .scheduler import scheduler_description
from .schemas import BrowserSession, CollectionTaskSpec
from .search import weekly_search_cohort
from .services import TaskQueueService
from .decisions import PriceDecisionService
from .alerts import AlertDryRunService
from .smoke_plan import build_smoke_plan


PROJECT_DIR = Path(__file__).resolve().parents[2]
app = typer.Typer(no_args_is_help=True, help="药品价格专员：固定监控 + 启发式 Search")


def settings() -> Settings:
    return Settings.from_env(PROJECT_DIR)


@app.command("db-init")
def db_init() -> None:
    engine, _ = configured_database(settings())
    init_database(engine)
    typer.echo("database schema ready")


@app.command("control-rules-import")
def control_rules_import(
    input_path: Path = typer.Option(..., "--input", exists=True, dir_okay=False),
    target_path: Path = typer.Option(PROJECT_DIR / "data/knowledge-base/control_price_rules.csv", "--target", dir_okay=False),
) -> None:
    """Validate and merge approved control rules into CSV only; never writes SQLite."""
    typer.echo(json.dumps(import_control_price_rules(input_path=input_path, target_path=target_path), ensure_ascii=False))


@app.command("control-rules-sync")
def control_rules_sync(
    source_path: Path = typer.Option(PROJECT_DIR / "data/knowledge-base/control_price_rules.csv", "--source", exists=True, dir_okay=False),
) -> None:
    """Apply validated control-rule CSV changes through the app, without manual SQLite edits."""
    engine, factory = configured_database(settings())
    init_database(engine)
    with factory.begin() as session:
        result = sync_control_price_rules(session, control_path=source_path)
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("control-rules-coverage")
def control_rules_coverage(
    source_path: Path = typer.Option(PROJECT_DIR / "data/knowledge-base/control_price_rules.csv", "--source", exists=True, dir_okay=False),
) -> None:
    """Print a read-only coverage report for the curated control-price rules CSV."""
    from scripts.control_price_coverage import compute_coverage

    report = compute_coverage(source_path)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("price-judge")
def price_judge(observation_id: str = typer.Option(..., "--observation-id")) -> None:
    """Persist a strict price decision; this command never sends notifications."""
    engine, factory = configured_database(settings())
    init_database(engine)
    with factory.begin() as session:
        decision = PriceDecisionService(session).evaluate_observation(observation_id)
    typer.echo(json.dumps({"id": decision.id, "observation_id": decision.observation_id, "verdict": decision.verdict, "reason_code": decision.reason_code}, ensure_ascii=False))


@app.command("route-dry-run")
def route_dry_run(comparison_id: str = typer.Option(..., "--comparison-id")) -> None:
    """Create/reuse a local dry-run case and notification preview; never sends."""
    engine, factory = configured_database(settings())
    init_database(engine)
    with factory.begin() as session:
        comparison = session.get(PriceComparison, comparison_id)
        if comparison is None:
            raise typer.BadParameter("price comparison not found", param_hint="--comparison-id")
        alerts = AlertDryRunService()
        event = alerts.ensure_event(session, comparison=comparison)
        result = {"comparison_id": comparison.id, "event": None}
        if event is not None:
            result["event"] = alerts.route_preview(session, event=event)
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("audit-data")
def audit_data(
    source_dir: Path = typer.Option(PROJECT_DIR / "data/raw", exists=True, file_okay=False),
    output_dir: Path = typer.Option(PROJECT_DIR / "outputs/data-quality", file_okay=False),
) -> None:
    """Audit source workbooks read-only and produce row-level quarantine records."""
    report = audit_sources(source_dir)
    json_path, markdown_path = write_audit_report(report, output_dir)
    typer.echo(json.dumps({"json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))


@app.command("build-smoke-plan")
def smoke_plan(
    source_dir: Path = typer.Option(PROJECT_DIR / "data/raw", exists=True, file_okay=False),
    stores: Path = typer.Option(PROJECT_DIR / "archive/legacy-2026-07-14/smoke_test_stores.json", exists=True, dir_okay=False),
    output: Path = typer.Option(PROJECT_DIR / "outputs/smoke/smoke_plan.json", dir_okay=False),
) -> None:
    result = build_smoke_plan(source_dir=source_dir, smoke_store_path=stores, output_path=output)
    typer.echo(json.dumps({"output": str(output), "jd_unique_stores": result["jd_unique_stores"], "taobao_unique_stores": result["taobao_unique_stores"]}, ensure_ascii=False))


@app.command("bootstrap")
def bootstrap(
    source_dir: Path = typer.Option(PROJECT_DIR / "data/raw", exists=True, file_okay=False),
    smoke_plan_path: Path = typer.Option(PROJECT_DIR / "outputs/smoke/smoke_plan.json", exists=True, dir_okay=False),
) -> None:
    """Load the 30-drug catalog, packages, current controls, stores and fixed targets."""
    cfg = settings()
    report = audit_sources(source_dir)
    plan = json.loads(smoke_plan_path.read_text(encoding="utf-8"))
    engine, factory = configured_database(cfg)
    init_database(engine)
    with factory.begin() as session:
        result = bootstrap_reference_data(
            session,
            source_dir=source_dir,
            audit_report=report,
            smoke_plan=plan,
        )
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("classify-search")
def classify_search(
    results: Path = typer.Option(PROJECT_DIR / "archive/legacy-2026-07-14/smoke_test_results_fixed.json", exists=True, dir_okay=False),
    targets: Path = typer.Option(PROJECT_DIR / "archive/legacy-2026-07-14/smoke_test_targets.json", exists=True, dir_okay=False),
    stores: Path = typer.Option(PROJECT_DIR / "archive/legacy-2026-07-14/store_matching.json", exists=True, dir_okay=False),
    output: Path = typer.Option(PROJECT_DIR / "outputs/search/offline_candidates.json", dir_okay=False),
) -> None:
    report = classify_existing_search(
        result_path=results,
        target_path=targets,
        store_matching_path=stores,
        output_path=output,
    )
    typer.echo(json.dumps({key: report[key] for key in ("raw_item_count", "deduplicated_candidate_count", "classification_rate", "formal_price_count")}, ensure_ascii=False))


@app.command("enqueue-p0")
def enqueue_p0() -> None:
    """Create an ordered P0 run: core, observation, then two-stage/fallback Search."""
    cfg = settings()
    engine, factory = configured_database(cfg)
    init_database(engine)
    with factory.begin() as session:
        queue = TaskQueueService(session)
        run = queue.create_run()
        drugs = list(session.scalars(select(DrugProduct).order_by(DrugProduct.brand_name)))
        packages: dict[str, PackageMaster] = {}
        for package in session.scalars(select(PackageMaster).order_by(PackageMaster.verified.desc())):
            packages.setdefault(package.drug_id, package)
        fixed_count = 0
        target_rows = session.execute(
            select(MonitorTarget, DrugProduct, StoreResponsibility)
            .join(DrugProduct, MonitorTarget.drug_id == DrugProduct.id)
            .outerjoin(StoreResponsibility, MonitorTarget.store_id == StoreResponsibility.id)
            .where(MonitorTarget.enabled.is_(True))
        )
        for target, drug, store in target_rows:
            tier = FixedTier(target.fixed_tier)
            spec = CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, target_id=target.id,
                platform=target.platform, session_alias=f"{target.platform}-p0",
                task_type=TaskType.FIXED_CORE if tier == FixedTier.RESPONSIBILITY_CORE else TaskType.FIXED_OBSERVATION,
                priority=10 if tier == FixedTier.RESPONSIBILITY_CORE else 20,
                drug_name=drug.brand_name, generic_name=drug.generic_name,
                spec=target.spec_normalized, shop_name=store.shop_name if store else None,
                product_id=target.product_id, url=target.url, fixed_tier=tier,
                metadata={
                    "stable_link": target.stable_link,
                    "expected_box_count": target.stable_link_evidence.get("historical_box_count"),
                },
            )
            queue.enqueue(spec)
            fixed_count += 1

        search_count = 0
        for platform in cfg.allowed_platforms:
            for drug in drugs:
                package = packages.get(drug.id)
                query_specs = [(f"{drug.brand_name} {drug.generic_name}", 50, False)]
                if package:
                    query_specs.extend([
                        (f"{drug.brand_name} {package.spec_normalized}", 51, False),
                        (f"{drug.generic_name} {package.spec_normalized}", 52, True),
                    ])
                for query, priority, fallback in query_specs:
                    queue.enqueue(CollectionTaskSpec(
                        task_id=str(uuid.uuid4()), run_id=run.id,
                        platform=platform, session_alias=f"{platform}-p0", task_type=TaskType.SEARCH,
                        priority=priority, drug_name=drug.brand_name, generic_name=drug.generic_name,
                        spec=package.spec_normalized if package else None, query=query,
                        metadata={"drug_id": drug.id, "target_brand": drug.brand_name,
                                  "target_spec": package.spec_normalized if package else None,
                                  "fallback_only": fallback},
                    ))
                    search_count += 1
        run.summary = {"fixed_tasks": fixed_count, "search_tasks": search_count, "notifications": "dry_run"}
    typer.echo(json.dumps({"run_id": run.id, "fixed_tasks": fixed_count, "search_tasks": search_count}, ensure_ascii=False))


@app.command("enqueue-yaoshibang-global-batch")
def enqueue_yaoshibang_global_batch(
    limit: int = typer.Option(20, min=1, help="本批次药品数"),
    offset: int = typer.Option(0, min=0, help="按药品名称排序后的起始偏移"),
    inspect_limit: int = typer.Option(5, min=1, help="每个药品最多生成的详情候选数"),
) -> None:
    """仅为指定批次药品创建药师帮 GLOBAL_SEARCH 任务。

    该入口不创建 STORE_SEARCH。默认每个药品只创建一条品牌+通用名搜索，
    搜索成功后由批次运行器为每个药品最多生成 5 个详情候选，
    用于测试药师帮大规模抓取能力。
    """
    cfg = settings()
    engine, factory = configured_database(cfg)
    init_database(engine)
    with factory.begin() as session:
        drugs = list(session.scalars(
            select(DrugProduct).order_by(DrugProduct.brand_name).offset(offset).limit(limit)
        ))
        if not drugs:
            raise typer.BadParameter("指定范围内没有药品")
        run = TaskQueueService(session).create_run()
        for index, drug in enumerate(drugs):
            TaskQueueService(session).enqueue(CollectionTaskSpec(
                task_id=str(uuid.uuid4()), run_id=run.id, platform="yaoshibang",
                session_alias="yaoshibang-p0", task_type=TaskType.SEARCH,
                priority=50 + index, drug_name=drug.brand_name,
                generic_name=drug.generic_name,
                query=f"{drug.brand_name} {drug.generic_name}",
                metadata={
                    "drug_id": drug.id,
                    "target_brand": drug.brand_name,
                    "target_spec": None,
                    "route": "global",
                    "batch_type": "yaoshibang_global_20",
                    "inspect_limit": inspect_limit,
                },
            ))
        run.summary = {
            "batch_type": "yaoshibang_global_20",
            "platform": "yaoshibang",
            "drug_count": len(drugs),
            "offset": offset,
            "limit": limit,
            "search_tasks": len(drugs),
            "inspect_limit": inspect_limit,
            "notifications": "dry_run",
        }
        payload = {"run_id": run.id, "drug_count": len(drugs), "search_tasks": len(drugs),
                   "drugs": [drug.brand_name for drug in drugs]}
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("run-batch")
def run_batch(
    run_id: str | None = typer.Option(None, help="只执行指定run；省略时使用最新pending run"),
    max_tasks_per_platform: int | None = typer.Option(None, min=1, help="每个平台最多执行多少条，用于有界烟测"),
    parallel: bool = typer.Option(True, help="淘宝和药师帮使用独立会话并行抓取"),
) -> None:
    """Run one queued P0 run; Search and fixed tasks retain independent records."""
    cfg = settings()
    engine, factory = configured_database(cfg)
    init_database(engine)
    collector = OpenCLIComputerUseCollector(cfg)
    with factory() as session:
        selected_run_id = run_id or session.scalar(
            select(CollectionRun.id)
            .where(CollectionRun.status == "pending")
            .order_by(CollectionRun.id.desc())
            .limit(1)
        )
        if not selected_run_id:
            raise typer.BadParameter("没有pending run，请先执行enqueue-p0")
        runner = BatchOrchestrator(
            session=session,
            collector=collector,
            evidence_store=EvidenceStore(cfg.evidence_dir),
            run_id=selected_run_id,
            session_factory=factory,
            collector_factory=lambda: OpenCLIComputerUseCollector(cfg),
        )
        outcome = asyncio.run(
            runner.execute_all(
                {platform: f"{platform}-p0" for platform in cfg.allowed_platforms},
                max_tasks_per_platform=max_tasks_per_platform,
                parallel=parallel,
            )
        )
    typer.echo(json.dumps(outcome, ensure_ascii=False))


@app.command("cancel-run")
def cancel_run(run_id: str) -> None:
    """Cancel pending/leased tasks for an abandoned or superseded run."""
    cfg = settings()
    engine, factory = configured_database(cfg)
    init_database(engine)
    with factory.begin() as session:
        run = session.get(CollectionRun, run_id)
        if run is None:
            raise typer.BadParameter("run不存在")
        run.status = "cancelled"
        session.query(CollectionTask).filter(
            CollectionTask.run_id == run_id,
            CollectionTask.status.in_(("pending", "leased", "running")),
        ).update({"status": "cancelled"}, synchronize_session=False)
    typer.echo(json.dumps({"run_id": run_id, "status": "cancelled"}, ensure_ascii=False))


@app.command("session-health")
def session_health() -> None:
    """Check only whether each authorized persistent session is usable; no account IDs are printed."""
    cfg = settings()
    collector = OpenCLIComputerUseCollector(cfg)

    async def check() -> dict[str, str]:
        output = {}
        for platform in cfg.allowed_platforms:
            result = await collector.health_check(BrowserSession(platform=platform, alias=f"{platform}-p0"))
            output[platform] = result.collection_status.value
        return output

    typer.echo(json.dumps(asyncio.run(check()), ensure_ascii=False))


@app.command("replay-smoke")
def replay_smoke(
    source: Path = typer.Option(PROJECT_DIR / "archive/legacy-2026-07-14/smoke_test_results_fixed.json", exists=True, dir_okay=False),
    metrics: Path = typer.Option(PROJECT_DIR / "METRICS.json", dir_okay=False),
    report: Path = typer.Option(PROJECT_DIR / "outputs/replay/7.14烟测纠偏回放.md", dir_okay=False),
) -> None:
    result = audit_legacy_smoke(source)
    write_replay_report(result, json_path=metrics, markdown_path=report)
    typer.echo(json.dumps({"verdict": result["verdict"], "metrics": str(metrics), "report": str(report)}, ensure_ascii=False))


@app.command("weekly-plan")
def weekly_plan(week: int = typer.Option(date.today().isocalendar().week, min=1, max=53)) -> None:
    brands = list(BRAND_TO_GENERIC)
    high_risk = set(brands[:10])
    cohort = weekly_search_cohort(brands, week_number=week, high_risk=high_risk)
    typer.echo(json.dumps({"week": week, "search_cohort": cohort, "scheduler": scheduler_description()}, ensure_ascii=False, indent=2))


@app.command("serve")
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8000, min=1, max=65535)) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("P0没有认证，只允许绑定本机地址")
    import uvicorn
    configure_logging()
    uvicorn.run(create_app(settings()), host=host, port=port)


if __name__ == "__main__":
    app()
