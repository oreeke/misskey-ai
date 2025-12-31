import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from .config import Config
from .constants import (
    DEFAULT_ERROR_MESSAGE,
    ERROR_MESSAGES,
    ConfigKeys,
)
from .exceptions import (
    APIBadRequestError,
    APIConnectionError,
    APIRateLimitError,
    AuthenticationError,
    ConfigurationError,
)
from .misskey_api import MisskeyAPI
from .openai_api import OpenAIAPI
from .persistence import PersistenceManager
from .plugin import PluginManager
from .runtime import BotRuntime
from .streaming import StreamingClient
from .transport import ClientSession
from .utils import extract_user_id, extract_username, get_memory_usage

__all__ = ("MisskeyBot",)


@dataclass(slots=True)
class MentionContext:
    mention_id: Optional[str]
    reply_target_id: Optional[str]
    text: str
    user_id: Optional[str]
    username: Optional[str]


class ErrorResponder:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(
        self,
        error: Exception,
        *,
        mention: Optional[MentionContext] = None,
        message: Optional[dict[str, Any]] = None,
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
        except (
            APIBadRequestError,
            APIConnectionError,
            APIRateLimitError,
            AuthenticationError,
            OSError,
        ) as e:
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
            logger.info(
                f"收到 @{mention.username} 的提及: {self.bot._format_log_text(mention.text)}"
            )
            if await self._try_plugin_response(mention, note):
                return
            await self._generate_ai_response(mention)
        except (
            ValueError,
            APIBadRequestError,
            APIConnectionError,
            APIRateLimitError,
            AuthenticationError,
            OSError,
        ) as e:
            logger.error(f"处理提及时出错: {e}")
            await self.errors.handle(e, mention=mention)

    def _parse(self, note: dict[str, Any]) -> MentionContext:
        try:
            is_reply_event = note.get("type") == "reply" and "note" in note
            logger.debug(f"提及数据: {json.dumps(note, ensure_ascii=False, indent=2)}")
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
        except ValueError as e:
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
                        f"插件已回复 @{mention.username}: {self.bot._format_log_text(formatted)}"
                    )
                return True
        return False

    async def _generate_ai_response(self, mention: MentionContext) -> None:
        reply = await self.bot.openai.generate_text(
            mention.text, self.bot.system_prompt, **self.bot._ai_config
        )
        logger.debug("生成提及回复成功")
        formatted = f"@{mention.username}\n{reply}"
        await self.bot.misskey.create_note(
            text=formatted, reply_id=mention.reply_target_id
        )
        logger.info(
            f"已回复 @{mention.username}: {self.bot._format_log_text(formatted)}"
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
        logger.debug(f"聊天数据: {json.dumps(message, ensure_ascii=False, indent=2)}")
        try:
            await self._process(message)
        except (
            APIBadRequestError,
            APIConnectionError,
            APIRateLimitError,
            AuthenticationError,
            ValueError,
            OSError,
        ) as e:
            logger.error(f"处理聊天时出错: {e}")
            await self.errors.handle(e, message=message)

    async def _process(self, message: dict[str, Any]) -> None:
        text = message.get("text") or message.get("content") or message.get("body", "")
        user_id = extract_user_id(message)
        username = extract_username(message)
        if not (user_id and text):
            logger.debug(f"聊天缺少必要信息 - 用户 ID: {user_id}, 文本: {bool(text)}")
            return
        logger.info(f"收到 @{username} 的聊天: {self.bot._format_log_text(text)}")
        if await self._try_plugin_response(message, user_id, username):
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
                        f"插件已回复 @{username}: {self.bot._format_log_text(response)}"
                    )
                return True
        return False

    async def _generate_ai_response(
        self, user_id: str, username: str, text: str
    ) -> None:
        history = await self._get_chat_history(user_id)
        history.append({"role": "user", "content": text})
        if not history or history[0].get("role") != "system":
            history.insert(0, {"role": "system", "content": self.bot.system_prompt})
        reply = await self.bot.openai.generate_chat(history, **self.bot._ai_config)
        logger.debug("生成聊天回复成功")
        await self.bot.misskey.send_message(user_id, reply)
        logger.info(f"已回复 @{username}: {self.bot._format_log_text(reply)}")
        history.append({"role": "assistant", "content": reply})

    async def _get_chat_history(
        self, user_id: str, limit: Optional[int] = None
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
        except (APIConnectionError, APIRateLimitError, ValueError, OSError) as e:
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
        logger.debug(f"反应数据: {json.dumps(reaction, ensure_ascii=False, indent=2)}")
        try:
            await self.bot.plugin_manager.on_reaction(reaction)
        except (ValueError, OSError) as e:
            logger.error(f"处理反应事件时出错: {e}")


class FollowHandler:
    def __init__(self, bot: "MisskeyBot"):
        self.bot = bot

    async def handle(self, follow: dict[str, Any]) -> None:
        username = extract_username(follow)
        logger.info(f"用户 @{username} 关注了 @{self.bot.bot_username}")
        logger.debug(f"关注数据: {json.dumps(follow, ensure_ascii=False, indent=2)}")
        try:
            await self.bot.plugin_manager.on_follow(follow)
        except (ValueError, OSError) as e:
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
        except (
            APIConnectionError,
            APIRateLimitError,
            AuthenticationError,
            ValueError,
            OSError,
        ) as e:
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
                logger.info(f"自动发帖成功: {self.bot._format_log_text(content)}")
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
        logger.info(f"自动发帖成功: {self.bot._format_log_text(content)}")
        logger.info(f"今日发帖计数: {self.bot.runtime.posts_today}/{max_posts}")

    async def _generate_post(
        self,
        system_prompt: str,
        prompt: str,
        plugin_prompt: str,
        timestamp_override: Optional[int] = None,
    ) -> str:
        if not prompt:
            raise ValueError("缺少提示词")
        timestamp_min = timestamp_override or int(
            datetime.now(timezone.utc).timestamp() // 60
        )
        full_prompt = f"[{timestamp_min}] {plugin_prompt}{prompt}"
        return await self.bot.openai.generate_text(
            full_prompt, system_prompt, **self.bot._ai_config
        )


class BotHandlers:
    def __init__(self, bot: "MisskeyBot"):
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
        self.plugin_manager = PluginManager(config, persistence=self.persistence)
        self.runtime = BotRuntime(self)
        self.system_prompt = config.get(ConfigKeys.BOT_SYSTEM_PROMPT, "")
        self.bot_user_id = None
        self.bot_username = None
        self.handlers = BotHandlers(self)
        logger.info("机器人初始化完成")

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
            await self.streaming.connect_once()
            self.runtime.add_task("streaming", self.streaming.connect())
        except (ValueError, OSError) as e:
            logger.error(f"设置 Streaming 连接失败: {e}")
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
            if (
                hasattr(self.scheduler, "_eventloop")
                and self.scheduler._eventloop is not None
            ):
                self.scheduler.shutdown(wait=False)
            await self.runtime.cleanup_tasks()
            await self.streaming.close()
            await self.misskey.close()
            await self.openai.close()
            await ClientSession.close_session(silent=True)
            await self.persistence.close()
        except (OSError, ValueError, RuntimeError) as e:
            logger.error(f"停止机器人时出错: {e}")
        finally:
            logger.info("服务组件已停止")

    def _format_log_text(self, text: str, max_length: int = 50) -> str:
        return (
            "None"
            if not text
            else f"{text[:max_length]}{'...' if len(text) > max_length else ''}"
        )

    @property
    def _ai_config(self) -> dict[str, Any]:
        return {
            "max_tokens": self.config.get(ConfigKeys.OPENAI_MAX_TOKENS),
            "temperature": self.config.get(ConfigKeys.OPENAI_TEMPERATURE),
        }
