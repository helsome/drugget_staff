from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from .collector import ComputerUseCollector
from .enums import CollectionStatus
from .enums import IncidentStatus, TaskStatus
from .errors import InvalidStateError, NotFoundError
from .models import CollectionTask, Incident
from .schemas import BrowserSession, CollectionResult


ALLOWED_TRANSITIONS: dict[IncidentStatus, set[IncidentStatus]] = {
    IncidentStatus.PENDING_HUMAN: {
        IncidentStatus.IN_PROGRESS,
        IncidentStatus.DEFERRED,
        IncidentStatus.ABANDONED,
        IncidentStatus.SESSION_DISABLED,
    },
    IncidentStatus.IN_PROGRESS: {
        IncidentStatus.RETRY_READY,
        IncidentStatus.DEFERRED,
        IncidentStatus.ABANDONED,
        IncidentStatus.SESSION_DISABLED,
    },
    IncidentStatus.DEFERRED: {IncidentStatus.IN_PROGRESS, IncidentStatus.ABANDONED},
    IncidentStatus.RETRY_READY: {IncidentStatus.RESOLVED, IncidentStatus.IN_PROGRESS},
    IncidentStatus.RESOLVED: set(),
    IncidentStatus.ABANDONED: set(),
    IncidentStatus.SESSION_DISABLED: {IncidentStatus.IN_PROGRESS, IncidentStatus.ABANDONED},
}


class IncidentService:
    def __init__(self, session: Session):
        self.session = session

    def list(self, *, status: str | None, limit: int, offset: int) -> tuple[list[Incident], int]:
        query: Select[tuple[Incident]] = select(Incident).order_by(Incident.detected_at.desc())
        count_query = select(func.count()).select_from(Incident)
        if status:
            query = query.where(Incident.status == status)
            count_query = count_query.where(Incident.status == status)
        rows = list(self.session.scalars(query.limit(limit).offset(offset)))
        total = int(self.session.scalar(count_query) or 0)
        return rows, total

    def get(self, incident_id: str) -> Incident:
        incident = self.session.get(Incident, incident_id)
        if incident is None:
            raise NotFoundError(f"incident {incident_id} 不存在")
        return incident

    def create(
        self,
        task: CollectionTask,
        result: CollectionResult,
        screenshot_path: str | None,
    ) -> Incident:
        prior_count = int(
            self.session.scalar(
                select(func.count())
                .select_from(Incident)
                .where(
                    Incident.task_id == task.id,
                    Incident.incident_type.in_(["challenge_detected", "login_required"]),
                )
            )
            or 0
        )
        is_rate_limit = result.collection_status == CollectionStatus.RATE_LIMITED
        exhausted = result.collection_status in {
            CollectionStatus.CHALLENGE_DETECTED,
            CollectionStatus.LOGIN_REQUIRED,
        } and prior_count >= 2
        status = (
            IncidentStatus.DEFERRED
            if is_rate_limit
            else IncidentStatus.SESSION_DISABLED
            if exhausted
            else IncidentStatus.PENDING_HUMAN
        )
        task.status = (
            TaskStatus.DEFERRED.value
            if status in {IncidentStatus.DEFERRED, IncidentStatus.SESSION_DISABLED}
            else TaskStatus.HUMAN_REQUIRED.value
        )
        incident = Incident(
            task_id=task.id,
            platform=task.platform,
            incident_type=result.collection_status.value,
            status=status.value,
            session_alias=task.session_alias,
            current_url=result.final_url,
            page_title=result.page_title,
            screenshot_path=screenshot_path,
            resume_count=prior_count,
            operator_note=(
                "当轮限流，不在当前进程重试"
                if is_rate_limit
                else "连续两次恢复后仍触发验证，会话已禁用"
                if exhausted
                else None
            ),
        )
        self.session.add(incident)
        self.session.flush()
        return incident

    def transition(
        self,
        incident_id: str,
        *,
        action: IncidentStatus,
        operator_note: str | None,
    ) -> Incident:
        incident = self.get(incident_id)
        current = IncidentStatus(incident.status)
        if action not in ALLOWED_TRANSITIONS[current]:
            raise InvalidStateError(
                f"incident不能从{current.value}切换到{action.value}",
                details={"allowed": sorted(value.value for value in ALLOWED_TRANSITIONS[current])},
            )
        incident.status = action.value
        incident.operator_note = operator_note
        incident.updated_at = datetime.now()
        if action == IncidentStatus.RESOLVED:
            task = self.session.get(CollectionTask, incident.task_id)
            if task is not None:
                task.status = TaskStatus.PENDING.value
                task.leased_at = None
                task.completed_at = None
        elif action in {IncidentStatus.DEFERRED, IncidentStatus.SESSION_DISABLED, IncidentStatus.ABANDONED}:
            task = self.session.get(CollectionTask, incident.task_id)
            if task is not None:
                task.status = TaskStatus.DEFERRED.value
        self.session.flush()
        return incident

    async def resume_check(
        self,
        incident_id: str,
        *,
        collector: ComputerUseCollector,
    ) -> tuple[Incident, CollectionResult]:
        incident = self.get(incident_id)
        if IncidentStatus(incident.status) != IncidentStatus.IN_PROGRESS:
            raise InvalidStateError("只有处理中的incident可执行恢复检查")
        result = await collector.resume_incident(
            incident.id,
            BrowserSession(platform=incident.platform, alias=incident.session_alias),
        )
        if result.collection_status == CollectionStatus.SUCCESS:
            self.transition(incident.id, action=IncidentStatus.RETRY_READY, operator_note="登录态恢复检查通过")
            return (
                self.transition(incident.id, action=IncidentStatus.RESOLVED, operator_note="已只重新入队当前任务"),
                result,
            )
        incident.resume_count += 1
        incident.operator_note = f"恢复检查未通过：{result.collection_status.value}"
        incident.updated_at = datetime.now()
        if incident.resume_count >= 2:
            incident.status = IncidentStatus.SESSION_DISABLED.value
            task = self.session.get(CollectionTask, incident.task_id)
            if task is not None:
                task.status = TaskStatus.DEFERRED.value
        self.session.flush()
        return incident, result
