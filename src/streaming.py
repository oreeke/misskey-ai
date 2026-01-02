import asyncio
import json
import uuid
from enum import Enum
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from cachetools import TTLCache
from loguru import logger

from .constants import (
    RECEIVE_TIMEOUT,
    STREAM_QUEUE_MAX,
    STREAM_QUEUE_PUT_TIMEOUT,
    STREAM_WORKERS,
    STREAM_DEDUP_CACHE_MAX,
    STREAM_DEDUP_CACHE_TTL,
    WS_MAX_RETRIES,
)
from .exceptions import WebSocketConnectionError, WebSocketReconnectError
from .transport import ClientSession

__all__ = ("ChannelType", "StreamingClient")


class ChannelType(str, Enum):
    MAIN = "main"
    HOME_TIMELINE = "homeTimeline"
    LOCAL_TIMELINE = "localTimeline"
    HYBRID_TIMELINE = "hybridTimeline"
    GLOBAL_TIMELINE = "globalTimeline"


TIMELINE_CHANNELS = frozenset(
    {
        ChannelType.HOME_TIMELINE.value,
        ChannelType.LOCAL_TIMELINE.value,
        ChannelType.HYBRID_TIMELINE.value,
        ChannelType.GLOBAL_TIMELINE.value,
    }
)


