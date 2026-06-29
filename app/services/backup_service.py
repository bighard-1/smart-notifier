import asyncio
import json
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_setting import AppSetting
from app.models.notification_channel import NotificationChannel
from app.models.task import Task
from app.services.channel_service import ChannelService
from app.services.settings_service import SettingsService
from app.services.task_service import TaskService


ENV_KEYS = [
    "APP_NAME",
    "APP_HOST",
    "APP_PORT",
    "APP_DEBUG",
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_POLL_INTERVAL",
    "SCHEDULER_TIMEZONE",
    "WEB_USERNAME",
    "WEB_PASSWORD",
]


class BackupService:
    @staticmethod
    async def build_backup(db: AsyncSession) -> dict:
        channels = await ChannelService.list_channels(db)
        settings_values = await SettingsService.get_all(db)
        return {
            "version": 2,
            "created_at": datetime.utcnow().isoformat(),
            "tasks": await TaskService.dump_all_tasks(db),
            "notification_channels": [ChannelService.present(c) for c in channels],
            "app_settings": settings_values,
            "environment": {key: os.getenv(key, "") for key in ENV_KEYS},
        }

    @staticmethod
    async def restore_backup(db: AsyncSession, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("备份文件格式错误")

        tasks = payload.get("tasks", [])
        channels = payload.get("notification_channels", [])
        app_settings = payload.get("app_settings", {})
        environment = payload.get("environment", {})

        await db.execute(delete(Task))
        await db.execute(delete(NotificationChannel))
        await db.execute(delete(AppSetting))
        await db.commit()

        inserted_tasks, updated_tasks = await TaskService.restore_from_dump(db, tasks)
        await db.execute(text("SELECT setval(pg_get_serial_sequence('tasks', 'id'), COALESCE((SELECT MAX(id) FROM tasks), 1), true)"))
        await db.commit()

        channel_count = 0
        for row in channels:
            config = row.get("config") or {}
            item = NotificationChannel(
                id=row.get("id"),
                name=row.get("name", "未命名渠道"),
                channel_type=row.get("channel_type", "telegram"),
                config_json=json.dumps(config, ensure_ascii=False),
                enabled=bool(row.get("enabled", True)),
                created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else datetime.utcnow(),
            )
            db.add(item)
            channel_count += 1
        await db.commit()
        await db.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('notification_channels', 'id'), "
                "COALESCE((SELECT MAX(id) FROM notification_channels), 1), true)"
            )
        )
        await db.commit()

        merged_settings = {}
        if isinstance(environment, dict):
            env_to_setting = {
                "APP_NAME": "app_name",
                "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
                "SCHEDULER_TIMEZONE": "scheduler_timezone",
                "WEB_USERNAME": "web_username",
                "WEB_PASSWORD": "web_password",
            }
            merged_settings.update({env_to_setting[k]: v for k, v in environment.items() if k in env_to_setting and v})
        if isinstance(app_settings, dict):
            merged_settings.update(app_settings)
        await SettingsService.set_many(db, merged_settings)

        return {
            "tasks_inserted": inserted_tasks,
            "tasks_updated": updated_tasks,
            "channels_restored": channel_count,
            "settings_restored": len(merged_settings),
        }

    @staticmethod
    async def send_email_backup(db: AsyncSession) -> bool:
        payload = await BackupService.build_backup(db)
        settings_values = await SettingsService.get_all(db)
        to_email = settings_values.get("backup_email_to", "")
        smtp_host = settings_values.get("smtp_host", "")
        smtp_from = settings_values.get("smtp_from_email") or settings_values.get("smtp_username", "")
        if not to_email or not smtp_host or not smtp_from:
            return False

        attachment = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        msg = EmailMessage()
        msg["Subject"] = f"Smart Notifier 全量备份 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        msg["From"] = smtp_from
        msg["To"] = to_email
        msg.set_content("附件为 Smart Notifier 全量备份，可在 Web 页面覆盖式恢复。")
        msg.add_attachment(attachment, maintype="application", subtype="json", filename="smart-notifier-backup.json")

        def send() -> bool:
            port = int(settings_values.get("smtp_port") or 587)
            use_tls = settings_values.get("smtp_use_tls", "true").lower() == "true"
            username = settings_values.get("smtp_username", "")
            password = settings_values.get("smtp_password", "")
            with smtplib.SMTP(smtp_host, port, timeout=20) as smtp:
                if use_tls:
                    smtp.starttls()
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
            return True

        return await asyncio.to_thread(send)


backup_service = BackupService()
