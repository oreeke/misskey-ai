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
from .utils import redact_misskey_access_token

__all__ = ("ChannelType", "StreamingClient")

ChannelSpec = str | tuple[str, dict[str, Any]]


class ChannelType(str, Enum):
    MAIN = "main"
    HOME_TIMELINE = "homeTimeline"
    LOCAL_TIMELINE = "localTimeline"
    HYBRID_TIMELINE = "hybridTimeline"
    GLOBAL_TIMELINE = "globalTimeline"
    ANTENNA = "antenna"
    CHAT_USER = "chatUser"


TIMELINE_CHANNELS = frozenset(
    {
        ChannelType.HOME_TIMELINE.value,
        ChannelType.LOCAL_TIMELINE.value,
        ChannelType.HYBRID_TIMELINE.value,
        ChannelType.GLOBAL_TIMELINE.value,
    }
)

NOTE_CHANNELS = frozenset({*TIMELINE_CHANNELS, ChannelType.ANTENNA.value})
CHAT_CHANNELS = frozenset({ChannelType.CHAT_USER.value})

_EVENT_DATA_LOG_TEMPLATE = "Event data: {}"


class StreamingClient:
    def __init__(
        self, instance_url: str, access_token: str, *, log_dump_events: bool = False
    ):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.ws_connection: aiohttp.ClientWebSocketResponse | None = None
        self.transport = ClientSession
        self.log_dump_events = log_dump_events
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
        self._chat_channel_tasks: dict[str, asyncio.Task[None]] = {}
        self._chat_user_channel_ids: dict[str, str] = {}
        self._chat_channel_other_ids: dict[str, str] = {}
        self._chat_user_cache: dict[str, dict[str, Any]] = {}

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
        logger.debug("Streaming client closed")

    def on_mention(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("mention", handler)

    def on_message(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("message", handler)

    def on_reaction(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("reaction", handler)

    def on_note(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("note", handler)

    def on_notification(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        self._add_event_handler("notification", handler)

    def _add_event_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers.setdefault(event_type, []).append(handler)

    @staticmethod
    def _channel_name(spec: ChannelSpec) -> str:
        return spec[0] if isinstance(spec, tuple) else str(spec)

    @staticmethod
    def _normalize_channel_specs(
        channels: list[ChannelSpec] | None,
    ) -> list[ChannelSpec]:
        return [c for c in (channels or []) if c and StreamingClient._channel_name(c)]

    async def connect(
        self, channels: list[ChannelSpec] | None = None, *, reconnect: bool = True
    ) -> None:
        self.should_reconnect = reconnect
        specs = self._normalize_channel_specs(channels)
        retry_count = 0
        while self.should_reconnect:
            try:
                await self.connect_once(specs)
                retry_count = 0
                await self._listen_messages()
                return
            except WebSocketConnectionError:
                retry_count += 1
                if not reconnect or retry_count >= WS_MAX_RETRIES:
                    logger.error(
                        f"WebSocket connection failed; max retries reached ({WS_MAX_RETRIES})"
                    )
                    raise
                logger.debug(
                    f"WebSocket connection error; reconnecting... {retry_count}/{WS_MAX_RETRIES}"
                )
                self.running = False
                self.channels.clear()
                await asyncio.sleep(3)

    async def disconnect(self) -> None:
        self.should_reconnect = False
        self.running = False
        self._cancel_chat_channel_tasks()
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
            raise ValueError("channel name must not be empty")
        effective_params = params or {}
        existing_channels = [
            ch_id
            for ch_id, ch_info in self.channels.items()
            if ch_info.get("name") == channel_name
            and ch_info.get("params") == effective_params
        ]
        if existing_channels:
            logger.debug(
                f"Channel {channel_name} already connected: {existing_channels}"
            )
            return existing_channels[0]
        channel_id = str(uuid.uuid4())
        message = {
            "type": "connect",
            "body": {
                "channel": channel_name,
                "id": channel_id,
                "params": effective_params,
            },
        }
        if not self._ws_available:
            logger.error(
                f"WebSocket unavailable; cannot connect channel: {channel_name}"
            )
            raise WebSocketConnectionError()
        await self.ws_connection.send_json(message)
        self.channels[channel_id] = {"name": channel_name, "params": effective_params}
        logger.debug(f"Connected channel: {channel_name} (ID: {channel_id})")
        return channel_id

    async def disconnect_channel(self, channel: ChannelType | str) -> None:
        channel_name = (
            channel.value if isinstance(channel, ChannelType) else str(channel)
        )
        if not channel_name:
            raise ValueError("channel name must not be empty")
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
        logger.debug(f"Disconnected channel: {channel_name}")

    async def disconnect_channel_id(self, channel_id: str) -> None:
        if not channel_id:
            return
        if channel_id in self.channels and self._ws_available:
            await self.ws_connection.send_json(
                {"type": "disconnect", "body": {"id": channel_id}}
            )
        self.channels.pop(channel_id, None)

    async def send_channel_message(
        self,
        channel: ChannelType | str,
        event_type: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> None:
        channel_name = (
            channel.value if isinstance(channel, ChannelType) else str(channel)
        )
        if not channel_name or not event_type:
            return
        channel_id = self._find_channel_id(channel_name, params or {})
        if not channel_id:
            return
        await self._send_channel_message(channel_id, event_type, body or {})

    async def _send_channel_message(
        self, channel_id: str, event_type: str, body: dict[str, Any]
    ) -> None:
        if not self._ws_available:
            return
        await self.ws_connection.send_json(
            {"type": "ch", "body": {"id": channel_id, "type": event_type, "body": body}}
        )

    def _find_channel_id(self, channel_name: str, params: dict[str, Any]) -> str | None:
        for ch_id, ch_info in self.channels.items():
            if ch_info.get("name") == channel_name and ch_info.get("params") == params:
                return ch_id
        return None

    async def connect_once(self, channels: list[ChannelSpec] | None = None) -> None:
        if self.running:
            return
        self.running = True
        self._ensure_workers_started()
        await self._connect_websocket()
        requested = self._normalize_channel_specs(channels)
        if not any(self._channel_name(s) == ChannelType.MAIN.value for s in requested):
            requested.insert(0, ChannelType.MAIN.value)
        for spec in requested:
            channel = self._channel_name(spec)
            params = spec[1] if isinstance(spec, tuple) else None
            try:
                await self.connect_channel(ChannelType(channel), params)
            except ValueError:
                await self.connect_channel(channel, params)
        if self._first_connection:
            logger.info("Streaming client started")
            self._first_connection = False

    async def _connect_websocket(self) -> None:
        base_ws_url = self.instance_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url = f"{base_ws_url}/streaming?i={self.access_token}"
        safe_url = f"{base_ws_url}/streaming"
        try:
            self.ws_connection = await self.transport.ws_connect(ws_url)
            logger.debug(f"WebSocket connected: {safe_url}")
        except (aiohttp.ClientError, OSError) as e:
            await self._cleanup_failed_connection()
            error_msg = redact_misskey_access_token(str(e))
            logger.error(f"WebSocket connection failed: {error_msg}")
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
                logger.error(f"Failed to parse message: {e}")
                continue

    async def _close_websocket(self) -> None:
        if self.ws_connection and not self.ws_connection.closed:
            await self.ws_connection.close()
        self.ws_connection = None

    async def _cleanup_failed_connection(self) -> None:
        try:
            await self._close_websocket()
        except Exception as e:
            logger.error(f"Error cleaning up failed connection: {e}")

    async def _disconnect_all_channels(self) -> None:
        for channel_id in self.channels:
            if self._ws_available:
                try:
                    message = {"type": "disconnect", "body": {"id": channel_id}}
                    await self.ws_connection.send_json(message)
                except Exception as e:
                    logger.warning(f"Error disconnecting channel {channel_id}: {e}")
        self.channels.clear()

    async def _process_message(
        self, data: dict[str, Any], raw_message: str | None = None
    ) -> None:
        if not data or not isinstance(data, dict):
            logger.debug(f"Invalid message format; skipping: {raw_message}")
            return
        message_type = data.get("type")
        body = data.get("body", {})
        if message_type == "channel":
            await self._handle_channel_message(body)
        else:
            logger.debug(f"Unknown message type received: {message_type}")

    async def _handle_channel_message(self, body: dict[str, Any]) -> None:
        channel_id = body.get("id")
        if channel_id not in self.channels:
            logger.debug(f"Message received for unknown channel: {channel_id}")
            return
        channel_info = self.channels[channel_id]
        channel_name = channel_info.get("name", "unknown")
        outer_type = body.get("type")
        event_body = body.get("body")
        if not isinstance(outer_type, str) or not outer_type:
            logger.debug(
                f"Received {channel_name} data without standard event type; skipping (channel_id={channel_id})"
            )
            if self.log_dump_events:
                logger.opt(lazy=True).debug(
                    _EVENT_DATA_LOG_TEMPLATE,
                    lambda: json.dumps(body, ensure_ascii=False, indent=2),
                )
            return
        if event_body is None:
            event_body = {}
        event_data: dict[str, Any] = {"type": outer_type, "body": event_body}
        event_type, event_data = self._normalize_channel_event(channel_name, event_data)
        if isinstance(event_data, dict) and "streamingChannelId" not in event_data:
            event_data["streamingChannelId"] = channel_id
        event_id = self._extract_event_id(event_data, event_type)
        if self._is_duplicate_event(event_id, event_type):
            return
        self._track_event(event_id, event_type)
        if event_type:
            logger.debug(
                f"Received {channel_name} event: {event_type} (channel_id={channel_id}, event_id={event_id})"
            )
        await self._enqueue_event(channel_name, event_data)

    def _normalize_channel_event(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        event_type = event_data.get("type")
        if channel_name == ChannelType.MAIN.value:
            return self._normalize_main_channel_event(event_type, event_data)
        if channel_name in CHAT_CHANNELS:
            return self._normalize_chat_channel_event(event_type, event_data)
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

    def _normalize_main_channel_event(
        self, event_type: Any, event_data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if not isinstance(event_type, str) or not event_type:
            return event_type, event_data
        payload = event_data.get("body")
        if not isinstance(payload, dict):
            return event_type, event_data

        normalizers = {
            "mention": lambda: self._wrap_note_event("mention", payload),
            "reply": lambda: self._wrap_note_event("reply", payload),
            "newChatMessage": lambda: self._wrap_new_chat_message(payload),
            "notification": lambda: self._normalize_notification_event(payload),
        }
        normalizer = normalizers.get(event_type)
        return normalizer() if normalizer else (event_type, event_data)

    def _normalize_chat_channel_event(
        self, event_type: Any, event_data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if event_type != "message":
            return event_type, event_data
        payload = event_data.get("body")
        if not isinstance(payload, dict):
            return event_type, event_data
        normalized = dict(payload)
        normalized.setdefault("type", "message")
        msg_id = normalized.get("id")
        if isinstance(msg_id, str) and msg_id:
            normalized["id"] = msg_id
        return "message", normalized

    @staticmethod
    def _extract_dict(container: dict[str, Any], key: str) -> dict[str, Any] | None:
        value = container.get(key)
        return value if isinstance(value, dict) else None

    def _wrap_note_event(
        self, event_type: str, note: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        note_id = note.get("id") if isinstance(note.get("id"), str) else None
        wrapped: dict[str, Any] = {"type": event_type, "note": note}
        if note_id:
            wrapped["id"] = note_id
        return event_type, wrapped

    @staticmethod
    def _wrap_new_chat_message(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        normalized = dict(message)
        normalized["type"] = "newChatMessage"
        return "newChatMessage", normalized

    @staticmethod
    def _wrap_notification(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        wrapped: dict[str, Any] = {"type": "notification", "notification": payload}
        notification_id = payload.get("id")
        if isinstance(notification_id, str) and notification_id:
            wrapped["id"] = notification_id
        return "notification", wrapped

    def _normalize_notification_event(
        self, payload: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        inner_type = payload.get("type")
        if not isinstance(inner_type, str) or not inner_type:
            return self._wrap_notification(payload)

        if inner_type == "reaction":
            return "reaction", payload
        return self._wrap_notification(payload)

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
            logger.warning(
                f"Event queue congested; dropping event: {event_type} (id={event_id})"
            )

    async def _worker_loop(self) -> None:
        while True:
            item = await self._event_queue.get()
            if item is None:
                return
            channel_name, event_data = item
            try:
                await self._dispatch_event(channel_name, event_data)
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.exception(f"Failed to process event: {e}")

    async def _dispatch_event(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> None:
        event_type = event_data.get("type")
        if not event_type:
            self._handle_no_event_type(channel_name, event_data)
        else:
            await self._handle_typed_event(channel_name, event_type, event_data)

    def _handle_no_event_type(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> None:
        event_id = event_data.get("id", "unknown")
        logger.debug(
            f"Received data without event type - channel: {channel_name}, event_id={event_id}"
        )
        if self.log_dump_events:
            logger.opt(lazy=True).debug(
                _EVENT_DATA_LOG_TEMPLATE,
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
        if channel_name in CHAT_CHANNELS:
            await self._handle_chat_channel_event(channel_name, event_type, event_data)
            return
        if channel_name in NOTE_CHANNELS:
            await self._handle_note_channel_event(channel_name, event_type, event_data)

    async def _handle_main_channel_event(
        self, event_type: str, event_data: dict[str, Any]
    ) -> None:
        handler_map = {
            "mention": "mention",
            "reply": "mention",
            "newChatMessage": "message",
            "reaction": "reaction",
            "notification": "notification",
        }
        if event_type == "newChatMessage":
            await self._ensure_chat_user_stream(event_data)
        if event_type in handler_map:
            await self._call_handlers(handler_map[event_type], event_data)
        else:
            logger.debug(f"Unknown main channel event type: {event_type}")
            if self.log_dump_events:
                logger.opt(lazy=True).debug(
                    _EVENT_DATA_LOG_TEMPLATE,
                    lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
                )

    async def _handle_chat_channel_event(
        self, channel_name: str, event_type: str, event_data: dict[str, Any]
    ) -> None:
        if event_type != "message":
            logger.debug(f"Unknown {channel_name} channel event type: {event_type}")
            if self.log_dump_events:
                logger.opt(lazy=True).debug(
                    _EVENT_DATA_LOG_TEMPLATE,
                    lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
                )
            return
        message = dict(event_data)
        from_user_id = message.get("fromUserId")
        if (
            isinstance(from_user_id, str)
            and from_user_id
            and "fromUser" not in message
            and from_user_id in self._chat_user_cache
        ):
            message["fromUser"] = self._chat_user_cache[from_user_id]
        channel_id = message.get("streamingChannelId")
        msg_id = message.get("id")
        if (
            isinstance(channel_id, str)
            and channel_id
            and isinstance(msg_id, str)
            and msg_id
        ):
            await self._send_channel_message(channel_id, "read", {"id": msg_id})
            self._refresh_chat_channel_timer(channel_id)
        await self._call_handlers("message", message)

    async def _ensure_chat_user_stream(self, message: dict[str, Any]) -> None:
        other_id = message.get("fromUserId")
        if not isinstance(other_id, str) or not other_id:
            return
        from_user = message.get("fromUser")
        if isinstance(from_user, dict):
            self._chat_user_cache[other_id] = from_user
        try:
            channel_id = await self.connect_channel(
                ChannelType.CHAT_USER, {"otherId": other_id}
            )
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.debug(f"Failed to connect chatUser channel for {other_id}: {e}")
            return
        self._chat_user_channel_ids[other_id] = channel_id
        self._chat_channel_other_ids[channel_id] = other_id
        if task := self._chat_channel_tasks.get(channel_id):
            task.cancel()
        self._chat_channel_tasks[channel_id] = asyncio.create_task(
            self._disconnect_chat_channel_later(other_id, channel_id),
            name=f"chatUser-disconnect-{other_id}",
        )

    async def _disconnect_chat_channel_later(
        self, other_id: str, channel_id: str
    ) -> None:
        try:
            await asyncio.sleep(120)
            await self.disconnect_channel_id(channel_id)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.debug(f"Failed to disconnect chatUser channel {channel_id}: {e}")
        finally:
            if self._chat_user_channel_ids.get(other_id) == channel_id:
                self._chat_user_channel_ids.pop(other_id, None)
            self._chat_channel_other_ids.pop(channel_id, None)
            self._chat_channel_tasks.pop(channel_id, None)

    def _refresh_chat_channel_timer(self, channel_id: str) -> None:
        other_id = self._chat_channel_other_ids.get(channel_id)
        if not other_id:
            return
        if task := self._chat_channel_tasks.get(channel_id):
            task.cancel()
        self._chat_channel_tasks[channel_id] = asyncio.create_task(
            self._disconnect_chat_channel_later(other_id, channel_id),
            name=f"chatUser-disconnect-{other_id}",
        )

    def _cancel_chat_channel_tasks(self) -> None:
        tasks = list(self._chat_channel_tasks.values())
        self._chat_channel_tasks.clear()
        self._chat_user_channel_ids.clear()
        self._chat_channel_other_ids.clear()
        self._chat_user_cache.clear()
        for task in tasks:
            task.cancel()

    async def _handle_note_channel_event(
        self, channel_name: str, event_type: str, event_data: dict[str, Any]
    ) -> None:
        if event_type != "note":
            logger.debug(f"Unknown {channel_name} channel event type: {event_type}")
            if self.log_dump_events:
                logger.opt(lazy=True).debug(
                    _EVENT_DATA_LOG_TEMPLATE,
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
        logger.debug(f"Received {channel_name} note")
        if channel_name == ChannelType.ANTENNA.value:
            logger.debug(f"Antenna note received: {payload.get('id', 'unknown')}")
        if self.log_dump_events:
            logger.opt(lazy=True).debug(
                _EVENT_DATA_LOG_TEMPLATE,
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
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.exception(f"Event handler failed ({event_type}): {e}")

    def _is_duplicate_event(self, event_id: str | None, event_type: str | None) -> bool:
        dedup_key = self._event_dedup_key(event_id, event_type)
        if dedup_key and dedup_key in self.processed_events:
            logger.debug(
                f"Duplicate event detected; skipping - {event_type}, event_id={event_id}"
            )
            return True
        return False

    def _track_event(self, event_id: str | None, event_type: str | None) -> None:
        self._track_dedup_key(self._event_dedup_key(event_id, event_type))

    def _track_dedup_key(self, dedup_key: str | None) -> None:
        if dedup_key:
            self.processed_events[dedup_key] = True

    @staticmethod
    def _event_dedup_key(event_id: str | None, event_type: str | None) -> str | None:
        if not event_id:
            return None
        if not event_type:
            return event_id
        if event_type in {"newChatMessage", "message"}:
            return f"chatMessage:{event_id}"
        return f"{event_type}:{event_id}"
