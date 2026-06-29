from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from croniter import croniter
from telegram.ext import Application

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.task import Task
from app.services.backup_service import backup_service
from app.services.notification_service import notification_service
from app.services.settings_service import SettingsService
from app.services.task_service import TaskService


class SchedulerService:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
        self.bot_app: Application | None = None

    def bind_bot(self, bot_app: Application) -> None:
        self.bot_app = bot_app
        notification_service.bind_bot(bot_app)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _job_id(self, task_id: int) -> str:
        return f"task_{task_id}"

    def remove_task_job(self, task_id: int) -> None:
        self.scheduler.remove_job(self._job_id(task_id)) if self.scheduler.get_job(self._job_id(task_id)) else None

    def remove_backup_job(self) -> None:
        if self.scheduler.get_job("auto_full_backup"):
            self.scheduler.remove_job("auto_full_backup")

    def schedule_task(self, task: Task) -> None:
        if task.status != "pending":
            return
        self.remove_task_job(task.id)

        tz = ZoneInfo(settings.scheduler_timezone)
        now_local = datetime.now(tz)

        if task.is_recurring and task.cron_expr:
            if task.cron_expr.startswith("every_ndays:"):
                _, days_text, hh_text, mm_text = task.cron_expr.split(":")
                interval_days = int(days_text)
                hh = int(hh_text)
                mm = int(mm_text)
                start = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if start <= now_local:
                    start = start + timedelta(days=interval_days)
                trigger = IntervalTrigger(days=interval_days, start_date=start, timezone=tz)
                self.scheduler.add_job(self.push_task_reminder, trigger=trigger, args=[task.id], id=self._job_id(task.id), replace_existing=True)
                return

            minute, hour, day, month, weekday = task.cron_expr.split()
            trigger = CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=weekday, timezone=tz)
            self.scheduler.add_job(self.push_task_reminder, trigger=trigger, args=[task.id], id=self._job_id(task.id), replace_existing=True)
            return

        if task.trigger_time:
            # DB stores UTC naive; convert to local timezone for trigger.
            run_date_utc = task.trigger_time.replace(tzinfo=timezone.utc)
            run_date_local = run_date_utc.astimezone(tz)
            if run_date_local <= now_local:
                run_date_local = now_local + timedelta(seconds=1)
            trigger = DateTrigger(run_date=run_date_local, timezone=tz)
            self.scheduler.add_job(self.push_task_reminder, trigger=trigger, args=[task.id], id=self._job_id(task.id), replace_existing=True)

    async def reload_pending_tasks(self) -> None:
        async with AsyncSessionLocal() as db:
            tasks = await TaskService.list_pending_tasks(db)
            for task in tasks:
                self.schedule_task(task)
        await self.reload_backup_job()

    async def reload_backup_job(self) -> None:
        self.remove_backup_job()
        async with AsyncSessionLocal() as db:
            values = await SettingsService.get_all(db)
        if values.get("backup_enabled", "false").lower() != "true":
            return
        cron_expr = values.get("backup_cron") or "0 3 * * *"
        if not croniter.is_valid(cron_expr):
            return
        minute, hour, day, month, weekday = cron_expr.split()
        trigger = CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=weekday, timezone=settings.scheduler_timezone)
        self.scheduler.add_job(self.send_auto_backup, trigger=trigger, id="auto_full_backup", replace_existing=True)

    async def send_auto_backup(self) -> None:
        async with AsyncSessionLocal() as db:
            await backup_service.send_email_backup(db)

    async def push_task_reminder(self, task_id: int) -> None:
        if self.bot_app is None:
            return

        async with AsyncSessionLocal() as db:
            task = await TaskService.get_task(db, task_id)
            if not task or task.status != "pending":
                return

            await notification_service.send_task_reminder(task)


scheduler_service = SchedulerService()
