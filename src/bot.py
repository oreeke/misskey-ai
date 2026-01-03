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
    DEFAULT_ERROR_MESSAGE,
    ERROR_MESSAGES,
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
from .streaming import ChannelType, StreamingClient
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


class ErrorResponder:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(
        self,
        error: Exception,
        *,
        mention: MentionContext | None = None,
        message: dict[str, Any] | None = None,
    ) -> None:
        error_message = ERROR_MESSAGES.get(type(error).__name__, DEFAULT_ERROR_MESSAGE)
        try:
            if mention and mention.username:
                await self.bot.misskey.create_note(
                    text=f"@{mention.username}\n{error_message}", validate_reply=False
                )
                return
            if message:
                user_id = extract_user_id(message)
                if user_id:
                    await self.bot.misskey.send_message(user_id, error_message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"发送错误回复失败: {e}")


class MentionHandler:
    def __init__(self, bot: "MisskeyBot", errors: ErrorResponder):
        self.bot = bot
        self.errors = errors

    async def handle(self, note: dict[str, Any]) -> None:
        if not self.bot.config.get(ConfigKeys.BOT_RESPONSE_MENTION_ENABLED):
            return
        mention = self._parse(note)
        if not mention.mention_id:
            return
        try:
            async with self.bot.lock_actor(mention.user_id, mention.username):
                logger.info(
                    f"收到 @{mention.username} 的提及: {self.bot.format_log_text(mention.text)}"
                )
                if await self._try_plugin_response(mention, note):
                    return
                await self._generate_ai_response(mention)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理提及时出错: {e}")
            await self.errors.handle(e, mention=mention)

    def _parse(self, note: dict[str, Any]) -> MentionContext:
        try:
            is_reply_event = note.get("type") == "reply" and "note" in note
            logger.opt(lazy=True).debug(
                "提及数据: {}",
                lambda: json.dumps(note, ensure_ascii=False, indent=2),
            )
            mention_id = note.get("id")
            reply_target_id = note.get("note", {}).get("id")
            if is_reply_event:
                note_data = note["note"]
                text = note_data.get("text", "")
                user_id = note_data.get("userId")
                username = note_data.get("user", {}).get("username")
                reply_info = note_data.get("reply", {})
                if reply_info and reply_info.get("text"):
                    text = f"{reply_info.get('text')}\n\n{text}"
            else:
                text = note.get("note", {}).get("text", "")
                user_id = extract_user_id(note)
                username = extract_username(note)
            if not self._is_bot_mentioned(text):
                logger.debug(f"用户 @{username} 的回复中未 @机器人，跳过处理")
                mention_id = None
            return MentionContext(mention_id, reply_target_id, text, user_id, username)
        except Exception as e:
            logger.error(f"解析消息数据失败: {e}")
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
                logger.debug(f"提及已被插件处理: {result.get('plugin_name')}")
                response = result.get("response")
                if response:
                    formatted = f"@{mention.username}\n{response}"
                    await self.bot.misskey.create_note(
                        text=formatted, reply_id=mention.reply_target_id
                    )
                    logger.info(
                        f"插件已回复 @{mention.username}: {self.bot.format_log_text(formatted)}"
                    )
                return True
        return False

    async def _generate_ai_response(self, mention: MentionContext) -> None:
        reply = await self.bot.openai.generate_text(
            mention.text, self.bot.system_prompt, **self.bot.ai_config
        )
        logger.debug("生成提及回复成功")
        formatted = f"@{mention.username}\n{reply}"
        await self.bot.misskey.create_note(
            text=formatted, reply_id=mention.reply_target_id
        )
        logger.info(
            f"已回复 @{mention.username}: {self.bot.format_log_text(formatted)}"
        )


