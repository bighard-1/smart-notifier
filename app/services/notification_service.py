import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from app.db.session import AsyncSessionLocal
from app.models.notification_channel import NotificationChannel
from app.models.task import Task
from app.services.channel_service import ChannelService


class NotificationService:
    def __init__(self) -> None:
        self.bot_app: Application | None = None

    def bind_bot(self, bot_app: Application) -> None:
        self.bot_app = bot_app

    @staticmethod
    def task_channel_ids(task: Task) -> list[int]:
        if not task.channel_ids:
            return []
        try:
            data = json.loads(task.channel_ids)
            return [int(item) for item in data if str(item).isdigit()]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    async def send_task_reminder(self, task: Task) -> list[dict]:
        message = f"提醒: {task.content}\n备注: {task.remarks or '无'}"
        channel_ids = self.task_channel_ids(task)
        results: list[dict] = []

        async with AsyncSessionLocal() as db:
            channels = await ChannelService.list_channels(db, enabled_only=True)
        selected = [c for c in channels if c.id in channel_ids]

        if not selected:
            ok = await self._send_telegram(task.chat_id, message, task.id, with_actions=True)
            return [{"channel": "telegram_default", "ok": ok}]

        for channel in selected:
            if channel.channel_type == "telegram":
                cfg = ChannelService.parse_config(channel)
                chat_id = str(cfg.get("chat_id") or task.chat_id)
                ok = await self._send_telegram(chat_id, message, task.id, with_actions=True)
                results.append({"channel": channel.name, "ok": ok})
            elif channel.channel_type == "feishu":
                ok = await self._send_feishu(channel, message)
                results.append({"channel": channel.name, "ok": ok})
            else:
                results.append({"channel": channel.name, "ok": False, "error": "unsupported channel"})
        return results

    async def _send_telegram(self, chat_id: str, message: str, task_id: int, *, with_actions: bool) -> bool:
        if self.bot_app is None:
            return False
        keyboard = None
        if with_actions:
            keyboard = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ 已完成 (Done)", callback_data=f"done_{task_id}"),
                    InlineKeyboardButton("⏳ 稍后提醒 (Snooze)", callback_data=f"snooze_{task_id}"),
                ]]
            )
        await self.bot_app.bot.send_message(chat_id=chat_id, text=message, reply_markup=keyboard)
        return True

    async def _send_feishu(self, channel: NotificationChannel, message: str) -> bool:
        cfg = ChannelService.parse_config(channel)
        webhook_url = str(cfg.get("webhook_url") or "")
        if not webhook_url:
            return False

        payload: dict = {"msg_type": "text", "content": {"text": message}}
        secret = str(cfg.get("secret") or "")
        if secret:
            timestamp = str(int(time.time()))
            sign_text = f"{timestamp}\n{secret}".encode("utf-8")
            sign = base64.b64encode(hmac.new(sign_text, b"", digestmod=hashlib.sha256).digest()).decode("utf-8")
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        def post() -> bool:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=12) as resp:
                return 200 <= resp.status < 300

        return await asyncio.to_thread(post)


notification_service = NotificationService()
