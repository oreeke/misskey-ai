import asyncio
import json
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from loguru import logger

from ...shared.constants import STREAM_QUEUE_MAX
from ...shared.exceptions import WebSocketConnectionError, WebSocketReconnectError
from ...shared.utils import redact_misskey_access_token

__all__ = ("_StreamingSocketMixin",)


class _StreamingSocketMixin:
    @property
    def _ws_available(self) -> bool:
        return self.ws_connection is not None and not self.ws_connection.closed

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
                raise WebSocketReconnectError() from e

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

    async def _connect_websocket(self) -> None:
        async with self._ws_lock:
            if self._ws_available:
                return
            task = getattr(self, "_connect_task", None)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._connect_websocket_inner(),
                    name="stream-ws-connect",
                )
                setattr(self, "_connect_task", task)
        try:
            await task
        finally:
            if task.done():
                async with self._ws_lock:
                    if getattr(self, "_connect_task", None) is task:
                        setattr(self, "_connect_task", None)

    async def _connect_websocket_inner(self) -> None:
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
            ws = await self.transport.ws_connect(ws_url)
            async with self._ws_lock:
                if self._ws_available:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                self.ws_connection = ws
            logger.debug(f"WebSocket connected: {safe_url}")
        except (aiohttp.ClientError, OSError) as e:
            await self._cleanup_failed_connection()
            error_msg = redact_misskey_access_token(str(e))
            logger.error(f"WebSocket connection failed: {error_msg}")
            raise WebSocketConnectionError() from e

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
            ) as e:
                raise WebSocketReconnectError() from e
            except (ValueError, TypeError, AttributeError, KeyError) as e:
                logger.error(f"Failed to parse message: {e}")

    async def _close_websocket(self) -> None:
        async with self._ws_lock:
            if self.ws_connection and not self.ws_connection.closed:
                try:
                    await self.ws_connection.close()
                except Exception:
                    pass
            self.ws_connection = None

    async def _cleanup_failed_connection(self) -> None:
        try:
            await self._close_websocket()
        except Exception as e:
            logger.error(f"Error cleaning up failed connection: {e}")
