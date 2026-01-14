import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from cachetools import TTLCache
from loguru import logger

from ...shared.constants import (
    RECEIVE_TIMEOUT,
    STREAM_DEDUP_CACHE_MAX,
    STREAM_DEDUP_CACHE_TTL,
    STREAM_QUEUE_MAX,
    STREAM_QUEUE_PUT_TIMEOUT,
    STREAM_WORKERS,
    WS_MAX_RETRIES,
)
from ...shared.exceptions import WebSocketConnectionError, WebSocketReconnectError
from ...shared.utils import redact_misskey_access_token
from .channels import ChannelSpec, ChannelType
from .events import _StreamingEventsMixin
from .transport import ClientSession

__all__ = ("StreamingClient",)


class StreamingClient(_StreamingEventsMixin):
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
        raw = self.instance_url.strip().rstrip("/")
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme != "https":
            raise ValueError("Insecure instance URL scheme is not allowed")
        base_ws_url = urlunsplit(
            ("wss", parsed.netloc, parsed.path.rstrip("/"), "", "")
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
