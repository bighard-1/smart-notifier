import json

from pydantic import BaseModel
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from croniter import croniter
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import verify_basic_auth
from app.services.backup_service import backup_service
from app.services.channel_service import ChannelService
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.services.scheduler_service import scheduler_service
from app.services.settings_service import SettingsService
from app.services.task_service import TaskService

router = APIRouter(prefix="/api", tags=["tasks"])


class TaskStatusUpdate(BaseModel):
    status: str


class SetupPayload(BaseModel):
    app_name: str = "Smart Notifier"
    scheduler_timezone: str = "Asia/Shanghai"
    telegram_bot_token: str
    web_username: str = "admin"
    web_password: str


class AppSettingsPayload(BaseModel):
    app_name: str | None = None
    scheduler_timezone: str | None = None
    telegram_bot_token: str | None = None
    web_username: str | None = None
    web_password: str | None = None
    backup_enabled: str | None = None
    backup_cron: str | None = None
    backup_email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: str | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: str | None = None


class ChannelPayload(BaseModel):
    name: str
    channel_type: str
    config: dict = {}
    enabled: bool = True


@router.get("/setup/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    return {"configured": await SettingsService.is_configured(db)}


@router.post("/setup")
async def initial_setup(payload: SetupPayload, db: AsyncSession = Depends(get_db)):
    if await SettingsService.is_configured(db):
        raise HTTPException(status_code=409, detail="系统已完成初始化，请登录后修改设置")
    if len(payload.web_password) < 8:
        raise HTTPException(status_code=400, detail="Web 密码至少 8 位")
    values = await SettingsService.set_many(db, payload.model_dump())
    await scheduler_service.reload_backup_job()
    return {"ok": True, "settings": values}


def _present(task):
    local_trigger = TaskService.to_local_display(task.trigger_time)
    channel_ids = []
    if task.channel_ids:
        try:
            channel_ids = json.loads(task.channel_ids)
        except json.JSONDecodeError:
            channel_ids = []
    return TaskRead(
        id=task.id,
        content=task.content,
        remarks=task.remarks,
        is_recurring=task.is_recurring,
        trigger_time=local_trigger,
        cron_expr=task.cron_expr,
        status=task.status,
        snooze_count=task.snooze_count,
        chat_id=task.chat_id,
        channel_ids=channel_ids,
        created_at=task.created_at,
        reminder_type="周期" if task.is_recurring else "一次性",
        rule_text=TaskService.rule_text(task),
        next_run_time=TaskService.next_run_local(task),
    )


@router.get("/tasks", response_model=list[TaskRead])
async def list_tasks(
    chat_id: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_basic_auth),
):
    if status and status not in {"pending", "completed"}:
        raise HTTPException(status_code=400, detail="status must be pending or completed")
    tasks = await TaskService.list_tasks(db, status=status, chat_id=chat_id)
    return [_present(t) for t in tasks]


@router.post("/tasks", response_model=TaskRead)
async def create_task(payload: TaskCreate, db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    task = await TaskService.create_task(db, payload)
    scheduler_service.schedule_task(task)
    return _present(task)


@router.post("/tasks/{task_id}/done", response_model=TaskRead)
async def done_task(task_id: int, db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    task = await TaskService.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task = await TaskService.mark_done(db, task)
    scheduler_service.remove_task_job(task.id)
    return _present(task)


@router.put("/tasks/{task_id}/status", response_model=TaskRead)
async def update_task_status(
    task_id: int,
    payload: TaskStatusUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_basic_auth),
):
    if payload.status not in {"pending", "completed"}:
        raise HTTPException(status_code=400, detail="status must be pending or completed")

    task = await TaskService.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await TaskService.update_status(db, task, payload.status)
    if payload.status == "completed":
        scheduler_service.remove_task_job(task.id)
    else:
        scheduler_service.schedule_task(task)
    return _present(task)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    task = await TaskService.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await TaskService.delete_task(db, task)
    scheduler_service.remove_task_job(task_id)
    return {"ok": True}


@router.put("/tasks/{task_id}", response_model=TaskRead)
async def update_task(task_id: int, payload: TaskUpdate, db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    task = await TaskService.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if payload.status and payload.status not in {"pending", "completed"}:
        raise HTTPException(status_code=400, detail="status must be pending or completed")

    updated = await TaskService.update_task_fields(
        db,
        task,
        content=payload.content,
        remarks=payload.remarks,
        trigger_time=payload.trigger_time,
        cron_expr=payload.cron_expr,
        status=payload.status,
        channel_ids=payload.channel_ids,
    )
    if updated.status == "pending":
        scheduler_service.schedule_task(updated)
    else:
        scheduler_service.remove_task_job(updated.id)
    return _present(updated)


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    return await SettingsService.get_all(db)


@router.put("/settings")
async def update_settings(payload: AppSettingsPayload, db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    if values.get("backup_cron") and not croniter.is_valid(values["backup_cron"]):
        raise HTTPException(status_code=400, detail="备份时间格式无效，请使用 5 段 Cron，例如 0 3 * * *")
    saved = await SettingsService.set_many(db, values)
    await scheduler_service.reload_backup_job()
    return saved


@router.get("/channels")
async def list_channels(db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    channels = await ChannelService.list_channels(db)
    return [ChannelService.present(c) for c in channels]


@router.post("/channels")
async def create_channel(payload: ChannelPayload, db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    if payload.channel_type not in {"telegram", "feishu"}:
        raise HTTPException(status_code=400, detail="channel_type 只能是 telegram 或 feishu")
    channel = await ChannelService.create_channel(
        db,
        name=payload.name,
        channel_type=payload.channel_type,
        config=payload.config,
        enabled=payload.enabled,
    )
    return ChannelService.present(channel)


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    payload: ChannelPayload,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_basic_auth),
):
    channel = await ChannelService.get(db, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    channel = await ChannelService.update_channel(
        db,
        channel,
        name=payload.name,
        channel_type=payload.channel_type,
        config=payload.config,
        enabled=payload.enabled,
    )
    return ChannelService.present(channel)


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int, db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    channel = await ChannelService.get(db, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    await ChannelService.delete_channel(db, channel)
    return {"ok": True}


@router.get("/backup/export")
async def export_backup(db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    payload = await backup_service.build_backup(db)
    return JSONResponse(
        payload,
        headers={"Content-Disposition": "attachment; filename=smart-notifier-backup.json"},
    )


@router.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...), db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
        result = await backup_service.restore_backup(db, payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"恢复失败：{exc}") from exc
    await scheduler_service.reload_pending_tasks()
    return {"ok": True, "result": result}


@router.post("/backup/send-now")
async def send_backup_now(db: AsyncSession = Depends(get_db), _: str = Depends(verify_basic_auth)):
    ok = await backup_service.send_email_backup(db)
    if not ok:
        raise HTTPException(status_code=400, detail="请先完整配置 SMTP 和备份收件邮箱")
    return {"ok": True}