class StreamingClient:
    def __init__(self, instance_url: str, access_token: str):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.ws_connection: aiohttp.ClientWebSocketResponse | None = None
        self.transport = ClientSession
        self.channels: dict[str, dict[str, Any]] = {}
        self.event_handlers: dict[str, list[Callable]] = {}
        self.processed_events = TTLCache(
            maxsize=STREAM_DEDUP_CACHE_MAX, ttl=STREAM_DEDUP_CACHE_TTL
        )
        self._event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = (
            asyncio.Queue(maxsize=STREAM_QUEUE_MAX)
        )
        self._worker_count = STREAM_WORKERS
        self._queue_put_timeout = STREAM_QUEUE_PUT_TIMEOUT
        self._workers: list[asyncio.Task[None]] = []
        self.running = False
        self.should_reconnect = True
        self._first_connection = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def close(self) -> None:
        await self.disconnect()
        await self._stop_workers()
        await self._close_websocket()
        await self.transport.close_session(silent=True)
        self.processed_events.clear()
        logger.debug("Streaming 客户端已关闭")

    def on_mention(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("mention", handler)

    def on_message(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("message", handler)

    def on_reaction(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("reaction", handler)

    def on_follow(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("follow", handler)

    def on_note(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("note", handler)

    def _add_event_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers.setdefault(event_type, []).append(handler)

    @staticmethod
    def _looks_like_note(data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        return (
            isinstance(data.get("id"), str)
            and isinstance(data.get("createdAt"), str)
            and isinstance(data.get("userId"), str)
            and isinstance(data.get("user"), dict)
        )

    async def connect(
        self, channels: list[str] | None = None, *, reconnect: bool = True
    ) -> None:
        self.should_reconnect = reconnect
        retry_count = 0
        while self.should_reconnect:
            try:
                await self.connect_once(channels)
                retry_count = 0
                await self._listen_messages()
                return
            except WebSocketConnectionError:
                retry_count += 1
                if not reconnect or retry_count >= WS_MAX_RETRIES:
                    logger.error(
                        f"WebSocket 连接失败，已达最大重试次数 {WS_MAX_RETRIES}"
                    )
                    raise
                logger.debug(
                    f"WebSocket 连接异常，重新连接... {retry_count}/{WS_MAX_RETRIES}"
                )
                self.running = False
                self.channels.clear()
                await asyncio.sleep(3)

    async def disconnect(self) -> None:
        self.should_reconnect = False
        self.running = False
        await self._disconnect_all_channels()
        await self._close_websocket()
        self.processed_events.clear()

    @property
    def _ws_available(self) -> bool:
        return self.ws_connection and not self.ws_connection.closed

    async def connect_channel(
        self, channel: ChannelType | str, params: dict[str, Any] | None = None
    ) -> str:
        channel_name = (
            channel.value if isinstance(channel, ChannelType) else str(channel)
        )
        if not channel_name:
            raise ValueError("频道名称不能为空")
        existing_channels = [
            ch_id
            for ch_id, ch_info in self.channels.items()
            if ch_info.get("name") == channel_name
        ]
        if existing_channels:
            logger.warning(f"频道类型 {channel_name} 已存在连接: {existing_channels}")
            return existing_channels[0]
        channel_id = str(uuid.uuid4())
        message = {
            "type": "connect",
            "body": {
                "channel": channel_name,
                "id": channel_id,
                "params": params or {},
            },
        }
        if not self._ws_available:
            logger.error(f"WebSocket 连接不可用，无法连接频道: {channel_name}")
            raise WebSocketConnectionError()
        await self.ws_connection.send_json(message)
        self.channels[channel_id] = {"name": channel_name, "params": params or {}}
        logger.debug(f"已连接频道: {channel_name} (ID: {channel_id})")
        return channel_id

    async def disconnect_channel(self, channel: ChannelType | str) -> None:
        channel_name = (
            channel.value if isinstance(channel, ChannelType) else str(channel)
        )
        if not channel_name:
            raise ValueError("频道名称不能为空")
        channels_to_remove = [
            ch_id
            for ch_id, ch_info in self.channels.items()
            if ch_info.get("name") == channel_name
        ]
        for channel_id in channels_to_remove:
            if self._ws_available:
                message = {"type": "disconnect", "body": {"id": channel_id}}
                await self.ws_connection.send_json(message)
            del self.channels[channel_id]
        logger.debug(f"已断开频道连接: {channel_name}")

    async def connect_once(self, channels: list[str] | None = None) -> None:
        if self.running:
            return
        self.running = True
        self._ensure_workers_started()
        await self._connect_websocket()
        requested = [c for c in (channels or []) if c]
        if ChannelType.MAIN.value not in requested:
            requested.insert(0, ChannelType.MAIN.value)
        for channel in requested:
            try:
                await self.connect_channel(ChannelType(channel))
            except ValueError:
                await self.connect_channel(channel)
        if self._first_connection:
            logger.info("Streaming 客户端已启动")
            self._first_connection = False

    async def _connect_websocket(self) -> None:
        base_ws_url = self.instance_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url = f"{base_ws_url}/streaming?i={self.access_token}"
        safe_url = f"{base_ws_url}/streaming"
        try:
            self.ws_connection = await self.transport.ws_connect(ws_url)
            logger.debug(f"WebSocket 连接成功: {safe_url}")
        except (aiohttp.ClientError, OSError) as e:
            await self._cleanup_failed_connection()
            logger.error(f"WebSocket 连接失败: {e}")
            raise WebSocketConnectionError()

    async def _listen_messages(self) -> None:
        while self.running:
            if not self._ws_available:
                raise WebSocketReconnectError()
            try:
                msg = await asyncio.wait_for(
                    self.ws_connection.receive(), timeout=RECEIVE_TIMEOUT
                )
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise WebSocketReconnectError()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self._process_message(data, msg.data)
            except asyncio.TimeoutError:
                continue
            except (
                aiohttp.ClientError,
                json.JSONDecodeError,
                OSError,
            ):
                raise WebSocketReconnectError()
            except (ValueError, TypeError, AttributeError, KeyError) as e:
                logger.error(f"解析消息失败: {e}")
                continue

    async def _close_websocket(self) -> None:
        if self.ws_connection and not self.ws_connection.closed:
            await self.ws_connection.close()
        self.ws_connection = None

    async def _cleanup_failed_connection(self) -> None:
        try:
            await self._close_websocket()
        except (OSError, ValueError) as e:
            logger.error(f"清理失败连接时出错: {e}")

    async def _disconnect_all_channels(self) -> None:
        for channel_id in list(self.channels.keys()):
            if self._ws_available:
                try:
                    message = {"type": "disconnect", "body": {"id": channel_id}}
                    await self.ws_connection.send_json(message)
                except (OSError, ValueError) as e:
                    logger.warning(f"断开频道 {channel_id} 时出错: {e}")
        self.channels.clear()

    async def _process_message(
        self, data: dict[str, Any], raw_message: str | None = None
    ) -> None:
        if not data or not isinstance(data, dict):
            logger.debug(f"收到无效消息格式，跳过处理: {raw_message}")
            return
        message_type = data.get("type")
        body = data.get("body", {})
        if message_type == "channel":
            await self._handle_channel_message(body)
        else:
            logger.debug(f"收到未知消息类型: {message_type}")

    async def _handle_channel_message(self, body: dict[str, Any]) -> None:
        channel_id = body.get("id")
        if channel_id not in self.channels:
            logger.debug(f"收到未知频道的消息: {channel_id}")
            return
        channel_info = self.channels[channel_id]
        channel_name = channel_info.get("name", "unknown")
        event_data = body.get("body") or {}
        event_type, event_data = self._normalize_channel_event(channel_name, event_data)
        event_id = self._extract_event_id(event_data, event_type)
        if self._is_duplicate_event(event_id, event_type):
            return
        self._track_event(event_id)
        if event_type:
            logger.debug(
                f"收到 {channel_name} 频道事件: {event_type} (频道 ID: {channel_id}, 事件 ID: {event_id})"
            )
        await self._enqueue_event(channel_name, event_data)

    @staticmethod
    def _is_chat_payload(event_data: dict[str, Any]) -> bool:
        has_users = bool(event_data.get("fromUserId") and event_data.get("toUserId"))
        has_content = (
            event_data.get("text") is not None
            or event_data.get("fileId") is not None
            or event_data.get("file") is not None
        )
        return has_users and has_content

    def _normalize_channel_event(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        event_type = event_data.get("type")
        if (
            not event_type
            and channel_name in TIMELINE_CHANNELS
            and self._looks_like_note(event_data)
        ):
            wrapped = {"type": "note", "body": event_data}
            return "note", wrapped
        if not event_type and self._is_chat_payload(event_data):
            event_data["type"] = "chat"
            return "chat", event_data
        return event_type, event_data

    @staticmethod
    def _extract_event_id(
        event_data: dict[str, Any], event_type: str | None
    ) -> str | None:
        event_id = event_data.get("id")
        if event_id or event_type != "note":
            return event_id
        inner_id = (event_data.get("body") or {}).get("id")
        return inner_id if isinstance(inner_id, str) else None

    def _ensure_workers_started(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker_loop(), name=f"stream-worker-{i}")
            for i in range(self._worker_count)
        ]

    async def _stop_workers(self) -> None:
        if not self._workers:
            return
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for _ in self._workers:
            await self._event_queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _enqueue_event(self, channel_name: str, event_data: dict[str, Any]):
        try:
            await asyncio.wait_for(
                self._event_queue.put((channel_name, event_data)),
                timeout=self._queue_put_timeout,
            )
        except asyncio.TimeoutError:
            event_id = event_data.get("id", "unknown")
            event_type = event_data.get("type", "unknown")
            logger.warning(f"事件队列拥塞，丢弃事件: {event_type} (ID: {event_id})")

    async def _worker_loop(self) -> None:
        while True:
            item = await self._event_queue.get()
            if item is None:
                return
            channel_name, event_data = item
            try:
                await self._dispatch_event(channel_name, event_data)
            except (ValueError, TypeError, AttributeError, KeyError) as e:
                logger.error(f"处理事件失败: {e}")
            except Exception as e:
                logger.exception(f"处理事件失败: {e}")

    async def _dispatch_event(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> None:
        event_type = event_data.get("type")
        if not event_type:
            await self._handle_no_event_type(channel_name, event_data)
        else:
            await self._handle_typed_event(channel_name, event_type, event_data)

    async def _handle_no_event_type(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> None:
        event_id = event_data.get("id", "unknown")
        logger.debug(
            f"收到无事件类型的数据 - 频道: {channel_name}, 事件 ID: {event_id}"
        )
        logger.debug(f"数据结构: {list(event_data.keys())}")
        logger.opt(lazy=True).debug(
            "事件数据: {}",
            lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
        )

    async def _handle_typed_event(
        self,
        channel_name: str,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        if channel_name == ChannelType.MAIN.value:
            await self._handle_main_channel_event(event_type, event_data)
            return
        if channel_name in TIMELINE_CHANNELS:
            await self._handle_timeline_channel_event(
                channel_name, event_type, event_data
            )

    async def _handle_main_channel_event(
        self, event_type: str, event_data: dict[str, Any]
    ) -> None:
        handler_map = {
            "mention": "mention",
            "reply": "mention",
            "chat": "message",
            "reaction": "reaction",
            "follow": "follow",
        }
        if event_type in handler_map:
            await self._call_handlers(handler_map[event_type], event_data)
        else:
            logger.debug(f"收到未知类型的 main 频道事件: {event_type}")
            logger.debug(f"数据结构: {list(event_data.keys())}")
            logger.opt(lazy=True).debug(
                "事件数据: {}",
                lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
            )

    async def _handle_timeline_channel_event(
        self, channel_name: str, event_type: str, event_data: dict[str, Any]
    ) -> None:
        if event_type != "note":
            logger.debug(f"收到未知类型的 {channel_name} 频道事件: {event_type}")
            logger.debug(f"数据结构: {list(event_data.keys())}")
            logger.opt(lazy=True).debug(
                "事件数据: {}",
                lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
            )
            return
        payload = event_data.get("body")
        if not isinstance(payload, dict):
            payload = event_data
        else:
            payload = dict(payload)
        if isinstance(payload, dict) and "streamingChannel" not in payload:
            payload["streamingChannel"] = channel_name
        logger.debug(f"收到 {channel_name} 频道 note")
        logger.debug(f"数据结构: {list(payload.keys())}")
        logger.opt(lazy=True).debug(
            "事件数据: {}",
            lambda: json.dumps(payload, ensure_ascii=False, indent=2),
        )
        await self._call_handlers("note", payload)

    async def _call_handlers(self, event_type: str, data: dict[str, Any]) -> None:
        handlers = self.event_handlers.get(event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except (ValueError, OSError) as e:
                logger.error(f"事件处理器执行失败 ({event_type}): {e}")
            except Exception as e:
                logger.exception(f"事件处理器执行失败 ({event_type}): {e}")

    def _is_duplicate_event(self, event_id: str | None, event_type: str | None) -> bool:
        if event_id and event_id in self.processed_events:
            logger.debug(
                f"检测到重复事件，跳过处理 - {event_type}, 事件 ID: {event_id}"
            )
            return True
        return False

    def _track_event(self, event_id: str | None) -> None:
        if event_id:
            self.processed_events[event_id] = True
