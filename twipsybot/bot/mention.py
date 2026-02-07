import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from ..plugin.base import PluginHookResult
from ..shared.config_keys import ConfigKeys
from ..shared.utils import extract_user_handle, extract_user_id, extract_username

if TYPE_CHECKING:
    from ..plugin.base import PluginHookResult
    from .core import MisskeyBot


@dataclass(slots=True)
class MentionContext:
    mention_id: str | None
    reply_target_id: str | None
    text: str
    user_id: str | None
    username: str | None


class MentionHandler:
    def __init__(self, bot: "MisskeyBot"):
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
            logger.exception("Error handling mention")

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
        except Exception:
            logger.exception("Failed to parse message data")
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
            if not (isinstance(result, dict) and result.get("handled")):
                continue
            await self._apply_plugin_result(cast(PluginHookResult, result), mention)
            return True
        return False

    async def _apply_plugin_result(
        self, result: PluginHookResult, mention: MentionContext
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
