#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import Config
from .deepseek_api import DeepSeekAPI
from .misskey_api import MisskeyAPI
from .persistence import PersistenceManager
from .plugin_manager import PluginManager
from .exceptions import (
    MisskeyBotError,
    ConfigurationError,
    APIConnectionError,
    APIRateLimitError,
    AuthenticationError,
    WebSocketConnectionError
)
from .constants import (
    DEFAULT_MAX_RETRIES,
    MAX_PROCESSED_ITEMS_CACHE,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_MAX_DAILY_POSTS,
    DEFAULT_API_TIMEOUT,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_AUTO_POST_INTERVAL_MINUTES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_AUTO_POST_ENABLED,
    DEFAULT_AUTO_POST_VISIBILITY,
    DEFAULT_RESPONSE_MENTION_ENABLED,
    DEFAULT_RESPONSE_CHAT_ENABLED,
    DEFAULT_DB_PATH
)
from .utils import retry_async

ERROR_MESSAGES = {
    APIRateLimitError: "抱歉，请求过于频繁，请稍后再试。",
    AuthenticationError: "抱歉，服务配置有误，请联系管理员。",
    APIConnectionError: "抱歉，AI 服务暂时不可用，请稍后再试。",
    WebSocketConnectionError: "抱歉，WebSocket 连接失败，将使用轮询模式。",
    ValueError: "抱歉，请求参数无效，请检查输入。",
    RuntimeError: "抱歉，系统资源不足，请稍后再试。"
}
DEFAULT_ERROR_MESSAGE = "抱歉，处理您的消息时出现了错误。"

