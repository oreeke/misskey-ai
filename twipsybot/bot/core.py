import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cachetools import TTLCache
from loguru import logger

from ..clients.misskey.misskey_api import MisskeyAPI
from ..clients.misskey.streaming import StreamingClient
from ..clients.misskey.transport import TCPClient
from ..clients.openai import OpenAIAPI
from ..db.sqlite import DBManager
from ..plugin.manager import PluginManager
from ..shared.config import Config
from ..shared.config_keys import ConfigKeys
from ..shared.constants import (
    CHAT_CACHE_MAX_USERS,
    CHAT_CACHE_TTL,
    USER_LOCK_CACHE_MAX,
    USER_LOCK_TTL,
)
from ..shared.exceptions import ConfigurationError
from ..shared.utils import get_memory_usage
from .connect import StreamingConnector
from .handlers import BotHandlers
from .limits import ResponseLimiter
from .runtime import BotRuntime

__all__ = ("MisskeyBot",)


class MisskeyBot:
    def __init__(self, config: Config):
        self.config = config
        try:
            instance_url = config.get_required(ConfigKeys.MISSKEY_INSTANCE_URL)
            access_token = config.get_required(ConfigKeys.MISSKEY_ACCESS_TOKEN)
            self._misskey_transport = TCPClient()
            self.misskey = MisskeyAPI(
                instance_url, access_token, transport=self._misskey_transport
            )
            self.streaming = StreamingClient(
                instance_url,
                access_token,
                log_dump_events=bool(config.get(ConfigKeys.LOG_DUMP_EVENTS)),
                transport=self._misskey_transport,
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
        self.db = DBManager(config.get(ConfigKeys.DB_PATH), config=config)
        self.runtime = BotRuntime(self)
        self.limits = ResponseLimiter(
            config=config,
            db=self.db,
            instance_url=getattr(self.misskey, "instance_url", None),
        )
        self.plugin_manager = PluginManager(
            config,
            db=self.db,
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
        self.connect = StreamingConnector(
            config=config,
            misskey=self.misskey,
            streaming=self.streaming,
            runtime=self.runtime,
            handlers=self.handlers,
        )
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

    def is_response_blacklisted_user(self, *, user_id: str, handle: str | None) -> bool:
        return self.limits.is_response_blacklisted_user(user_id=user_id, handle=handle)

    async def get_response_block_reply(
        self, *, user_id: str, handle: str | None
    ) -> str | None:
        return await self.limits.get_response_block_reply(
            user_id=user_id, handle=handle
        )

    async def maybe_send_blocked_reply(
        self,
        *,
        user_id: str,
        handle: str | None,
        send_reply: Callable[[str], Awaitable[None]],
    ) -> bool:
        return await self.limits.maybe_send_blocked_reply(
            user_id=user_id,
            handle=handle,
            send_reply=send_reply,
        )

    async def apply_handled_plugin_result(
        self,
        result: Any,
        *,
        kind: str,
        user_id: str | None,
        send_reply: Callable[[str], Awaitable[None]],
        log_sent: Callable[[str], None],
        after_sent: Callable[[str], Any] | None = None,
    ) -> bool:
        if not (isinstance(result, dict) and result.get("handled")):
            return False
        logger.debug(f"{kind} handled by plugin: {result.get('plugin_name')}")
        response = result.get("response")
        if not response:
            return True
        await send_reply(response)
        log_sent(response)
        if user_id:
            await self.record_response(user_id, count_turn=True)
        if after_sent is not None:
            maybe = after_sent(response)
            if inspect.isawaitable(maybe):
                await maybe
        return True

    async def run_response_pipeline(
        self,
        *,
        actor_id: str | None,
        actor_name: str | None,
        user_id: str | None,
        handle: str | None,
        log_incoming: Callable[[], None],
        send_reply: Callable[[str], Awaitable[None]],
        plugin_call: Callable[[], Awaitable[list[Any]]],
        plugin_kind: str,
        plugin_log_sent: Callable[[str], None],
        plugin_after_sent: Callable[[str], Any] | None = None,
        ai_generate: Callable[[], Awaitable[str | None]],
        ai_log_sent: Callable[[str], None],
        ai_after_sent: Callable[[str], Any] | None = None,
    ) -> None:
        async with self.lock_actor(actor_id, actor_name):
            log_incoming()
            if user_id and await self.maybe_send_blocked_reply(
                user_id=user_id, handle=handle, send_reply=send_reply
            ):
                return
            plugin_results = await plugin_call()
            for result in plugin_results:
                if await self.apply_handled_plugin_result(
                    result,
                    kind=plugin_kind,
                    user_id=user_id,
                    send_reply=send_reply,
                    log_sent=plugin_log_sent,
                    after_sent=plugin_after_sent,
                ):
                    return
            reply = await ai_generate()
            if not reply:
                return
            await send_reply(reply)
            ai_log_sent(reply)
            if user_id:
                await self.record_response(user_id, count_turn=True)
            if ai_after_sent is not None:
                maybe = ai_after_sent(reply)
                if inspect.isawaitable(maybe):
                    await maybe

    async def record_response(self, user_id: str, *, count_turn: bool) -> None:
        await self.limits.record_response(user_id, count_turn=count_turn)

    def load_timeline_channels(self) -> set[str]:
        return self.connect.load_timeline_channels()

    async def get_streaming_channels(self):
        return await self.connect.get_streaming_channels()

    async def restart_streaming(self) -> None:
        await self.connect.restart_streaming()

    async def get_or_load_chat_history(
        self,
        conversation_id: str,
        *,
        limit: int | None,
        user_id: str | None = None,
        room_id: str | None = None,
    ) -> list[dict[str, str]]:
        config_limit = self.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        limit_value = limit if isinstance(limit, int) else config_limit
        if not isinstance(limit_value, int):
            limit_value = 0
        if (cached := self._chat_histories.get(conversation_id)) is not None:
            return list(cached)[-max(0, limit_value * 2) :]
        if conversation_id.startswith("room:"):
            room_id = room_id or conversation_id.removeprefix("room:")
        history = await self.handlers.chat.get_chat_history(
            user_id=user_id, room_id=room_id, limit=limit_value
        )
        trimmed = history[-max(0, limit_value * 2) :]
        self._chat_histories[conversation_id] = trimmed
        return list(trimmed)

    def append_chat_turn(
        self, user_id: str, user_text: str, assistant_text: str, limit: int | None
    ) -> None:
        config_limit = self.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        limit_value = limit if isinstance(limit, int) else config_limit
        if not isinstance(limit_value, int):
            limit_value = 0
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
        self._chat_histories[user_id] = history[-max(0, limit_value * 2) :]

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
        await self.db.initialize()
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
            (self.db.vacuum, 2),
            (self.db.cleanup_response_limit_state, 3),
        ]
        for func, hour in cron_jobs:
            self.scheduler.add_job(func, "cron", hour=hour, minute=0, second=0)
        interval_minutes = self.config.get(ConfigKeys.BOT_AUTO_POST_INTERVAL)
        enabled = bool(self.config.get(ConfigKeys.BOT_AUTO_POST_ENABLED))
        logger.info(
            f"Auto-post scheduler ready; enabled={enabled}; interval: {interval_minutes} minutes"
        )
        self.scheduler.add_job(
            self.handlers.on_auto_post,
            "interval",
            minutes=interval_minutes,
            next_run_time=datetime.now(UTC) + timedelta(minutes=1),
            id="auto_post",
            replace_existing=True,
        )
        self.scheduler.start()

    async def _setup_streaming(self) -> None:
        await self.connect.setup_streaming()

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
            await self.db.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
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
