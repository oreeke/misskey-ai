import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cachetools import TTLCache
from loguru import logger

from .config import Config
from .constants import (
    CHAT_CACHE_MAX_USERS,
    CHAT_CACHE_TTL,
    ConfigKeys,
    USER_LOCK_CACHE_MAX,
    USER_LOCK_TTL,
)
from .exceptions import (
    ConfigurationError,
)
from .misskey_api import MisskeyAPI
from .openai_api import OpenAIAPI
from .persistence import PersistenceManager
from .plugin import PluginManager
from .runtime import BotRuntime
from .streaming import ChannelSpec, ChannelType, StreamingClient
from .transport import ClientSession
from .utils import extract_user_id, extract_username, get_memory_usage

__all__ = ("MisskeyBot",)


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

    async def handle(self, note: dict[str, Any]) -> None:
        if not self.bot.config.get(ConfigKeys.BOT_RESPONSE_MENTION_ENABLED):
            return
        mention = self._parse(note)
        if not mention.mention_id:
            return
        try:
            async with self.bot.lock_actor(mention.user_id, mention.username):
                logger.info(
                    f"Mention received from @{mention.username}: {self.bot.format_log_text(mention.text)}"
                )
                if await self._try_plugin_response(mention, note):
                    return
                await self._generate_ai_response(mention)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error handling mention: {e}")

    def _parse(self, note: dict[str, Any]) -> MentionContext:
        try:
            is_reply_event = note.get("type") == "reply" and "note" in note
            if self.bot.config.get(ConfigKeys.LOG_DUMP_EVENTS):
                logger.opt(lazy=True).debug(
                    "Mention data: {}",
                    lambda: json.dumps(note, ensure_ascii=False, indent=2),
                )
            mention_id = note.get("id")
            reply_target_id = note.get("note", {}).get("id")
            if is_reply_event:
                note_data = note["note"]
                text_parts: list[str] = []
                if t := self._effective_text(note_data.get("reply")):
                    text_parts.append(t)
                if t := self._effective_text(note_data):
                    text_parts.append(t)
                text = "\n\n".join(text_parts).strip()
                user_id = note_data.get("userId")
                username = note_data.get("user", {}).get("username")
            else:
                text = self._effective_text(note.get("note"))
                user_id = extract_user_id(note)
                username = extract_username(note)
            if not self._is_bot_mentioned(text):
                logger.debug(
                    f"Reply from @{username} does not mention the bot; skipping"
                )
                mention_id = None
            return MentionContext(mention_id, reply_target_id, text, user_id, username)
        except Exception as e:
            logger.error(f"Failed to parse message data: {e}")
            return MentionContext(None, None, "", None, None)

    def _is_bot_mentioned(self, text: str) -> bool:
        return bool(
            text and self.bot.bot_username and f"@{self.bot.bot_username}" in text
        )

    async def _try_plugin_response(
        self, mention: MentionContext, note: dict[str, Any]
    ) -> bool:
        plugin_results = await self.bot.plugin_manager.on_mention(note)
        for result in plugin_results:
            if result and result.get("handled"):
                logger.debug(f"Mention handled by plugin: {result.get('plugin_name')}")
                response = result.get("response")
                if response:
                    formatted = f"@{mention.username}\n{response}"
                    await self.bot.misskey.create_note(
                        text=formatted, reply_id=mention.reply_target_id
                    )
                    logger.info(
                        f"Plugin replied to @{mention.username}: {self.bot.format_log_text(formatted)}"
                    )
                return True
        return False

    async def _generate_ai_response(self, mention: MentionContext) -> None:
        reply = await self.bot.openai.generate_text(
            mention.text, self.bot.system_prompt, **self.bot.ai_config
        )
        logger.debug("Mention reply generated")
        formatted = f"@{mention.username}\n{reply}"
        await self.bot.misskey.create_note(
            text=formatted, reply_id=mention.reply_target_id
        )
        logger.info(
            f"Replied to @{mention.username}: {self.bot.format_log_text(formatted)}"
        )


class ChatHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    def _is_bot_mentioned(self, text: str) -> bool:
        return bool(
            text and self.bot.bot_username and f"@{self.bot.bot_username}" in text
        )

    async def handle(self, message: dict[str, Any]) -> None:
        if not self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_ENABLED):
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
        text = message.get("text") or message.get("content") or message.get("body", "")
        user_id = extract_user_id(message)
        username = extract_username(message)
        room_id, room_name = self._parse_room(message)
        has_media = bool(message.get("fileId") or message.get("file"))
        if not isinstance(user_id, str) or not user_id:
            logger.debug("Chat missing required info: user_id is empty")
            return
        if not text and not has_media:
            logger.debug("Chat missing required info: empty text and no media")
            return
        if room_id and not self._is_bot_mentioned(text):
            logger.debug(
                f"Room chat from @{username} does not mention the bot; skipping"
            )
            return
        conversation_id = f"room:{room_id}" if room_id else user_id
        actor_id = room_id or user_id
        room_label = room_name or room_id
        async with self.bot.lock_actor(actor_id, username):
            self._log_incoming_chat(
                username=username, text=text, has_media=has_media, room_label=room_label
            )
            if await self._try_plugin_response(
                message, conversation_id, user_id, username, room_id
            ):
                return
            if not text:
                return
            await self._generate_ai_response(
                conversation_id, user_id, username, text, room_id
            )

    async def _send_chat_reply(
        self, *, user_id: str, room_id: str | None, text: str
    ) -> None:
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
        room_id: str | None,
    ) -> bool:
        if not (result and result.get("handled")):
            return False
        logger.debug(f"Chat handled by plugin: {result.get('plugin_name')}")
        response = result.get("response")
        if not response:
            return True
        await self._send_chat_reply(user_id=user_id, room_id=room_id, text=response)
        logger.info(
            f"Plugin replied to @{username}: {self.bot.format_log_text(response)}"
        )
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
        await self._send_chat_reply(user_id=user_id, room_id=room_id, text=reply)
        logger.info(f"Replied to @{username}: {self.bot.format_log_text(reply)}")
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


class ReactionHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(self, reaction: dict[str, Any]) -> None:
        username = extract_username(reaction)
        note_id = reaction.get("note", {}).get("id", "unknown")
        reaction_type = reaction.get("reaction", "unknown")
        logger.info(f"User @{username} reacted to note {note_id}: {reaction_type}")
        if self.bot.config.get(ConfigKeys.LOG_DUMP_EVENTS):
            logger.opt(lazy=True).debug(
                "Reaction data: {}",
                lambda: json.dumps(reaction, ensure_ascii=False, indent=2),
            )
        try:
            await self.bot.plugin_manager.on_reaction(reaction)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error handling reaction event: {e}")


class FollowHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(self, follow: dict[str, Any]) -> None:
        username = extract_username(follow)
        logger.info(f"User @{username} followed @{self.bot.bot_username}")
        if self.bot.config.get(ConfigKeys.LOG_DUMP_EVENTS):
            logger.opt(lazy=True).debug(
                "Follow data: {}",
                lambda: json.dumps(follow, ensure_ascii=False, indent=2),
            )
        try:
            await self.bot.plugin_manager.on_follow(follow)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error handling follow event: {e}")


class AutoPostService:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def run(self) -> None:
        max_posts = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY)
        local_only = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_LOCAL_ONLY)
        if not self.bot.runtime.running or not self.bot.runtime.check_post_counter(
            max_posts
        ):
            return
        try:
            plugin_results = await self.bot.plugin_manager.on_auto_post()
            if await self._try_plugin_post(plugin_results, max_posts, local_only):
                return
            await self._generate_ai_post(plugin_results, max_posts, local_only)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error during auto-post: {e}")

    async def _try_plugin_post(
        self, plugin_results: list[Any], max_posts: int, local_only: bool | None
    ) -> bool:
        for result in plugin_results:
            if result and result.get("content"):
                content = result.get("content")
                visibility = result.get(
                    "visibility",
                    self.bot.config.get(ConfigKeys.BOT_AUTO_POST_VISIBILITY),
                )
                await self.bot.misskey.create_note(
                    content, visibility=visibility, local_only=local_only
                )
                self.bot.runtime.post_count()
                logger.info(f"Auto-post succeeded: {self.bot.format_log_text(content)}")
                logger.info(
                    f"Daily post count: {self.bot.runtime.posts_today}/{max_posts}"
                )
                return True
        return False

    async def _generate_ai_post(
        self, plugin_results: list[Any], max_posts: int, local_only: bool | None
    ) -> None:
        plugin_prompt = ""
        timestamp_override = None
        for result in plugin_results:
            if result and result.get("modify_prompt"):
                if result.get("plugin_prompt"):
                    plugin_prompt = result.get("plugin_prompt")
                if result.get("timestamp"):
                    timestamp_override = result.get("timestamp")
                logger.info(
                    f"Plugin {result.get('plugin_name')} requested prompt modification: {plugin_prompt}"
                )
        post_prompt = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_PROMPT, "")
        try:
            content = await self._generate_post(
                self.bot.system_prompt, post_prompt, plugin_prompt, timestamp_override
            )
        except ValueError as e:
            logger.warning(f"Auto-post failed; skipping this run: {e}")
            return
        visibility = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_VISIBILITY)
        await self.bot.misskey.create_note(
            content, visibility=visibility, local_only=local_only
        )
        self.bot.runtime.post_count()
        logger.info(f"Auto-post succeeded: {self.bot.format_log_text(content)}")
        logger.info(f"Daily post count: {self.bot.runtime.posts_today}/{max_posts}")

    async def _generate_post(
        self,
        system_prompt: str,
        prompt: str,
        plugin_prompt: str,
        timestamp_override: int | None = None,
    ) -> str:
        if not prompt:
            raise ValueError("Missing prompt")
        timestamp_min = timestamp_override or int(
            datetime.now(timezone.utc).timestamp() // 60
        )
        full_prompt = f"[{timestamp_min}] {plugin_prompt}{prompt}"
        return await self.bot.openai.generate_text(
            full_prompt, system_prompt, **self.bot.ai_config
        )