class ChatHandler:
    def __init__(self, bot: "MisskeyBot", errors: ErrorResponder):
        self.bot = bot
        self.errors = errors

    async def handle(self, message: dict[str, Any]) -> None:
        if not self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_ENABLED):
            return
        if not message.get("id"):
            logger.debug("缺少 ID，跳过处理")
            return
        logger.opt(lazy=True).debug(
            "聊天数据: {}",
            lambda: json.dumps(message, ensure_ascii=False, indent=2),
        )
        try:
            await self._process(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理聊天时出错: {e}")
            await self.errors.handle(e, message=message)

    async def _process(self, message: dict[str, Any]) -> None:
        text = message.get("text") or message.get("content") or message.get("body", "")
        user_id = extract_user_id(message)
        username = extract_username(message)
        has_media = bool(message.get("fileId") or message.get("file"))
        if not user_id:
            logger.debug("聊天缺少必要信息 - 用户 ID 为空")
            return
        if not text and not has_media:
            logger.debug("聊天缺少必要信息 - 文本为空且无媒体")
            return
        async with self.bot.lock_actor(user_id, username):
            if text:
                logger.info(
                    f"收到 @{username} 的聊天: {self.bot.format_log_text(text)}"
                )
            else:
                logger.info(f"收到 @{username} 的聊天: （无文本，包含媒体）")
            if await self._try_plugin_response(message, user_id, username):
                return
            if not text:
                return
            await self._generate_ai_response(user_id, username, text)

    async def _try_plugin_response(
        self, message: dict[str, Any], user_id: str, username: str
    ) -> bool:
        plugin_results = await self.bot.plugin_manager.on_message(message)
        for result in plugin_results:
            if result and result.get("handled"):
                logger.debug(f"聊天已被插件处理: {result.get('plugin_name')}")
                response = result.get("response")
                if response:
                    await self.bot.misskey.send_message(user_id, response)
                    logger.info(
                        f"插件已回复 @{username}: {self.bot.format_log_text(response)}"
                    )
                    text = message.get("text") or message.get("content") or ""
                    if text:
                        self.bot.append_chat_turn(
                            user_id,
                            text,
                            response,
                            self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY),
                        )
                return True
        return False

    async def _generate_ai_response(
        self, user_id: str, username: str, text: str
    ) -> None:
        limit = self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        history = await self.bot.get_or_load_chat_history(user_id, limit=limit)
        messages: list[dict[str, str]] = []
        if self.bot.system_prompt:
            messages.append({"role": "system", "content": self.bot.system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": text})
        reply = await self.bot.openai.generate_chat(messages, **self.bot.ai_config)
        logger.debug("生成聊天回复成功")
        await self.bot.misskey.send_message(user_id, reply)
        logger.info(f"已回复 @{username}: {self.bot.format_log_text(reply)}")
        self.bot.append_chat_turn(user_id, text, reply, limit)

    async def get_chat_history(
        self, user_id: str, limit: int | None = None
    ) -> list[dict[str, str]]:
        try:
            limit = limit or self.bot.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
            messages = await self.bot.misskey.get_messages(user_id, limit=limit)
            return [
                {
                    "role": "user" if msg.get("userId") == user_id else "assistant",
                    "content": msg.get("text", ""),
                }
                for msg in reversed(messages)
            ]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"获取聊天历史时出错: {e}")
            return []


class ReactionHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(self, reaction: dict[str, Any]) -> None:
        username = extract_username(reaction)
        note_id = reaction.get("note", {}).get("id", "unknown")
        reaction_type = reaction.get("reaction", "unknown")
        logger.info(f"用户 @{username} 对帖子 {note_id} 做出反应: {reaction_type}")
        logger.opt(lazy=True).debug(
            "反应数据: {}",
            lambda: json.dumps(reaction, ensure_ascii=False, indent=2),
        )
        try:
            await self.bot.plugin_manager.on_reaction(reaction)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理反应事件时出错: {e}")


class FollowHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(self, follow: dict[str, Any]) -> None:
        username = extract_username(follow)
        logger.info(f"用户 @{username} 关注了 @{self.bot.bot_username}")
        logger.opt(lazy=True).debug(
            "关注数据: {}",
            lambda: json.dumps(follow, ensure_ascii=False, indent=2),
        )
        try:
            await self.bot.plugin_manager.on_follow(follow)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理关注事件时出错: {e}")


class AutoPostService:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def run(self) -> None:
        max_posts = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY)
        if not self.bot.runtime.running or not self.bot.runtime.check_post_counter(
            max_posts
        ):
            return
        try:
            plugin_results = await self.bot.plugin_manager.on_auto_post()
            if await self._try_plugin_post(plugin_results, max_posts):
                return
            await self._generate_ai_post(plugin_results, max_posts)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"自动发帖时出错: {e}")

    async def _try_plugin_post(self, plugin_results: list[Any], max_posts: int) -> bool:
        for result in plugin_results:
            if result and result.get("content"):
                content = result.get("content")
                visibility = result.get(
                    "visibility",
                    self.bot.config.get(ConfigKeys.BOT_AUTO_POST_VISIBILITY),
                )
                await self.bot.misskey.create_note(content, visibility=visibility)
                self.bot.runtime.post_count()
                logger.info(f"自动发帖成功: {self.bot.format_log_text(content)}")
                logger.info(f"今日发帖计数: {self.bot.runtime.posts_today}/{max_posts}")
                return True
        return False

    async def _generate_ai_post(
        self, plugin_results: list[Any], max_posts: int
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
                    f"{result.get('plugin_name')} 插件请求修改提示词: {plugin_prompt}"
                )
        post_prompt = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_PROMPT, "")
        try:
            content = await self._generate_post(
                self.bot.system_prompt, post_prompt, plugin_prompt, timestamp_override
            )
        except ValueError as e:
            logger.warning(f"自动发帖失败，跳过本次发帖: {e}")
            return
        visibility = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_VISIBILITY)
        await self.bot.misskey.create_note(content, visibility=visibility)
        self.bot.runtime.post_count()
        logger.info(f"自动发帖成功: {self.bot.format_log_text(content)}")
        logger.info(f"今日发帖计数: {self.bot.runtime.posts_today}/{max_posts}")

    async def _generate_post(
        self,
        system_prompt: str,
        prompt: str,
        plugin_prompt: str,
        timestamp_override: int | None = None,
    ) -> str:
        if not prompt:
            raise ValueError("缺少提示词")
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
        self.errors = ErrorResponder(bot)
        self.mention = MentionHandler(bot, self.errors)
        self.chat = ChatHandler(bot, self.errors)
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
            self.streaming = StreamingClient(instance_url, access_token)
            self.openai = OpenAIAPI(
                config.get_required(ConfigKeys.OPENAI_API_KEY),
                config.get(ConfigKeys.OPENAI_MODEL),
                config.get(ConfigKeys.OPENAI_API_BASE),
            )
            self.scheduler = AsyncIOScheduler()
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"初始化失败: {e}")
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
        logger.info("机器人初始化完成")

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

    def get_streaming_channels(self) -> list[str]:
        active = {ChannelType.MAIN.value, *self.timeline_channels}
        ordered = [
            ChannelType.MAIN.value,
            ChannelType.HOME_TIMELINE.value,
            ChannelType.LOCAL_TIMELINE.value,
            ChannelType.HYBRID_TIMELINE.value,
            ChannelType.GLOBAL_TIMELINE.value,
        ]
        return [c for c in ordered if c in active]

    async def restart_streaming(self) -> None:
        if (task := self.runtime.tasks.get("streaming")) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.streaming.disconnect()
        channels = self.get_streaming_channels()
        await self.streaming.connect_once(channels)
        self.runtime.add_task("streaming", self.streaming.connect(channels))

    async def get_or_load_chat_history(
        self, user_id: str, *, limit: int | None
    ) -> list[dict[str, str]]:
        limit = limit or self.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        if (cached := self._chat_histories.get(user_id)) is not None:
            return list(cached)[-max(0, limit * 2) :]
        history = await self.handlers.chat.get_chat_history(user_id, limit=limit)
        trimmed = history[-max(0, limit * 2) :]
        self._chat_histories[user_id] = trimmed
        return list(trimmed)

    def append_chat_turn(
        self, user_id: str, user_text: str, assistant_text: str, limit: int | None
    ) -> None:
        limit = limit or self.config.get(ConfigKeys.BOT_RESPONSE_CHAT_MEMORY)
        history = list(self._chat_histories.get(user_id) or [])
        if user_text and not (
            history
            and history[-1].get("role") == "user"
            and history[-1].get("content") == user_text
        ):
            history.append({"role": "user", "content": user_text})
        if assistant_text and not (
            history
            and history[-1].get("role") == "assistant"
            and history[-1].get("content") == assistant_text
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
            logger.warning("机器人已在运行中")
            return
        logger.info("启动服务组件...")
        self.runtime.running = True
        await self._initialize_services()
        await self._setup_scheduler()
        await self._setup_streaming()
        logger.info("服务组件就绪，等待新任务...")
        memory_usage = get_memory_usage()
        logger.debug(f"内存使用: {memory_usage['rss_mb']} MB")

    async def _initialize_services(self) -> None:
        await self.persistence.initialize()
        await self.openai.initialize()
        current_user = await self.misskey.get_current_user()
        self.bot_user_id = current_user.get("id")
        self.bot_username = current_user.get("username")
        logger.info(
            f"已连接 Misskey 实例，机器人 ID: {self.bot_user_id}, @{self.bot_username}"
        )
        await self.plugin_manager.load_plugins()
        await self.plugin_manager.on_startup()

    async def _setup_scheduler(self) -> None:
        cron_jobs = [
            (self.runtime.reset_daily_counters, 0),
            (self.persistence.vacuum, 2),
        ]
        for func, hour in cron_jobs:
            self.scheduler.add_job(func, "cron", hour=hour, minute=0, second=0)
        if self.config.get(ConfigKeys.BOT_AUTO_POST_ENABLED):
            interval_minutes = self.config.get(ConfigKeys.BOT_AUTO_POST_INTERVAL)
            logger.info(f"自动发帖已启用，间隔: {interval_minutes} 分钟")
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
            channels = self.get_streaming_channels()
            await self.streaming.connect_once(channels)
            self.runtime.add_task("streaming", self.streaming.connect(channels))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"设置 Streaming 连接失败: {e}")
            raise

    async def stop(self) -> None:
        if not self.runtime.running:
            logger.warning("机器人已停止")
            return
        logger.info("停止服务组件...")
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"停止机器人时出错: {e}")
        finally:
            logger.info("服务组件已停止")

    @staticmethod
    def format_log_text(text: str, max_length: int = 50) -> str:
        return (
            "None"
            if not text
            else f"{text[:max_length]}{'...' if len(text) > max_length else ''}"
        )

    @property
    def ai_config(self) -> dict[str, Any]:
        return {
            "max_tokens": self.config.get(ConfigKeys.OPENAI_MAX_TOKENS),
            "temperature": self.config.get(ConfigKeys.OPENAI_TEMPERATURE),
        }
