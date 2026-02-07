import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..shared.config_keys import ConfigKeys
from ..shared.utils import extract_user_handle, extract_user_id, extract_username

if TYPE_CHECKING:
    from .core import MisskeyBot


@dataclass(slots=True)
class _ChatContext:
    text: str
    user_id: str
    username: str
    handle: str | None
    mention_to: str | None
    room_id: str | None
    room_name: str | None
    has_media: bool
    conversation_id: str
    actor_id: str
    room_label: str | None


class ChatHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    def _is_bot_mentioned(self, text: str) -> bool:
        return bool(
            text and self.bot.bot_username and f"@{self.bot.bot_username}" in text
        )

    async def handle(self, message: dict[str, Any]) -> None:
        if not self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT):
            return
        if not message.get("id"):
            logger.debug("Missing id; skipping")
            return
        if self.bot.bot_user_id and extract_user_id(message) == self.bot.bot_user_id:
            return
        if self.bot.config.get(ConfigKeys.LOG_DUMP_EVENTS):
            logger.opt(lazy=True).debug(
                "Chat data: {}",
                lambda: json.dumps(message, ensure_ascii=False, indent=2),
            )
        try:
            await self._process(message)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error handling chat: {e}")

    @staticmethod
    def _parse_room(message: dict[str, Any]) -> tuple[str | None, str | None]:
        to_room = message.get("toRoom")
        room_id = message.get("toRoomId")
        room_name = None
        if isinstance(to_room, dict):
            if not room_id:
                room_id = to_room.get("id")
            room_name = to_room.get("name")
        room_id = room_id if isinstance(room_id, str) and room_id else None
        room_name = room_name if isinstance(room_name, str) and room_name else None
        return room_id, room_name

    def _log_incoming_chat(
        self,
        *,
        username: str,
        text: str,
        has_media: bool,
        room_label: str | None,
    ) -> None:
        prefix = f"Room {room_label} " if room_label else ""
        if text:
            logger.info(
                f"Chat received from {prefix}@{username}: {self.bot.format_log_text(text)}"
            )
            return
        if has_media:
            logger.info(f"Chat received from {prefix}@{username}: (no text; has media)")

    async def _process(self, message: dict[str, Any]) -> None:
        ctx = self._parse_chat_context(message)
        if not ctx:
            return
        if self.bot.is_response_blacklisted_user(
            user_id=ctx.user_id, handle=ctx.handle or ctx.mention_to
        ):
            return
        async with self.bot.lock_actor(ctx.actor_id, ctx.username):
            self._log_incoming_chat(
                username=ctx.username,
                text=ctx.text,
                has_media=ctx.has_media,
                room_label=ctx.room_label,
            )
            if await self._maybe_send_blocked_reply(ctx):
                return
            if await self._try_plugin_response(
                message,
                ctx.conversation_id,
                ctx.user_id,
                ctx.username,
                ctx.mention_to,
                ctx.room_id,
            ):
                return
            if not ctx.text:
                return
            await self._generate_ai_response(
                ctx.conversation_id,
                ctx.user_id,
                ctx.username,
                ctx.mention_to,
                ctx.text,
                ctx.room_id,
            )

    def _parse_chat_context(self, message: dict[str, Any]) -> _ChatContext | None:
        text = message.get("text") or message.get("content") or message.get("body", "")
        user_id = extract_user_id(message)
        if not isinstance(user_id, str) or not user_id:
            logger.debug("Chat missing required info: user_id is empty")
            return None
        username = extract_username(message)
        handle = extract_user_handle(message)
        mention_to = handle or (username if username != "unknown" else None)
        room_id, room_name = self._parse_room(message)
        has_media = bool(message.get("fileId") or message.get("file"))
        if not text and not has_media:
            logger.debug("Chat missing required info: empty text and no media")
            return None
        if room_id and not self._is_bot_mentioned(text):
            logger.debug(
                f"Room chat from @{username} does not mention the bot; skipping"
            )
            return None
        conversation_id = f"room:{room_id}" if room_id else user_id
        actor_id = room_id or user_id
        room_label = room_name or room_id
        return _ChatContext(
            text=str(text or ""),
            user_id=user_id,
            username=username,
            handle=handle,
            mention_to=mention_to,
            room_id=room_id,
            room_name=room_name,
            has_media=has_media,
            conversation_id=conversation_id,
            actor_id=actor_id,
            room_label=room_label,
        )

    async def _maybe_send_blocked_reply(self, ctx: _ChatContext) -> bool:
        blocked = await self.bot.get_response_block_reply(
            user_id=ctx.user_id,
            handle=ctx.handle or ctx.mention_to,
        )
        if not blocked:
            return False
        await self._send_chat_reply(
            user_id=ctx.user_id,
            room_id=ctx.room_id,
            text=blocked,
            mention_to=ctx.mention_to,
        )
        await self.bot.record_response(ctx.user_id, count_turn=False)
        return True

    async def _send_chat_reply(
        self, *, user_id: str, room_id: str | None, text: str, mention_to: str | None
    ) -> None:
        if room_id and mention_to:
            mention = mention_to if mention_to.startswith("@") else f"@{mention_to}"
            stripped = text.lstrip()
            if not stripped.startswith(mention):
                text = f"{mention}\n{text}"
        if room_id:
            await self.bot.misskey.send_room_message(room_id, text)
        else:
            await self.bot.misskey.send_message(user_id, text)

    async def _try_plugin_response(
        self,
        message: dict[str, Any],
        conversation_id: str,
        user_id: str,
        username: str,
        mention_to: str | None,
        room_id: str | None,
    ) -> bool:
        plugin_results = await self.bot.plugin_manager.on_message(message)
        for result in plugin_results:
            if await self._apply_plugin_result(
                result,
                message=message,
                conversation_id=conversation_id,
                user_id=user_id,
                username=username,
                mention_to=mention_to,
                room_id=room_id,
            ):
                return True
        return False

    async def _apply_plugin_result(
        self,
        result: Any,
        *,
        message: dict[str, Any],
        conversation_id: str,
        user_id: str,
        username: str,
        mention_to: str | None,
        room_id: str | None,
    ) -> bool:
        if not (isinstance(result, dict) and result.get("handled")):
            return False
        logger.debug(f"Chat handled by plugin: {result.get('plugin_name')}")
        response = result.get("response")
        if not response:
            return True
        await self._send_chat_reply(
            user_id=user_id, room_id=room_id, text=response, mention_to=mention_to
        )
        logger.info(
            f"Plugin replied to @{username}: {self.bot.format_log_text(response)}"
        )
        await self.bot.record_response(user_id, count_turn=True)
        user_text = message.get("text") or message.get("content") or ""
        if user_text:
            user_content = f"{username}: {user_text}" if room_id else user_text
            self.bot.append_chat_turn(
                conversation_id,
                user_content,
                response,
                self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY),
            )
        return True

    async def _generate_ai_response(
        self,
        conversation_id: str,
        user_id: str,
        username: str,
        mention_to: str | None,
        text: str,
        room_id: str | None,
    ) -> None:
        limit = self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        history = await self.bot.get_or_load_chat_history(
            conversation_id, limit=limit, user_id=user_id, room_id=room_id
        )
        messages: list[dict[str, str]] = []
        if self.bot.system_prompt:
            messages.append({"role": "system", "content": self.bot.system_prompt})
        messages.extend(history)
        user_content = f"{username}: {text}" if room_id else text
        last = next(reversed(history), None)
        if not (
            isinstance(last, dict)
            and last.get("role") == "user"
            and last.get("content") == user_content
        ):
            messages.append({"role": "user", "content": user_content})
        reply = await self.bot.openai.generate_chat(messages, **self.bot.ai_config)
        logger.debug("Chat reply generated")
        await self._send_chat_reply(
            user_id=user_id, room_id=room_id, text=reply, mention_to=mention_to
        )
        logger.info(f"Replied to @{username}: {self.bot.format_log_text(reply)}")
        await self.bot.record_response(user_id, count_turn=True)
        self.bot.append_chat_turn(conversation_id, user_content, reply, limit)

    async def get_chat_history(
        self,
        *,
        user_id: str | None = None,
        room_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, str]]:
        try:
            config_limit = self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
            limit_value = limit if isinstance(limit, int) else config_limit
            if not isinstance(limit_value, int):
                limit_value = 0
            if room_id:
                return await self._get_room_chat_history(room_id, limit_value)
            if user_id:
                return await self._get_user_chat_history(user_id, limit_value)
            return []
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.exception("Error getting chat history")
            return []

    async def _get_room_chat_history(
        self, room_id: str, limit: int
    ) -> list[dict[str, str]]:
        messages = await self.bot.misskey.get_room_messages(room_id, limit=limit)
        bot_user_id = self.bot.bot_user_id
        history: list[dict[str, str]] = []
        for msg in reversed(messages):
            sender_id = extract_user_id(msg)
            is_assistant = bool(
                bot_user_id and isinstance(sender_id, str) and sender_id == bot_user_id
            )
            content = msg.get("text") or msg.get("content") or msg.get("body", "")
            if not is_assistant:
                content = f"{extract_username(msg)}: {content}"
            history.append(
                {"role": "assistant" if is_assistant else "user", "content": content}
            )
        return history

    async def _get_user_chat_history(
        self, user_id: str, limit: int
    ) -> list[dict[str, str]]:
        messages = await self.bot.misskey.get_messages(user_id, limit=limit)
        history: list[dict[str, str]] = []
        for msg in reversed(messages):
            role = "user" if extract_user_id(msg) == user_id else "assistant"
            content = msg.get("text") or msg.get("content") or msg.get("body", "")
            history.append({"role": role, "content": content})
        return history
