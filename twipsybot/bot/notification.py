import asyncio
import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..shared.config_keys import ConfigKeys

if TYPE_CHECKING:
    from .core import MisskeyBot


class NotificationHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(self, notification: dict[str, Any]) -> None:
        if self.bot.config.get(ConfigKeys.LOG_DUMP_EVENTS):
            logger.opt(lazy=True).debug(
                "Notification data: {}",
                lambda: json.dumps(notification, ensure_ascii=False, indent=2),
            )
        try:
            await self.bot.plugin_manager.on_notification(notification)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error handling notification event")
