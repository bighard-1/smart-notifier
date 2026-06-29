import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_channel import NotificationChannel


class ChannelService:
    @staticmethod
    def parse_config(channel: NotificationChannel) -> dict:
        try:
            data = json.loads(channel.config_json or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def present(channel: NotificationChannel) -> dict:
        return {
            "id": channel.id,
            "name": channel.name,
            "channel_type": channel.channel_type,
            "config": ChannelService.parse_config(channel),
            "enabled": channel.enabled,
            "created_at": channel.created_at,
        }

    @staticmethod
    async def list_channels(db: AsyncSession, enabled_only: bool = False) -> list[NotificationChannel]:
        stmt = select(NotificationChannel).order_by(NotificationChannel.created_at.desc())
        if enabled_only:
            stmt = stmt.where(NotificationChannel.enabled.is_(True))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get(db: AsyncSession, channel_id: int) -> NotificationChannel | None:
        return await db.get(NotificationChannel, channel_id)

    @staticmethod
    async def create_channel(db: AsyncSession, *, name: str, channel_type: str, config: dict, enabled: bool = True) -> NotificationChannel:
        channel = NotificationChannel(
            name=name.strip(),
            channel_type=channel_type.strip(),
            config_json=json.dumps(config, ensure_ascii=False),
            enabled=enabled,
        )
        db.add(channel)
        await db.commit()
        await db.refresh(channel)
        return channel

    @staticmethod
    async def update_channel(
        db: AsyncSession,
        channel: NotificationChannel,
        *,
        name: str | None = None,
        channel_type: str | None = None,
        config: dict | None = None,
        enabled: bool | None = None,
    ) -> NotificationChannel:
        if name is not None:
            channel.name = name.strip()
        if channel_type is not None:
            channel.channel_type = channel_type.strip()
        if config is not None:
            channel.config_json = json.dumps(config, ensure_ascii=False)
        if enabled is not None:
            channel.enabled = enabled
        await db.commit()
        await db.refresh(channel)
        return channel

    @staticmethod
    async def delete_channel(db: AsyncSession, channel: NotificationChannel) -> None:
        await db.delete(channel)
        await db.commit()
