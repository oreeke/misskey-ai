import asyncio
import json
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from cachetools import TTLCache
from loguru import logger

from ...shared.constants import (
    STREAM_DEDUP_CACHE_MAX,
    STREAM_DEDUP_CACHE_TTL,
    STREAM_QUEUE_MAX,
    STREAM_QUEUE_PUT_TIMEOUT,
    STREAM_WORKERS,
)
from ...shared.exceptions import WebSocketConnectionError, WebSocketReconnectError
from ...shared.utils import redact_misskey_access_token
from .channels import ChannelSpec, ChannelType
from .events import _StreamingEventsMixin
from .transport import client_session

__all__ = ("StreamingClient",)


class StreamingClient(_StreamingEventsMixin):
    def __init__(
        self,
        instance_url: str,
        access_token: str,
        *,
        log_dump_events: bool = False,
    ):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.ws_connection: aiohttp.ClientWebSocketResponse | None = None
        self.transport = client_session
        self.log_dump_events = log_dump_events
        self.state = "initializing"
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
        self._send_buffer: deque[dict[str, Any]] = deque()
        self._ws_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

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
        self._send_buffer.clear()
        logger.debug("Streaming client closed")

    def on_mention(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("mention", handler)

    def on_message(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._add_event_handler("message", handler)

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
        await self.connect_once(specs)
        retry_delay = 1.0
        while self.should_reconnect and self.running:
            try:
                await self._listen_messages()
                return
            except WebSocketConnectionError:
                if not reconnect:
                    raise
                self.state = "reconnecting"
                logger.debug(f"WebSocket disconnected; reconnecting in {retry_delay}s")
                try:
                    await self._reconnect_with_backoff(retry_delay)
                    retry_delay = 1.0
                except WebSocketConnectionError:
                    retry_delay = min(retry_delay * 2, 30.0)

    async def disconnect(self) -> None:
        self.should_reconnect = False
        self.running = False
        self._cancel_chat_channel_tasks()
        await self._disconnect_all_channels()
        await self._close_websocket()
        self.processed_events.clear()
        self._send_buffer.clear()
        self.state = "disconnected"

    @property
    def _ws_available(self) -> bool:
        return self.ws_connection and not self.ws_connection.closed

    def _buffer_outgoing(self, message: dict[str, Any]) -> None:
        if len(self._send_buffer) >= STREAM_QUEUE_MAX:
            try:
                self._send_buffer.popleft()
            except IndexError:
                pass
        self._send_buffer.append(message)

    async def _send_or_buffer(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            if not self._ws_available:
                self._buffer_outgoing(message)
                return
            try:
                await self.ws_connection.send_json(message)
            except (aiohttp.ClientError, OSError) as e:
                self._buffer_outgoing(message)
                await self._close_websocket()
                error_msg = redact_misskey_access_token(str(e))
                logger.debug(f"WebSocket send failed; reconnecting: {error_msg}")
                return

    async def _send_control(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            if not self._ws_available:
                raise WebSocketReconnectError()
            try:
                await self.ws_connection.send_json(message)
            except (aiohttp.ClientError, OSError) as e:
                await self._close_websocket()
                error_msg = redact_misskey_access_token(str(e))
                logger.debug(f"WebSocket send failed; reconnecting: {error_msg}")
                raise WebSocketReconnectError()

    async def _flush_send_buffer(self) -> None:
        while self._send_buffer and self._ws_available:
            message = self._send_buffer.popleft()
            await self._send_control(message)

    async def _reconnect_with_backoff(self, delay_seconds: float) -> None:
        await self._close_websocket()
        await asyncio.sleep(delay_seconds)
        await self._connect_websocket()
        await self._resubscribe_channels()
        await self._flush_send_buffer()
        self.state = "connected"

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
        self.channels[channel_id] = {"name": channel_name, "params": effective_params}
        if self._ws_available:
            await self._send_control(
                {
                    "type": "connect",
                    "body": {
                        "channel": channel_name,
                        "id": channel_id,
                        "params": effective_params,
                    },
                }
            )
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
                try:
                    await self._send_control(
                        {"type": "disconnect", "body": {"id": channel_id}}
                    )
                except WebSocketConnectionError:
                    pass
            self.channels.pop(channel_id, None)
        logger.debug(f"Disconnected channel: {channel_name}")

    async def disconnect_channel_id(self, channel_id: str) -> None:
        if not channel_id:
            return
        if channel_id in self.channels and self._ws_available:
            try:
                await self._send_control(
                    {"type": "disconnect", "body": {"id": channel_id}}
                )
            except WebSocketConnectionError:
                pass
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
        await self._send_or_buffer(
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
        try:
            await self._connect_websocket()
            await self._resubscribe_channels()
            await self._flush_send_buffer()
            self.state = "connected"
        except WebSocketConnectionError:
            self.state = "reconnecting"
        if self._first_connection:
            logger.info("Streaming client started")
            self._first_connection = False

    async def _connect_websocket(self) -> None:
        async with self._ws_lock:
            if self._ws_available:
                return
        raw = self.instance_url.strip().rstrip("/")
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in {"https", "http"}:
            raise ValueError("Unsupported instance URL scheme")
        ws_scheme = "wss" if scheme == "https" else "ws"
        base_ws_url = urlunsplit(
            (ws_scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        ).rstrip("/")
        qs = urlencode({"i": self.access_token})
        ws_url = f"{base_ws_url}/streaming?{qs}"
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
                msg = await asyncio.wait_for(self.ws_connection.receive(), timeout=10)
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise WebSocketReconnectError()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self._process_message(data, msg.data)
            except TimeoutError:
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
        async with self._ws_lock:
            if self.ws_connection and not self.ws_connection.closed:
                try:
                    await self.ws_connection.close()
                except Exception:
                    pass
            self.ws_connection = None

    async def _resubscribe_channels(self) -> None:
        for channel_id, info in self.channels.items():
            channel_name = info.get("name")
            if not isinstance(channel_name, str) or not channel_name:
                continue
            params = info.get("params") or {}
            await self._send_control(
                {
                    "type": "connect",
                    "body": {
                        "channel": channel_name,
                        "id": channel_id,
                        "params": params,
                    },
                }
            )

    async def _cleanup_failed_connection(self) -> None:
        try:
            await self._close_websocket()
        except Exception as e:
            logger.error(f"Error cleaning up failed connection: {e}")

    async def _disconnect_all_channels(self) -> None:
        for channel_id in self.channels:
            if self._ws_available:
                try:
                    await self._send_control(
                        {"type": "disconnect", "body": {"id": channel_id}}
                    )
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
