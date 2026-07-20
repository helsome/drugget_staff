from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler


def build_scheduler(
    weekly_job: Callable[[], None],
    *,
    day_of_week: str = "mon",
    hour: int = 9,
    minute: int = 0,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        weekly_job,
        trigger="cron",
        day_of_week=day_of_week,
        hour=hour,
        minute=minute,
        id="weekly-price-specialist",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        next_run_time=None,
    )
    return scheduler


def scheduler_description(*, day_of_week: str = "mon", hour: int = 9, minute: int = 0) -> dict[str, object]:
    return {
        "timezone": "Asia/Shanghai",
        "day_of_week": day_of_week,
        "time": f"{hour:02d}:{minute:02d}",
        "enabled_by_default": False,
        "generated_at": datetime.now().astimezone().isoformat(),
        "note": "P0仅提供调度定义；业务确认控价与通知渠道后再启用常驻进程。",
    }

