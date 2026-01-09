import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cachetools import TTLCache
from loguru import logger

from ..clients.misskey_api import MisskeyAPI
from ..clients.openai_api import OpenAIAPI
from ..clients.channels import ChannelSpec, ChannelType
from ..clients.streaming import StreamingClient
from ..shared.config import Config
from ..shared.constants import (
    CHAT_CACHE_MAX_USERS,
    CHAT_CACHE_TTL,
    ConfigKeys,
    RESPONSE_LIMIT_CACHE_MAX,
    RESPONSE_LIMIT_CACHE_TTL,
    USER_LOCK_CACHE_MAX,
    USER_LOCK_TTL,
)
from ..shared.exceptions import ConfigurationError
from ..shared.utils import get_memory_usage, normalize_tokens
from ..storage.persistence import PersistenceManager
from ..plugin.manager import PluginManager
from .handlers import BotHandlers
from .runtime import BotRuntime

__all__ = ("MisskeyBot",)

_DURATION_PART_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]+)?", re.IGNORECASE)
_DURATION_UNITS: dict[str, int] = {
    "h": 3600,
    "m": 60,
    "s": 1,
}


@dataclass(slots=True)
class _ResponseLimitState:
    last_reply_ts: float | None = None
    turns: int = 0
    blocked_until_ts: float | None = None


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
        self.persistence = PersistenceManager(
            config.get(ConfigKeys.DB_PATH), config=config
        )
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
        self._response_limits: TTLCache[str, _ResponseLimitState] = TTLCache(
            maxsize=RESPONSE_LIMIT_CACHE_MAX, ttl=RESPONSE_LIMIT_CACHE_TTL
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
        return normalize_tokens(self.config.get(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS))

    @staticmethod
    def _parse_user_list(value: Any) -> set[str]:
        return set(normalize_tokens(value, lower=True))

    def _canonicalize_user_handle(self, username: str) -> str | None:
        misskey = getattr(self, "misskey", None)
        instance_url = getattr(misskey, "instance_url", None) if misskey else None
        if not isinstance(instance_url, str) or not instance_url:
            return None
        host = urlparse(instance_url).hostname
        return f"{username}@{host}" if host else None

    def _user_candidates(self, *, user_id: str, handle: str | None) -> set[str]:
        candidates = {user_id.lower()}
        if handle:
            normalized = handle.lower().lstrip("@").strip()
            if normalized:
                candidates.add(normalized)
                candidates.add(f"@{normalized}")
                if "@" not in normalized and (
                    canonical := self._canonicalize_user_handle(normalized)
                ):
                    candidates.add(canonical)
                    candidates.add(f"@{canonical}")
        return candidates

    def _load_response_whitelist_users(self) -> set[str]:
        return self._parse_user_list(self.config.get(ConfigKeys.BOT_RESPONSE_WHITELIST))

    def _load_response_blacklist_users(self) -> set[str]:
        return self._parse_user_list(self.config.get(ConfigKeys.BOT_RESPONSE_BLACKLIST))

    def _is_response_whitelisted_user(
        self, *, user_id: str, handle: str | None
    ) -> bool:
        whitelist = self._load_response_whitelist_users()
        if not whitelist:
            return False
        return any(
            c in whitelist
            for c in self._user_candidates(user_id=user_id, handle=handle)
        )

    def is_response_blacklisted_user(self, *, user_id: str, handle: str | None) -> bool:
        blacklist = self._load_response_blacklist_users()
        if not blacklist:
            return False
        return any(
            c in blacklist
            for c in self._user_candidates(user_id=user_id, handle=handle)
        )

    @staticmethod
    def _parse_duration_seconds(value: Any) -> int | None:
        parsed = MisskeyBot._parse_duration_number(value)
        if parsed is not None:
            return parsed
        if not isinstance(value, str):
            return None
        s = value.strip().lower()
        if not s:
            return None
        if s in {"-1", "unlimited", "none", "off"}:
            return -1
        return MisskeyBot._parse_duration_string(s)

    @staticmethod
    def _parse_duration_number(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return None

    @staticmethod
    def _parse_duration_string(s: str) -> int | None:
        total = 0
        pos = 0
        while pos < len(s):
            m = _DURATION_PART_RE.match(s, pos)
            if not m:
                return None
            num = float(m.group(1))
            unit = (m.group(2) or "").lower()
            end = m.end()
            if not unit:
                return int(num) if end == len(s) and total == 0 else None
            mul = _DURATION_UNITS.get(unit)
            if not mul:
                return None
            total += int(num * mul)
            pos = end
        return total

    def _duration_config_seconds(self, key: str) -> int:
        seconds = self._parse_duration_seconds(self.config.get(key))
        if seconds is None:
            return -1
        return seconds

    async def _get_response_limit_state(self, user_id: str) -> _ResponseLimitState:
        if user_id in self._response_limits:
            return self._response_limits[user_id]
        last_reply_ts = None
        turns = 0
        blocked_until_ts = None
        row = await self.persistence.get_response_limit_state(user_id)
        if row:
            last_reply_ts, turns, blocked_until_ts = row
            if blocked_until_ts == -1:
                blocked_until_ts = float("inf")
        state = _ResponseLimitState(
            last_reply_ts=last_reply_ts,
            turns=int(turns or 0),
            blocked_until_ts=blocked_until_ts,
        )
        self._response_limits[user_id] = state
        return state

    async def _save_response_limit_state(
        self, user_id: str, state: _ResponseLimitState
    ) -> None:
        blocked_until_ts = state.blocked_until_ts
        if blocked_until_ts == float("inf"):
            blocked_until_ts = -1
        await self.persistence.set_response_limit_state(
            user_id=user_id,
            last_reply_ts=state.last_reply_ts,
            turns=state.turns,
            blocked_until_ts=blocked_until_ts,
        )

    async def get_response_block_reply(
        self, *, user_id: str, handle: str | None
    ) -> str | None:
        if self._is_response_whitelisted_user(user_id=user_id, handle=handle):
            return None
        now = time.time()
        state = await self._get_response_limit_state(user_id)
        if (
            state.blocked_until_ts is not None
            and state.blocked_until_ts != float("inf")
            and now >= state.blocked_until_ts
        ):
            state.turns = 0
            state.blocked_until_ts = None
            await self._save_response_limit_state(user_id, state)
        if state.blocked_until_ts is not None and now < state.blocked_until_ts:
            return self.config.get(ConfigKeys.BOT_RESPONSE_MAX_TURNS_REPLY)
        interval = self._duration_config_seconds(ConfigKeys.BOT_RESPONSE_RATE_LIMIT)
        if (
            interval > 0
            and state.last_reply_ts is not None
            and now - state.last_reply_ts < interval
        ):
            return self.config.get(ConfigKeys.BOT_RESPONSE_RATE_LIMIT_REPLY)
        max_turns = self.config.get(ConfigKeys.BOT_RESPONSE_MAX_TURNS)
        if isinstance(max_turns, int) and max_turns >= 0 and state.turns >= max_turns:
            release = self._duration_config_seconds(
                ConfigKeys.BOT_RESPONSE_MAX_TURNS_RELEASE
            )
            if release < 0:
                state.blocked_until_ts = float("inf")
            else:
                state.blocked_until_ts = now + release
            await self._save_response_limit_state(user_id, state)
            return self.config.get(ConfigKeys.BOT_RESPONSE_MAX_TURNS_REPLY)
        return None

    async def record_response(self, user_id: str, *, count_turn: bool) -> None:
        state = await self._get_response_limit_state(user_id)
        state.last_reply_ts = time.time()
        if count_turn:
            state.turns += 1
        await self._save_response_limit_state(user_id, state)

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
            (self.handlers.auto_post.reset_daily_counters, 0),
            (self.persistence.vacuum, 2),
            (self.persistence.cleanup_response_limit_state, 3),
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
            self.streaming.on_notification(self.handlers.on_notification)
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