class BotHandlers:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot
        self.mention = MentionHandler(bot)
        self.chat = ChatHandler(bot)
        self.reaction = ReactionHandler(bot)
        self.follow = FollowHandler(bot)
        self.auto_post = AutoPostService(bot)

    async def on_mention(self, note: dict[str, Any]) -> None:
        await self.mention.handle(note)

    async def on_message(self, message: dict[str, Any]) -> None:
        await self.chat.handle(message)

    async def on_reaction(self, reaction: dict[str, Any]) -> None:
        await self.reaction.handle(reaction)

    async def on_follow(self, follow: dict[str, Any]) -> None:
        await self.follow.handle(follow)

    async def on_timeline_note(self, note: dict[str, Any]) -> None:
        await self.bot.plugin_manager.on_timeline_note(note)

    async def on_auto_post(self) -> None:
        await self.auto_post.run()


class MisskeyBot:
    def __init__(self, config: Config):
        self.config = config
        try:
            instance_url = config.get_required(ConfigKeys.MISSKEY_INSTANCE_URL)
            access_token = config.get_required(ConfigKeys.MISSKEY_ACCESS_TOKEN)
            self.misskey = MisskeyAPI(instance_url, access_token)
            self.streaming = StreamingClient(
                instance_url,
                access_token,
                log_dump_events=bool(config.get(ConfigKeys.LOG_DUMP_EVENTS)),
            )
            self.openai = OpenAIAPI(
                config.get_required(ConfigKeys.OPENAI_API_KEY),
                config.get(ConfigKeys.OPENAI_MODEL),
                config.get(ConfigKeys.OPENAI_API_BASE),
                config.get(ConfigKeys.OPENAI_API_MODE),
            )
            self.scheduler = AsyncIOScheduler()
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Initialization failed: {e}")
            raise ConfigurationError() from e
        self.persistence = PersistenceManager(config.get(ConfigKeys.DB_PATH))
        self.runtime = BotRuntime(self)
        self.plugin_manager = PluginManager(
            config,
            persistence=self.persistence,
            context_objects={
                "misskey": self.misskey,
                "drive": self.misskey.drive,
                "openai": self.openai,
                "streaming": self.streaming,
                "runtime": self.runtime,
                "bot": self,
            },
        )
        self.system_prompt = config.get(ConfigKeys.BOT_SYSTEM_PROMPT, "")
        self.bot_user_id = None
        self.bot_username = None
        self._user_locks: TTLCache[str, asyncio.Lock] = TTLCache(
            maxsize=USER_LOCK_CACHE_MAX, ttl=USER_LOCK_TTL
        )
        self._chat_histories: TTLCache[str, list[dict[str, str]]] = TTLCache(
            maxsize=CHAT_CACHE_MAX_USERS, ttl=CHAT_CACHE_TTL
        )
        self.handlers = BotHandlers(self)
        self.timeline_channels = self._load_timeline_channels()
        logger.info("Bot initialized")

    @staticmethod
    def _actor_key(user_id: str | None, username: str | None) -> str | None:
        if user_id:
            return f"id:{user_id}"
        if username:
            return f"name:{username}"
        return None

    def _get_actor_lock(
        self, user_id: str | None, username: str | None
    ) -> asyncio.Lock:
        key = self._actor_key(user_id, username)
        if not key:
            return asyncio.Lock()
        if key not in self._user_locks:
            self._user_locks[key] = asyncio.Lock()
        return self._user_locks[key]

    def lock_actor(self, user_id: str | None, username: str | None):
        return self._get_actor_lock(user_id, username)

    def load_timeline_channels(self) -> set[str]:
        return self._load_timeline_channels()

    def _load_timeline_channels(self) -> set[str]:
        if not self.config.get(ConfigKeys.BOT_TIMELINE_ENABLED):
            return set()
        enabled: set[str] = set()
        if self.config.get(ConfigKeys.BOT_TIMELINE_HOME):
            enabled.add(ChannelType.HOME_TIMELINE.value)
        if self.config.get(ConfigKeys.BOT_TIMELINE_LOCAL):
            enabled.add(ChannelType.LOCAL_TIMELINE.value)
        if self.config.get(ConfigKeys.BOT_TIMELINE_HYBRID):
            enabled.add(ChannelType.HYBRID_TIMELINE.value)
        if self.config.get(ConfigKeys.BOT_TIMELINE_GLOBAL):
            enabled.add(ChannelType.GLOBAL_TIMELINE.value)
        return enabled

    def _load_antenna_selectors(self) -> list[str]:
        value = self.config.get(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS)
        if value is None or isinstance(value, bool):
            return []
        if isinstance(value, str):
            tokens = [t.strip() for t in value.replace(",", " ").split() if t.strip()]
            return list(dict.fromkeys(tokens))
        if isinstance(value, list):
            tokens = [str(v).strip() for v in value if v is not None and str(v).strip()]
            return list(dict.fromkeys(tokens))
        s = str(value).strip()
        return [s] if s else []

    @staticmethod
    def _build_antenna_index(
        antennas: list[dict[str, Any]],
    ) -> tuple[set[str], dict[str, list[str]]]:
        antenna_ids: set[str] = set()
        name_to_ids: dict[str, list[str]] = {}
        for antenna in antennas:
            if not isinstance(antenna, dict):
                continue
            antenna_id = antenna.get("id")
            if isinstance(antenna_id, str) and antenna_id:
                antenna_ids.add(antenna_id)
            name = antenna.get("name")
            if not isinstance(name, str):
                continue
            normalized_name = name.strip()
            if not normalized_name or not isinstance(antenna_id, str) or not antenna_id:
                continue
            name_to_ids.setdefault(normalized_name, []).append(antenna_id)
        return antenna_ids, name_to_ids

    @staticmethod
    def _dedupe_non_empty(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @staticmethod
    def _resolve_antenna_selector(
        selector: str, antenna_ids: set[str], name_to_ids: dict[str, list[str]]
    ) -> str:
        if selector in antenna_ids:
            return selector
        candidates = name_to_ids.get(selector)
        if not candidates:
            logger.warning(f"Antenna not found: {selector}")
            return ""
        if len(candidates) != 1:
            logger.warning(f"Antenna name is ambiguous: {selector}")
            return ""
        return candidates[0]

    async def _resolve_antenna_ids(self, selectors: list[str]) -> list[str]:
        normalized = [s.strip() for s in selectors if isinstance(s, str) and s.strip()]
        if not normalized:
            return []
        antennas = await self.misskey.list_antennas()
        antenna_ids, name_to_ids = self._build_antenna_index(antennas)
        resolved = [
            self._resolve_antenna_selector(s, antenna_ids, name_to_ids)
            for s in normalized
        ]
        return self._dedupe_non_empty(resolved)

    async def get_streaming_channels(self) -> list[ChannelSpec]:
        active = {ChannelType.MAIN.value, *self.timeline_channels}
        ordered = [
            ChannelType.MAIN.value,
            ChannelType.HOME_TIMELINE.value,
            ChannelType.LOCAL_TIMELINE.value,
            ChannelType.HYBRID_TIMELINE.value,
            ChannelType.GLOBAL_TIMELINE.value,
        ]
        result: list[ChannelSpec] = [c for c in ordered if c in active]
        selectors = self._load_antenna_selectors()
        for antenna_id in await self._resolve_antenna_ids(selectors):
            result.append((ChannelType.ANTENNA.value, {"antennaId": antenna_id}))
        return result

    async def restart_streaming(self) -> None:
        if (task := self.runtime.tasks.get("streaming")) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.streaming.disconnect()
        channels = await self.get_streaming_channels()
        await self.streaming.connect_once(channels)
        self.runtime.add_task("streaming", self.streaming.connect(channels))

    async def get_or_load_chat_history(
        self,
        conversation_id: str,
        *,
        limit: int | None,
        user_id: str | None = None,
        room_id: str | None = None,
    ) -> list[dict[str, str]]:
        limit = limit or self.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        if (cached := self._chat_histories.get(conversation_id)) is not None:
            return list(cached)[-max(0, limit * 2) :]
        if conversation_id.startswith("room:"):
            room_id = room_id or conversation_id.removeprefix("room:")
        history = await self.handlers.chat.get_chat_history(
            user_id=user_id, room_id=room_id, limit=limit
        )
        trimmed = history[-max(0, limit * 2) :]
        self._chat_histories[conversation_id] = trimmed
        return list(trimmed)

    def append_chat_turn(
        self, user_id: str, user_text: str, assistant_text: str, limit: int | None
    ) -> None:
        limit = limit or self.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        history = list(self._chat_histories.get(user_id) or [])
        last = next(reversed(history), None)
        if user_text and not (
            isinstance(last, dict)
            and last.get("role") == "user"
            and last.get("content") == user_text
        ):
            history.append({"role": "user", "content": user_text})
        last = next(reversed(history), None)
        if assistant_text and not (
            isinstance(last, dict)
            and last.get("role") == "assistant"
            and last.get("content") == assistant_text
        ):
            history.append({"role": "assistant", "content": assistant_text})
        self._chat_histories[user_id] = history[-max(0, limit * 2) :]

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False

    async def start(self) -> None:
        if self.runtime.running:
            logger.warning("Bot is already running")
            return
        logger.info("Starting services...")
        self.runtime.running = True
        await self._initialize_services()
        self._setup_scheduler()
        await self._setup_streaming()
        logger.info("Services ready; awaiting new tasks...")
        memory_usage = get_memory_usage()
        logger.debug(f"Memory usage: {memory_usage['rss_mb']} MB")

    async def _initialize_services(self) -> None:
        await self.persistence.initialize()
        self.openai.initialize()
        current_user = await self.misskey.get_current_user()
        self.bot_user_id = current_user.get("id")
        self.bot_username = current_user.get("username")
        logger.info(
            f"Connected to Misskey instance: bot_id={self.bot_user_id}, @{self.bot_username}"
        )
        await self.plugin_manager.load_plugins()
        await self.plugin_manager.on_startup()

    def _setup_scheduler(self) -> None:
        cron_jobs = [
            (self.runtime.reset_daily_counters, 0),
            (self.persistence.vacuum, 2),
        ]
        for func, hour in cron_jobs:
            self.scheduler.add_job(func, "cron", hour=hour, minute=0, second=0)
        if self.config.get(ConfigKeys.BOT_AUTO_POST_ENABLED):
            interval_minutes = self.config.get(ConfigKeys.BOT_AUTO_POST_INTERVAL)
            logger.info(f"Auto-post enabled; interval: {interval_minutes} minutes")
            self.scheduler.add_job(
                self.handlers.on_auto_post,
                "interval",
                minutes=interval_minutes,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        self.scheduler.start()

    async def _setup_streaming(self) -> None:
        try:
            self.streaming.on_mention(self.handlers.on_mention)
            self.streaming.on_message(self.handlers.on_message)
            self.streaming.on_reaction(self.handlers.on_reaction)
            self.streaming.on_follow(self.handlers.on_follow)
            self.streaming.on_note(self.handlers.on_timeline_note)
            channels = await self.get_streaming_channels()
            await self.streaming.connect_once(channels)
            self.runtime.add_task("streaming", self.streaming.connect(channels))
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.exception(f"Failed to set up Streaming connection: {e}")
            raise

    async def stop(self) -> None:
        if not self.runtime.running:
            logger.warning("Bot is already stopped")
            return
        logger.info("Stopping services...")
        self.runtime.running = False
        try:
            await self.plugin_manager.on_shutdown()
            await self.plugin_manager.cleanup_plugins()
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
            await self.runtime.cleanup_tasks()
            await self.streaming.close()
            await self.misskey.close()
            await self.openai.close()
            await ClientSession.close_session(silent=True)
            await self.persistence.close()
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.exception(f"Error stopping bot: {e}")
        finally:
            logger.info("Services stopped")

    @staticmethod
    def format_log_text(text: str, max_length: int = 50) -> str:
        if not text:
            return "None"
        suffix = "..." if len(text) > max_length else ""
        return f"{text[:max_length]}{suffix}"

    @property
    def ai_config(self) -> dict[str, Any]:
        return {
            "max_tokens": self.config.get(ConfigKeys.OPENAI_MAX_TOKENS),
            "temperature": self.config.get(ConfigKeys.OPENAI_TEMPERATURE),
        }
