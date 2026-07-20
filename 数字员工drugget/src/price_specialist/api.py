import csv
import io
from collections.abc import Generator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .collector import OpenCLIComputerUseCollector
from .database import create_db_engine, init_database, make_session_factory
from .errors import AppError
from .incidents import IncidentService
from .models import CentralAssignmentQueue, CollectionRun, NotificationDelivery, PriceBreakEvent, PriceComparison
from .schemas import IncidentAction


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()
    engine = create_db_engine(cfg.database_url)
    init_database(engine)
    session_factory = make_session_factory(engine)
    app = FastAPI(
        title="价格专员数字员工 API",
        version="0.1.0",
        description="固定重点药房监控与开放启发式搜索的本地控制面。",
    )

    def get_session() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    SessionDependency = Annotated[Session, Depends(get_session)]

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/workbench", response_class=HTMLResponse)
    def workbench() -> str:
        return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>价格专员·人工验证队列</title>
<style>body{font-family:system-ui;margin:30px;background:#f4f6fa;color:#182235}h1{margin-bottom:6px}.hint{color:#65738b}table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:10px;border-bottom:1px solid #e4e8ef;text-align:left;vertical-align:top}button{margin:2px;padding:6px 9px}code{word-break:break-all}.empty{background:#fff;padding:28px}</style>
</head><body><h1>集中人工验证队列</h1><p class="hint">请在原持久化会话中完成验证，然后点“验证完成”。系统会先重检登录态，只重新入队当前任务。</p><div id="root">加载中…</div>
<script>const E=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
async function patch(id,action){let operator_note=prompt('备注（可留空）')||null;let r=await fetch(`/v1/incidents/${id}`,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({action,operator_note})});if(!r.ok)alert(await r.text());load()}
async function resume(id){let r=await fetch(`/v1/incidents/${id}/resume-check`,{method:'POST'});let x=await r.json();if(!r.ok)alert(JSON.stringify(x));else alert(`恢复检查：${x.collection_status}`);load()}
async function load(){let x=await(await fetch('/v1/incidents?limit=100')).json(),root=document.getElementById('root');x.items=x.items.filter(i=>['pending_human','in_progress','deferred'].includes(i.status));if(!x.items.length){root.innerHTML='<div class="empty">当前没有待人工处理事件。</div>';return}root.innerHTML='<table><tr><th>平台/类型</th><th>任务/会话</th><th>页面上下文</th><th>操作</th></tr>'+x.items.map(i=>`<tr><td><b>${E(i.platform)}</b><br>${E(i.incident_type)}<br>${E(i.status)}</td><td><code>${E(i.task_id)}</code></td><td>${E(i.page_title)}<br><code>${E(i.current_url)}</code>${i.screenshot_path?`<br><a href="/v1/incidents/${E(i.id)}/screenshot" target="_blank">查看截图</a>`:''}</td><td>${['pending_human','deferred'].includes(i.status)?`<button onclick="patch('${E(i.id)}','in_progress')">开始处理</button>`:`<button onclick="resume('${E(i.id)}')">验证完成/恢复检查</button>`}<button onclick="patch('${E(i.id)}','deferred')">延期</button><button onclick="patch('${E(i.id)}','session_disabled')">禁用会话</button></td></tr>`).join('')+'</table>'}load();setInterval(load,15000)</script></body></html>"""

    @app.get("/ready")
    def ready(session: SessionDependency) -> dict[str, str]:
        try:
            session.execute(select(1))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ready"}

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, session: SessionDependency) -> dict[str, object]:
        run = session.get(CollectionRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "id": run.id,
            "status": run.status,
            "fixed_status": run.fixed_status,
            "search_status": run.search_status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "summary": run.summary,
        }

    @app.get("/v1/price-comparisons")
    def list_price_comparisons(session: SessionDependency) -> dict[str, object]:
        rows = list(session.scalars(select(PriceComparison).order_by(PriceComparison.created_at.desc())))
        return {"items": [{"id": row.id, "observation_id": row.observation_id, "verdict": row.verdict, "reason_code": row.reason_code, "comparison_unit_price": row.comparison_unit_price, "control_price": row.control_price, "difference": row.difference, "rule": row.rule_snapshot, "evidence": row.detail_evidence_snapshot} for row in rows]}

    @app.get("/v1/price-break-events")
    def list_price_break_events(session: SessionDependency) -> dict[str, object]:
        rows = list(session.scalars(select(PriceBreakEvent).order_by(PriceBreakEvent.created_at.desc())))
        return {"items": [{"id": row.id, "comparison_id": row.comparison_id, "observation_id": row.observation_id, "routing_status": row.routing_status, "event_status": row.event_status, "payload": row.payload} for row in rows]}

    @app.get("/v1/assignment-queue")
    def list_assignment_queue(session: SessionDependency) -> dict[str, object]:
        rows = list(session.scalars(select(CentralAssignmentQueue).order_by(CentralAssignmentQueue.created_at.desc())))
        return {"items": [{"id": row.id, "event_id": row.event_id, "reason_code": row.reason_code, "status": row.status, "payload": row.payload} for row in rows]}

    @app.get("/v1/price-break-events/export.csv")
    def export_price_break_events(session: SessionDependency) -> StreamingResponse:
        rows = list(session.scalars(select(PriceBreakEvent).order_by(PriceBreakEvent.created_at.desc())))
        output = io.StringIO()
        fields = ["event_id", "verdict", "药品", "规格", "店铺", "页面价格", "单盒价格", "最小单位价格", "控价", "差额", "责任人", "联系人", "路由状态", "控价来源", "详情证据"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for event in rows:
            detail = event.payload.get("detail_evidence", {})
            rule = event.payload.get("control_rule", {})
            delivery = session.scalar(select(NotificationDelivery).where(NotificationDelivery.event_id == event.id))
            preview = delivery.payload if delivery else {}
            writer.writerow({"event_id": event.id, "verdict": event.payload.get("verdict"), "药品": detail.get("brand"), "规格": detail.get("selected_spec"), "店铺": detail.get("page_shop"), "页面价格": detail.get("page_price"), "单盒价格": detail.get("single_box_price"), "最小单位价格": detail.get("single_unit_price"), "控价": rule.get("price_per_min_unit"), "差额": event.payload.get("difference"), "责任人": preview.get("responsible_person"), "联系人": delivery.recipient if delivery else None, "路由状态": event.routing_status, "控价来源": f"{rule.get('source_file') or ''}:{rule.get('source_line_number') or ''} {rule.get('source_line') or ''}".strip(), "详情证据": detail.get("evidence_path")})
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=price-break-events.csv"})

    @app.get("/v1/incidents")
    def list_incidents(
        session: SessionDependency,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        rows, total = IncidentService(session).list(status=status, limit=limit, offset=offset)
        return {
            "items": [
                {
                    "id": row.id,
                    "task_id": row.task_id,
                    "platform": row.platform,
                    "incident_type": row.incident_type,
                    "status": row.status,
                    "current_url": row.current_url,
                    "page_title": row.page_title,
                    "screenshot_path": row.screenshot_path,
                    "detected_at": row.detected_at,
                    "operator_note": row.operator_note,
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.patch("/v1/incidents/{incident_id}")
    def update_incident(
        incident_id: str,
        payload: IncidentAction,
        session: SessionDependency,
    ) -> dict[str, object]:
        row = IncidentService(session).transition(
            incident_id,
            action=payload.action,
            operator_note=payload.operator_note,
        )
        return {"id": row.id, "status": row.status, "operator_note": row.operator_note}

    @app.post("/v1/incidents/{incident_id}/resume-check")
    async def resume_check(incident_id: str, session: SessionDependency) -> dict[str, object]:
        row, result = await IncidentService(session).resume_check(
            incident_id,
            collector=OpenCLIComputerUseCollector(cfg),
        )
        return {
            "id": row.id,
            "status": row.status,
            "collection_status": result.collection_status.value,
            "requeued_current_task": row.status == "resolved",
        }

    @app.get("/v1/incidents/{incident_id}/screenshot")
    def incident_screenshot(incident_id: str, session: SessionDependency) -> FileResponse:
        row = IncidentService(session).get(incident_id)
        path = Path(row.screenshot_path or "")
        if not row.screenshot_path or not path.is_file():
            raise HTTPException(status_code=404, detail="screenshot not found")
        try:
            path.resolve().relative_to(cfg.evidence_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="invalid screenshot path") from exc
        return FileResponse(path)

    return app


app = create_app()
