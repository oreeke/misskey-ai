from typing import TYPE_CHECKING, Any

from .chat import ChatHandler
from .mention import MentionHandler
from .notification import NotificationHandler
from .post import AutoPostService

if TYPE_CHECKING:
    from .core import MisskeyBot


class BotHandlers:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot
        self.mention = MentionHandler(bot)
        self.chat = ChatHandler(bot)
        self.notification = NotificationHandler(bot)
        self.auto_post = AutoPostService(bot)

    async def on_mention(self, note: dict[str, Any]) -> None:
        await self.mention.handle(note)

    async def on_message(self, message: dict[str, Any]) -> None:
        await self.chat.handle(message)

    async def on_notification(self, notification: dict[str, Any]) -> None:
        await self.notification.handle(notification)

    async def on_timeline_note(self, note: dict[str, Any]) -> None:
        await self.bot.plugin_manager.on_timeline_note(note)

    async def on_auto_post(self) -> None:
        await self.auto_post.run()
