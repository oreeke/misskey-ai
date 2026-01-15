from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..shared.constants import ConfigKeys
from ..shared.utils import extract_user_handle, extract_user_id, extract_username
from .autopost import AutoPostService

if TYPE_CHECKING:
    from .core import MisskeyBot


@dataclass(slots=True)
class MentionContext:
    mention_id: str | None
    reply_target_id: str | None
    text: str
    user_id: str | None
    username: str | None


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


class MentionHandler:
    def __init__(self, bot: MisskeyBot):
        self.bot = bot

    def _is_self_mention(self, mention: MentionContext) -> bool:
        if (
            self.bot.bot_user_id
            and mention.user_id
            and mention.user_id == self.bot.bot_user_id
        ):
            return True
        if not (self.bot.bot_username and mention.username):
            return False
        return mention.username == self.bot.bot_username or mention.username.startswith(
            f"{self.bot.bot_username}@"
        )

    @staticmethod
    def _format_mention_reply(mention: MentionContext, text: str) -> str:
        return f"@{mention.username}\n{text}" if mention.username else text

    async def _send_mention_reply(self, mention: MentionContext, text: str) -> None:
        await self.bot.misskey.create_note(
            text=self._format_mention_reply(mention, text),
            reply_id=mention.reply_target_id,
        )

    async def _maybe_send_blocked_reply(self, mention: MentionContext) -> bool:
        if not mention.user_id:
            return False
        blocked = await self.bot.get_response_block_reply(
            user_id=mention.user_id, handle=mention.username
        )
        if not blocked:
            return False
        await self._send_mention_reply(mention, blocked)
        await self.bot.record_response(mention.user_id, count_turn=False)
        return True

    def _should_handle_note(
        self,
        *,
        note_type: str | None,
        is_reply_event: bool,
        reply_to_bot: bool,
        text: str,
        note_data: dict[str, Any],
    ) -> bool:
        if note_type == "mention" and reply_to_bot:
            return False
        if is_reply_event:
            return reply_to_bot
        return self._is_bot_mentioned(text) or self._mentions_bot(note_data)

    @staticmethod
    def _effective_text(note_data: Any) -> str:
        if not isinstance(note_data, dict):
            return ""
        parts: list[str] = []
        for k in ("cw", "text"):
            v = note_data.get(k)
            if isinstance(v, str) and (s := v.strip()):
                parts.append(s)
        return "\n\n".join(parts).strip()

    @staticmethod
    def _note_payload(note: dict[str, Any]) -> dict[str, Any] | None:
        payload = note.get("note")
        return payload if isinstance(payload, dict) else None

    def _is_reply_to_bot(self, note_data: dict[str, Any]) -> bool:
        replied = note_data.get("reply")
        if not isinstance(replied, dict):
            return False
        replied_user_id = extract_user_id(replied)
        if self.bot.bot_user_id and replied_user_id == self.bot.bot_user_id:
            return True
        if not self.bot.bot_username:
            return False
        replied_user = replied.get("user")
        return (
            isinstance(replied_user, dict)
            and replied_user.get("username") == self.bot.bot_username
        )

    def _parse_reply_text(self, note_data: dict[str, Any]) -> str:
        parts: list[str] = []
        if t := self._effective_text(note_data.get("reply")):
            parts.append(t)
        if t := self._effective_text(note_data):
            parts.append(t)
        return "\n\n".join(parts).strip()

    async def _build_mention_prompt(
        self, mention: MentionContext, note: dict[str, Any]
    ) -> str:
        note_data = self._note_payload(note)
        base = mention.text.strip()
        if not note_data:
            return base
        quoted_text = ""
        quoted = note_data.get("renote")
        if isinstance(quoted, dict):
            quoted_text = self._effective_text(quoted)
        elif isinstance((quoted_id := note_data.get("renoteId")), str) and quoted_id:
            try:
                quoted_note = await self.bot.misskey.get_note(quoted_id)
            except Exception as e:
                logger.debug(f"Failed to fetch quoted note: {quoted_id} - {e}")
            else:
                quoted_text = self._effective_text(quoted_note)
        if not quoted_text:
            return base
        if base:
            return f"{base}\n\nQuote:\n{quoted_text}".strip()
        return f"Quote:\n{quoted_text}".strip()

    async def handle(self, note: dict[str, Any]) -> None:
        if not self.bot.config.get(ConfigKeys.BOT_RESPONSE_MENTION):
            return
        mention = self._parse(note)
        if not mention.mention_id or self._is_self_mention(mention):
            return
        if mention.user_id and self.bot.is_response_blacklisted_user(
            user_id=mention.user_id, handle=mention.username
        ):
            return
        try:
            async with self.bot.lock_actor(mention.user_id, mention.username):
                display = mention.username or "unknown"
                logger.info(
                    f"Mention received from @{display}: {self.bot.format_log_text(mention.text)}"
                )
                if await self._maybe_send_blocked_reply(mention):
                    return
                if await self._try_plugin_response(mention, note):
                    return
                await self._generate_ai_response(mention, note)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error handling mention: {e}")

    def _parse(self, note: dict[str, Any]) -> MentionContext:
        try:
            if self.bot.config.get(ConfigKeys.LOG_DUMP_EVENTS):
                logger.opt(lazy=True).debug(
                    "Mention data: {}",
                    lambda: json.dumps(note, ensure_ascii=False, indent=2),
                )
            note_data = self._note_payload(note)
            if not note_data:
                return MentionContext(None, None, "", None, None)
            note_type = note.get("type")
            is_reply_event = note_type == "reply"
            note_id = (
                note_data.get("id") if isinstance(note_data.get("id"), str) else None
            )
            reply_target_id = note_id
            user_id = extract_user_id(note_data)
            username = extract_user_handle(note_data)
            if is_reply_event:
                text = self._parse_reply_text(note_data)
            else:
                text = self._effective_text(note_data)
            reply_to_bot = self._is_reply_to_bot(note_data)
            should_handle = self._should_handle_note(
                note_type=note_type,
                is_reply_event=is_reply_event,
                reply_to_bot=reply_to_bot,
                text=text,
                note_data=note_data,
            )
            if not should_handle:
                if not is_reply_event and not (note_type == "mention" and reply_to_bot):
                    display = username or extract_username(note_data)
                    logger.debug(
                        f"Mention from @{display} does not mention the bot; skipping"
                    )
                note_id = None
            return MentionContext(note_id, reply_target_id, text, user_id, username)
        except Exception as e:
            logger.error(f"Failed to parse message data: {e}")
            return MentionContext(None, None, "", None, None)

    def _mentions_bot(self, note_data: dict[str, Any]) -> bool:
        mentions = note_data.get("mentions")
        if not self.bot.bot_user_id or not isinstance(mentions, list):
            return False
        return self.bot.bot_user_id in mentions

    def _is_bot_mentioned(self, text: str) -> bool:
        return bool(
            text and self.bot.bot_username and f"@{self.bot.bot_username}" in text
        )

    async def _try_plugin_response(
        self, mention: MentionContext, note: dict[str, Any]
    ) -> bool:
        plugin_results = await self.bot.plugin_manager.on_mention(note)
        for result in plugin_results:
            if not (result and result.get("handled")):
                continue
            await self._apply_plugin_result(result, mention)
            return True
        return False

    async def _apply_plugin_result(
        self, result: dict[str, Any], mention: MentionContext
    ) -> None:
        logger.debug(f"Mention handled by plugin: {result.get('plugin_name')}")
        response = result.get("response")
        if response:
            formatted = self._format_mention_reply(mention, response)
            await self.bot.misskey.create_note(
                text=formatted, reply_id=mention.reply_target_id
            )
            logger.info(
                f"Plugin replied to @{mention.username or 'unknown'}: {self.bot.format_log_text(formatted)}"
            )
            if mention.user_id:
                await self.bot.record_response(mention.user_id, count_turn=True)

    async def _generate_ai_response(
        self, mention: MentionContext, note: dict[str, Any]
    ) -> None:
        prompt = await self._build_mention_prompt(mention, note)
        reply = await self.bot.openai.generate_text(
            prompt, self.bot.system_prompt, **self.bot.ai_config
        )
        logger.debug("Mention reply generated")
        formatted = f"@{mention.username}\n{reply}" if mention.username else reply
        await self.bot.misskey.create_note(
            text=formatted, reply_id=mention.reply_target_id
        )
        logger.info(
            f"Replied to @{mention.username or 'unknown'}: {self.bot.format_log_text(formatted)}"
        )
        if mention.user_id:
            await self.bot.record_response(mention.user_id, count_turn=True)


class ChatHandler:
    def __init__(self, bot: MisskeyBot):
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
        if not (result and result.get("handled")):
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
            limit = limit or self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
            if room_id:
                return await self._get_room_chat_history(room_id, limit)
            if user_id:
                return await self._get_user_chat_history(user_id, limit)
            return []
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error getting chat history: {e}")
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


class NotificationHandler:
    def __init__(self, bot: MisskeyBot):
        self.bot = bot

    async def handle(self, notification: dict[str, Any]) -> None:
        if self.bot.config.get(ConfigKeys.LOG_DUMP_EVENTS):
            logger.opt(lazy=True).debug(
                "Notification data: {}",
                lambda: json.dumps(notification, ensure_ascii=False, indent=2),
            )
        try:
            await self.bot.plugin_manager.on_notification(notification)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error handling notification event: {e}")


class BotHandlers:
    def __init__(self, bot: MisskeyBot):
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