class MisskeyBot:
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, '_cleanup_needed') and self._cleanup_needed:
            await self.stop()
        return False
    
    def __init__(self, config: Config):
        if not isinstance(config, Config):
            raise ValueError("配置参数必须是 Config 类型")
        
        self.config = config
        self.startup_time = datetime.now(timezone.utc)
        logger.debug(f"机器人启动时间 (UTC): {self.startup_time.isoformat()}")

        try:
            self.misskey = MisskeyAPI(
                instance_url=config.get("misskey.instance_url"),
                access_token=config.get("misskey.access_token"),
                max_retries=config.get("api.max_retries", DEFAULT_MAX_RETRIES),
                timeout=config.get("api.timeout", DEFAULT_API_TIMEOUT),
                config=config,
            )
            
            self.deepseek = DeepSeekAPI(
                api_key=config.get("deepseek.api_key"),
                model=config.get("deepseek.model"),
                api_base=config.get("deepseek.api_base"),
                max_retries=config.get("api.max_retries", DEFAULT_MAX_RETRIES),
                timeout=config.get("api.timeout", DEFAULT_API_TIMEOUT),
            )
            
            self.scheduler = AsyncIOScheduler()
            self._cleanup_needed = True
            logger.debug("API 客户端和调度器初始化完成")
            
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            raise ConfigurationError(f"初始化失败: {e}")
        
        db_path = config.get("persistence.db_path", DEFAULT_DB_PATH)
        self.persistence = PersistenceManager(db_path)
        self.plugin_manager = PluginManager(config)
        self.processed_mentions: deque = deque(maxlen=MAX_PROCESSED_ITEMS_CACHE)
        self.processed_messages: deque = deque(maxlen=MAX_PROCESSED_ITEMS_CACHE)
        self.last_auto_post_time = datetime.now(timezone.utc) - timedelta(hours=24)
        self.posts_today = 0
        self.today = datetime.now(timezone.utc).date()
        self.system_prompt = config.get("bot.system_prompt", "")
        self.running = False
        self.tasks = []
        self.error_counts = {
            'api_errors': 0,
            'rate_limit_errors': 0,
            'auth_errors': 0,
            'connection_errors': 0
        }
        logger.info("机器人初始化完成")
    
    async def _load_recent_processed_items(self) -> None:
        try:
            recent_mentions = await self.persistence.get_recent_mentions(MAX_PROCESSED_ITEMS_CACHE)
            for mention in recent_mentions:
                self.processed_mentions.append(mention['note_id'])
            
            recent_messages = await self.persistence.get_recent_messages(MAX_PROCESSED_ITEMS_CACHE)
            for message in recent_messages:
                self.processed_messages.append(message['message_id'])
                
            logger.debug(f"已加载 {len(recent_mentions)} 个提及和 {len(recent_messages)} 个消息到缓存")
            
        except Exception as e:
            logger.warning(f"加载已处理消息 ID 到缓存时出错: {e}，将从空状态开始")
    
    async def _cleanup_old_processed_items(self) -> None:
        try:
            cleanup_days = self.config.get("persistence.cleanup_days", DEFAULT_CLEANUP_DAYS)
            deleted_count = await self.persistence.cleanup_old_records(cleanup_days)
            
            if deleted_count > 0:
                logger.debug(f"已清理 {deleted_count} 条过期记录")
                
        except Exception as e:
            logger.error(f"清理旧记录时出错: {e}")
    
    async def _retry_with_backoff(self, func, max_retries: int = 10, base_delay: float = 5, max_delay: float = 300):
        retry_count = 0
        while self.running and retry_count < max_retries:
            try:
                await func()
                return
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.running:
                    break
                retry_count += 1
                delay = min(base_delay * (2 ** (retry_count - 1)), max_delay)
                logger.error(f"{func.__name__}错误: {e}")
                logger.info(f"将在 {delay} 秒后重试... (尝试 {retry_count}/{max_retries})")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break
     
    def _handle_error(self, error: Exception, context: str = "") -> str:
        error_type = type(error).__name__
        self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1
        logger.error(f"错误类型: {error_type}, 上下文: {context}, 详情: {str(error)}")
        return ERROR_MESSAGES.get(type(error), DEFAULT_ERROR_MESSAGE)
    
    def get_error_stats(self) -> Dict[str, int]:
        return self.error_counts.copy()
    
    def _is_message_after_startup(self, message: Dict[str, Any]) -> bool:
        try:
            created_at = message.get('createdAt') or message.get('created_at') or message.get('timestamp')
            
            if not created_at:
                logger.debug(f"消息缺少时间戳信息: {message.get('id', 'unknown')}")
                return False
            
            if isinstance(created_at, str):
                try:
                    message_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    if message_time.tzinfo is None:
                        message_time = message_time.replace(tzinfo=timezone.utc)
                except ValueError:
                    logger.debug(f"无法解析时间戳格式: {created_at}")
                    return False
            elif isinstance(created_at, (int, float)):
                message_time = datetime.fromtimestamp(created_at / 1000 if created_at > 1e10 else created_at, tz=timezone.utc)
            else:
                logger.debug(f"未知的时间戳类型: {type(created_at)}")
                return False
            
            startup_time = self.startup_time
            if startup_time.tzinfo is None:
                startup_time = startup_time.replace(tzinfo=timezone.utc)
            
            is_after = message_time > startup_time
            logger.debug(f"消息时间检查 - 消息时间: {message_time.isoformat()}, 启动时间: {self.startup_time.isoformat()}, 结果: {is_after}")
            return is_after
            
        except Exception as e:
            logger.debug(f"检查消息时间时出错: {e}")
            return False
     
    async def start(self) -> None:
        if self.running:
            logger.warning("机器人已在运行中")
            return
            
        logger.info("启动服务组件...")
        self.running = True
        
        try:
            current_user = await self.misskey.get_current_user()
            self.bot_user_id = current_user.get("id")
            logger.info(f"已连接到 Misskey 实例，用户 ID: {self.bot_user_id}")
        except Exception as e:
            logger.error(f"连接 Misskey 实例失败: {e}")
            self.bot_user_id = None
        
        await self._load_recent_processed_items()
        await self.plugin_manager.load_plugins()
        await self.plugin_manager.on_startup()
        
        self.scheduler.add_job(
            self._reset_daily_post_count,
            "cron",
            hour=0,
            minute=0,
            second=0,
        )
        
        self.scheduler.add_job(
            lambda: asyncio.create_task(self._cleanup_old_processed_items()),
            "cron",
            hour=1,
            minute=0,
            second=0,
        )
        
        self.scheduler.add_job(
            lambda: asyncio.create_task(self.persistence.vacuum()),
            "cron",
            hour=2,
            minute=0,
            second=0,
        )
        
        if self.config.get("bot.auto_post.enabled", DEFAULT_AUTO_POST_ENABLED):
            interval_minutes = self.config.get("bot.auto_post.interval_minutes", DEFAULT_AUTO_POST_INTERVAL_MINUTES)
            logger.info(f"自动发帖已启用，间隔: {interval_minutes} 分钟")
            
            self.scheduler.add_job(
                self._auto_post,
                "interval",
                minutes=interval_minutes,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        
        self.scheduler.start()
        
        websocket_task = asyncio.create_task(self._start_websocket())
        self.tasks.append(websocket_task)
        
        polling_task = asyncio.create_task(self._poll_mentions())
        self.tasks.append(polling_task)
        
        logger.info("服务组件就绪，开始监听消息")
    
    async def stop(self) -> None:
        if not self.running:
            logger.warning("机器人已停止")
            return
            
        logger.info("停止服务组件...")
        self.running = False
        
        try:
            await self.plugin_manager.on_shutdown()
            await self.plugin_manager.cleanup_plugins()
            self.scheduler.shutdown()
            
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks = []
            
            await self.misskey.close()
            await self.persistence.close()
            
        except Exception as e:
            logger.error(f"停止机器人时出错: {e}")
        finally:
            self._cleanup_needed = False
            logger.info("服务组件已停止")
    
    async def _start_websocket(self) -> None:
        async def websocket_connect():
            await self.misskey.connect_websocket(self._handle_websocket_message)
        
        await self._retry_with_backoff(websocket_connect)
    
    async def _handle_websocket_message(self, data: Dict[str, Any]) -> None:
        try:
            logger.debug(f"收到 WebSocket 消息: {json.dumps(data, ensure_ascii=False, indent=2)}")
            
            if data.get("type") != "channel":
                logger.debug(f"忽略非频道消息，类型: {data.get('type')}")
                return
            
            body = data.get("body", {})
            if not body:
                logger.debug("消息体为空，忽略")
                return
            
            message_type = body.get("type")
            logger.debug(f"消息类型: {message_type}")
            
            if message_type == "mention" and self.config.get("bot.response.mention_enabled", DEFAULT_RESPONSE_MENTION_ENABLED):
                note = body.get("body", {})
                if note and note.get("id") not in self.processed_mentions:
                    logger.debug(f"处理提及消息: {note.get('id')}")
                    await self._handle_mention(note)
                else:
                    logger.debug(f"提及消息已处理或无效: {note.get('id') if note else 'None'}")
            
            elif message_type in ["messaging_message", "messagingMessage", "message", "chat"] and self.config.get("bot.response.chat_enabled", DEFAULT_RESPONSE_CHAT_ENABLED):
                message = body.get("body", {})
                if message and message.get("id") not in self.processed_messages:
                    logger.debug(f"处理聊天消息: {message.get('id')}")
                    await self._handle_message(message)
                else:
                    logger.debug(f"聊天消息已处理或无效: {message.get('id') if message else 'None'}")
            
            else:
                logger.debug(f"未处理的消息类型: {message_type}")
                    
        except Exception as e:
            logger.error(f"处理 WebSocket 消息时出错: {e}")
    
    async def _poll_mentions(self) -> None:
        base_delay = self.config.get("bot.response.polling_interval", DEFAULT_POLLING_INTERVAL)
        
        async def poll_once():
            if self.config.get("bot.response.mention_enabled", DEFAULT_RESPONSE_MENTION_ENABLED):
                mentions = await self.misskey.get_mentions(limit=20)
                if mentions:
                    logger.debug(f"轮询获取到 {len(mentions)} 个提及")
                
                for mention in mentions:
                    if mention["id"] not in self.processed_mentions:
                        await self._handle_mention(mention)
            
            if self.config.get("bot.response.chat_enabled", DEFAULT_RESPONSE_CHAT_ENABLED):
                await self._poll_chat_messages()
            
            await asyncio.sleep(base_delay)
        
        while self.running:
            try:
                await poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.running:
                    break
                logger.error(f"轮询错误: {e}")
                await asyncio.sleep(base_delay)
    
    async def _poll_chat_messages(self) -> None:
        try:
            messages = await self.misskey.get_all_chat_messages(limit=20)
            
            if messages:
                logger.debug(f"轮询获取到 {len(messages)} 条聊天消息")
            
            for message in messages:
                message_id = message.get("id")
                if message_id and message_id not in self.processed_messages:
                    if not await self.persistence.is_message_processed(message_id):
                        logger.debug(f"通过轮询发现新聊天消息: {message_id}")
                        await self._handle_message(message)
                    else:
                        logger.debug(f"聊天消息已在数据库中标记为已处理: {message_id}")
                else:
                    logger.debug(f"聊天消息已在缓存中: {message_id}")
                        
        except Exception as e:
            logger.error(f"轮询聊天消息时出错: {e}")
            logger.debug(f"轮询聊天消息详细错误: {e}", exc_info=True)
    
    async def _get_chat_notifications(self) -> List[Dict[str, Any]]:
        logger.warning("_get_chat_notifications 方法已弃用，请使用 get_all_chat_messages 方法")
        try:
            return await self.misskey.get_all_chat_messages(limit=20)
            
        except Exception as e:
            logger.debug(f"获取聊天消息失败: {e}")
            return []
    
    async def _handle_mention(self, note: Dict[str, Any]) -> None:
        note_id = note.get("id")
        if not note_id:
            return
            
        if not self._is_message_after_startup(note):
            logger.debug(f"跳过启动时间之前的提及消息: {note_id}")
            await self.persistence.mark_mention_processed(note_id, note.get("user", {}).get("id"), note.get("user", {}).get("username", "unknown"))
            self.processed_mentions.append(note_id)
            return

        if note_id in self.processed_mentions or await self.persistence.is_mention_processed(note_id):
            return
        
        try:
            if not isinstance(note, dict):
                raise ValueError("提及数据必须是字典格式")
            
            required_fields = ["user", "id", "text"]
            for field in required_fields:
                if field not in note:
                    raise ValueError(f"提及数据缺少必要字段: {field}")
            
            text = note.get("text", "")
            user = note.get("user", {})
            username = user.get("username", "用户")
            user_id = user.get("id")
            
            await self.persistence.mark_mention_processed(note_id, user_id, username)
            self.processed_mentions.append(note_id)
            
            logger.info(f"收到 @{username} 的提及: {text[:100]}{'...' if len(text) > 100 else ''}")
            
            plugin_results = await self.plugin_manager.on_mention(note)
            for result in plugin_results:
                if result and result.get("handled"):
                    logger.debug(f"提及消息已被插件处理: {result.get('plugin_name')}")
                    return
            
            max_tokens = self.config.get("deepseek.max_tokens", DEFAULT_MAX_TOKENS)
            temperature = self.config.get("deepseek.temperature", DEFAULT_TEMPERATURE)
            
            try:
                reply = await self.deepseek.generate_reply(text, self.system_prompt, username, max_tokens=max_tokens, temperature=temperature)
                logger.debug(f"生成提及回复成功")
            except (APIRateLimitError, APIConnectionError, AuthenticationError) as e:
                error_message = self._handle_error(e, "生成回复时")
                await self._send_error_reply(username, note_id, error_message)
                return
            
            try:
                await self.misskey.create_note(reply, reply_id=note_id)
                logger.info(f"已回复 @{username}: {reply[:50]}{'...' if len(reply) > 50 else ''}")
            except (APIRateLimitError, APIConnectionError, AuthenticationError) as e:
                self._handle_error(e, "发送回复时")
                await self._send_error_reply(username, note_id, "抱歉，回复发送失败，请稍后再试。")
            
        except ValueError as e:
            logger.error(f"输入验证错误: {e}")
            self._handle_error(e, "处理提及时")
        except Exception as e:
            logger.error(f"处理提及时出错: {e}")
            self._handle_error(e, "处理提及时")
            try:
                username = note.get("user", {}).get("username", "用户")
                if username and note_id:
                    await self._send_error_reply(username, note_id, "抱歉，处理您的消息时出现了错误。")
            except Exception as reply_error:
                logger.error(f"发送错误回复失败: {reply_error}")
    
    async def _send_error_reply(self, username: str, note_id: str, message: str) -> None:
        try:
            await self.misskey.create_note(
                text=f"@{username} {message}",
                reply_id=note_id
            )
        except Exception as e:
            logger.error(f"发送错误回复失败: {e}")
    
    async def _handle_message(self, message: Dict[str, Any]) -> None:
        logger.debug(f"处理聊天消息: {json.dumps(message, ensure_ascii=False, indent=2)}")
        
        message_id = message.get("id")
        if not message_id:
            logger.debug("消息缺少 ID，跳过处理")
            return
            
        if not self._is_message_after_startup(message):
            logger.debug(f"跳过启动时间之前的聊天消息: {message_id}")
            user_id = message.get("userId") or message.get("user_id") or message.get("fromUserId") or message.get("from_user_id")
            if not user_id and "user" in message:
                user_obj = message.get("user", {})
                user_id = user_obj.get("id") if isinstance(user_obj, dict) else None
            await self.persistence.mark_message_processed(message_id, user_id, "private")
            self.processed_messages.append(message_id)
            return

        if message_id in self.processed_messages or await self.persistence.is_message_processed(message_id):
            logger.debug(f"消息已处理: {message_id}")
            return
        
        try:
            text = message.get("text") or message.get("content") or message.get("body", "")
            user_id = message.get("userId") or message.get("user_id") or message.get("fromUserId") or message.get("from_user_id")
            
            if not user_id and "user" in message:
                user_obj = message.get("user", {})
                user_id = user_obj.get("id") if isinstance(user_obj, dict) else None
            
            if not user_id and "sender" in message:
                sender_obj = message.get("sender", {})
                user_id = sender_obj.get("id") if isinstance(sender_obj, dict) else None
            
            logger.debug(f"解析消息 - ID: {message_id}, 用户 ID: {user_id}, 文本: {text[:50] if text else 'None'}...")
            
            if self.bot_user_id and user_id == self.bot_user_id:
                logger.debug(f"跳过自己发送的消息: {message_id}")
                await self.persistence.mark_message_processed(message_id, user_id, "private")
                self.processed_messages.append(message_id)
                return
            
            await self.persistence.mark_message_processed(message_id, user_id, "private")
            self.processed_messages.append(message_id)
            
            if not user_id or not text:
                logger.debug(f"消息缺少必要信息 - 用户 ID: {user_id}, 文本: {bool(text)}")
                return
            
            logger.info(f"收到用户 {user_id} 的私信: {text[:100]}{'...' if len(text) > 100 else ''}")
            
            plugin_results = await self.plugin_manager.on_message(message)
            for result in plugin_results:
                if result and result.get("handled"):
                    logger.debug(f"私信消息已被插件处理: {result.get('plugin_name')}")
                    return
            
            chat_history = await self._get_chat_history(user_id)
            chat_history.append({"role": "user", "content": text})
            
            if not chat_history or chat_history[0].get("role") != "system":
                chat_history.insert(0, {"role": "system", "content": self.system_prompt})
            
            max_tokens = self.config.get("deepseek.max_tokens", DEFAULT_MAX_TOKENS)
            temperature = self.config.get("deepseek.temperature", DEFAULT_TEMPERATURE)
            reply = await self.deepseek.generate_chat_response(chat_history, max_tokens=max_tokens, temperature=temperature)
            logger.debug(f"生成聊天回复成功")
            
            await self.misskey.send_message(user_id, reply)
            logger.info(f"已回复用户 {user_id}: {reply[:50]}{'...' if len(reply) > 50 else ''}")
            
            chat_history.append({"role": "assistant", "content": reply})
            
        except Exception as e:
            logger.error(f"处理消息时出错: {e}")
            logger.debug(f"处理消息详细错误: {e}", exc_info=True)
    
    async def _get_chat_history(self, user_id: str, limit: int = 5) -> List[Dict[str, str]]:
        try:
            messages = await self.misskey.get_messages(user_id, limit=limit)
            
            chat_history = []
            for msg in reversed(messages):
                if msg.get("userId") == user_id:
                    chat_history.append({"role": "user", "content": msg.get("text", "")})
                else:
                    chat_history.append({"role": "assistant", "content": msg.get("text", "")})
            
            return chat_history
            
        except Exception as e:
            logger.error(f"获取聊天历史时出错: {e}")
            return []
    
    async def _auto_post(self) -> None:
        if not self.running:
            return
            
        try:
            current_date = datetime.now(timezone.utc).date()
            if current_date != self.today:
                self._reset_daily_post_count()
            
            max_posts = self.config.get("bot.auto_post.max_posts_per_day", DEFAULT_MAX_DAILY_POSTS)
            if self.posts_today >= max_posts:
                logger.debug(f"今日发帖数量已达上限 ({max_posts})，跳过自动发帖")
                return
            
            plugin_results = await self.plugin_manager.on_auto_post()
            plugin_prompt = ""
            timestamp_override = None
            
            for result in plugin_results:
                if result and result.get("content"):
                    post_content = result.get("content")
                    visibility = result.get("visibility", self.config.get("bot.auto_post.visibility", DEFAULT_AUTO_POST_VISIBILITY))
                    await self.misskey.create_note(post_content, visibility=visibility)
                    
                    self.posts_today += 1
                    self.last_auto_post_time = datetime.now(timezone.utc)
                    
                    logger.info(f"自动发帖成功: {post_content[:50]}{'...' if len(post_content) > 50 else ''}")
                    logger.debug(f"今日发帖计数: {self.posts_today}/{max_posts}")
                    return
                elif result and result.get("modify_prompt"):
                    if result.get("plugin_prompt"):
                        plugin_prompt = result.get("plugin_prompt")
                    if result.get("timestamp"):
                        timestamp_override = result.get("timestamp")
                    logger.debug(f"插件 {result.get('plugin_name')} 请求修改提示词: {plugin_prompt}")
            
            post_prompt = self.config.get("bot.auto_post.prompt", "生成一篇有趣、有见解的社交媒体帖子。")
            max_tokens = self.config.get("deepseek.max_tokens", DEFAULT_MAX_TOKENS)
            temperature = self.config.get("deepseek.temperature", DEFAULT_TEMPERATURE)
            
            try:
                plugin_name = "system"
                for result in plugin_results:
                    if result and result.get("modify_prompt") and result.get("plugin_prompt"):
                        plugin_name = result.get("plugin_name", "unknown")
                        break
                
                post_content = await self._generate_post_with_plugin(
                    self.system_prompt, 
                    post_prompt, 
                    plugin_prompt,
                    timestamp_override,
                    plugin_name,
                    max_tokens=max_tokens, 
                    temperature=temperature
                )
            except ValueError as e:
                logger.warning(f"自动发帖失败: {e}，跳过本次发帖")
                return
            
            visibility = self.config.get("bot.auto_post.visibility", DEFAULT_AUTO_POST_VISIBILITY)
            await self.misskey.create_note(post_content, visibility=visibility)
            
            self.posts_today += 1
            self.last_auto_post_time = datetime.now(timezone.utc)
            
            logger.info(f"自动发帖成功: {post_content[:50]}{'...' if len(post_content) > 50 else ''}")
            logger.debug(f"今日发帖计数: {self.posts_today}/{max_posts}")
            
        except Exception as e:
            logger.error(f"自动发帖时出错: {e}")
    
    async def _generate_post_with_plugin(self, system_prompt: str, prompt: str, plugin_prompt: str, timestamp_override: Optional[int] = None, plugin_name: str = "system", max_tokens: int = DEFAULT_MAX_TOKENS, temperature: float = DEFAULT_TEMPERATURE) -> str:
        import time
        
        if not prompt or not prompt.strip():
            raise ValueError("缺少提示词")
        
        timestamp_min = timestamp_override if timestamp_override is not None else int(time.time() // 60)
        full_prompt = f"[{timestamp_min}] {plugin_prompt}{prompt}"
        
        return await self.deepseek.generate_text(full_prompt, system_prompt, max_tokens=max_tokens, temperature=temperature)
    
    def _reset_daily_post_count(self) -> None:
        self.posts_today = 0
        self.today = datetime.now(timezone.utc).date()
        logger.debug("已重置每日发帖计数")