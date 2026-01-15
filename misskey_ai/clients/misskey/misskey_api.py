import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import aiohttp
from loguru import logger

from ...shared.constants import (
    API_MAX_RETRIES,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_NO_CONTENT,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
    MISSKEY_MAX_CONCURRENCY,
)
from ...shared.exceptions import (
    APIBadRequestError,
    APIConnectionError,
    APIRateLimitError,
    AuthenticationError,
)
from ...shared.utils import retry_async
from .drive import MisskeyDrive
from .transport import ClientSession, TCPClient

__all__ = ("MisskeyAPI",)


class MisskeyAPI:
    def __init__(self, instance_url: str, access_token: str):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.transport: TCPClient = ClientSession
        self.drive: MisskeyDrive = MisskeyDrive(self)
        self._semaphore = asyncio.Semaphore(MISSKEY_MAX_CONCURRENCY)
        self._antennas_cache: list[dict[str, Any]] = []
        self._antennas_cache_expires_at = 0.0
        self._antennas_cache_lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def close(self) -> None:
        await self.transport.close_session(silent=True)
        logger.debug("Misskey API client closed")

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.transport.session

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    @staticmethod
    def handle_response_status(response, endpoint: str):
        status = response.status
        if status == HTTP_BAD_REQUEST:
            logger.error(f"API bad request: {endpoint}")
            raise APIBadRequestError()
        if status == HTTP_UNAUTHORIZED:
            logger.error(f"API authentication failed: {endpoint}")
            raise AuthenticationError()
        if status == HTTP_FORBIDDEN:
            logger.error(f"API forbidden: {endpoint}")
            raise AuthenticationError()
        if status == HTTP_TOO_MANY_REQUESTS:
            logger.warning(f"API rate limited: {endpoint}")
            raise APIRateLimitError()

    @staticmethod
    def _format_error_text(error_text: str) -> str:
        s = error_text.strip()
        if not s:
            return ""
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return s
        if not isinstance(obj, dict):
            return s
        err = obj.get("error")
        if not isinstance(err, dict):
            return s
        code = err.get("code") or err.get("id")
        msg = err.get("message") or err.get("info") or err.get("kind")
        if isinstance(code, str) and isinstance(msg, str):
            return f"{code}: {msg}"
        if isinstance(msg, str):
            return msg
        return s

    async def _process_response(self, response, endpoint: str):
        if response.status in (HTTP_OK, HTTP_NO_CONTENT):
            if response.status == HTTP_NO_CONTENT:
                logger.debug(f"Misskey API request succeeded: {endpoint}")
                return {}
            try:
                result = await response.json()
                logger.debug(f"Misskey API request succeeded: {endpoint}")
                return result
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                if not await response.read():
                    logger.debug(f"Misskey API request succeeded: {endpoint}")
                    return {}
                raise APIConnectionError()
        error_text = self._format_error_text(await response.text())
        status = response.status
        if status == HTTP_BAD_REQUEST:
            logger.error(f"API bad request: {endpoint} - {error_text}")
            raise APIBadRequestError(error_text)
        if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            logger.error(f"API authentication failed: {endpoint} - {error_text}")
            raise AuthenticationError(error_text)
        if status == HTTP_TOO_MANY_REQUESTS:
            logger.warning(f"API rate limited: {endpoint} - {error_text}")
            raise APIRateLimitError(error_text)
        logger.error(f"API request failed: {status} - {endpoint} - {error_text}")
        raise APIConnectionError(error_text)

    @retry_async(
        max_retries=API_MAX_RETRIES,
        retryable_exceptions=(APIConnectionError, APIRateLimitError),
    )
    async def make_request(
        self, endpoint: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._make_request_once(endpoint, data)

    async def _make_request_once(
        self, endpoint: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.instance_url}/api/{endpoint}"
        payload = {"i": self.access_token}
        if data:
            payload.update(data)
        try:
            session: aiohttp.ClientSession = self.session
            async with self._semaphore, session.post(url, json=payload) as response:
                return await self._process_response(response, endpoint)
        except (
            aiohttp.ClientError,
            json.JSONDecodeError,
        ) as e:
            logger.error(f"HTTP request error: {e}")
            raise APIConnectionError() from e

    @retry_async(
        max_retries=API_MAX_RETRIES,
        retryable_exceptions=(APIConnectionError, APIRateLimitError),
    )
    async def make_multipart_request(
        self,
        endpoint: str,
        build_form: Callable[[], tuple[aiohttp.FormData, list[Any]]],
    ) -> dict[str, Any]:
        url = f"{self.instance_url}/api/{endpoint}"
        resources: list[Any] = []
        try:
            form, resources = build_form()
            session: aiohttp.ClientSession = self.session
            async with self._semaphore, session.post(url, data=form) as response:
                return await self._process_response(response, endpoint)
        except (aiohttp.ClientError, json.JSONDecodeError) as e:
            logger.error(f"HTTP request error: {e}")
            raise APIConnectionError() from e
        finally:
            for resource in resources:
                try:
                    close = getattr(resource, "close", None)
                    if callable(close):
                        close()
                except (OSError, AttributeError):
                    pass

    @staticmethod
    def _determine_reply_visibility(
        original_visibility: str, visibility: str | None
    ) -> str:
        if visibility is None:
            return original_visibility
        visibility_priority = {
            "followers": 1,
            "home": 2,
            "public": 3,
        }
        original_priority = visibility_priority.get(original_visibility, 0)
        reply_priority = visibility_priority.get(visibility, 3)
        if reply_priority > original_priority:
            logger.debug(
                f"Adjusted reply visibility from {visibility} to {original_visibility} to match original"
            )
            return original_visibility
        return visibility

    async def create_note(
        self,
        text: str,
        visibility: str | None = None,
        reply_id: str | None = None,
        local_only: bool | None = None,
        validate_reply: bool = True,
    ) -> dict[str, Any]:
        resolved_reply_id = reply_id
        if resolved_reply_id:
            resolved_reply_id, visibility = await self._resolve_reply_visibility(
                resolved_reply_id, visibility, validate_reply
            )
        if visibility is None:
            visibility = "public"
        data = {"text": text, "visibility": visibility}
        if resolved_reply_id:
            data["replyId"] = resolved_reply_id
        if local_only:
            data["localOnly"] = True
        result = await self.make_request("notes/create", data)
        logger.debug(
            f"Misskey note created: note_id={result.get('createdNote', {}).get('id', 'unknown')}"
        )
        return result

    async def _resolve_reply_visibility(
        self,
        reply_id: str,
        visibility: str | None,
        validate_reply: bool,
    ) -> tuple[str | None, str | None]:
        delays = (0.0, 2.0)
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            result = await self._try_resolve_reply_visibility(
                reply_id, visibility, validate_reply, is_last=delay == delays[-1]
            )
            if result is not None:
                return result
        return None, visibility

    async def _try_resolve_reply_visibility(
        self,
        reply_id: str,
        visibility: str | None,
        validate_reply: bool,
        *,
        is_last: bool,
    ) -> tuple[str | None, str | None] | None:
        try:
            original_note = await self._make_request_once(
                "notes/show", {"noteId": reply_id}
            )
            original_visibility = original_note.get("visibility", "public")
            return reply_id, self._determine_reply_visibility(
                original_visibility, visibility
            )
        except APIBadRequestError:
            return self._reply_visibility_missing(reply_id, visibility, validate_reply)
        except (APIConnectionError, APIRateLimitError) as e:
            if not is_last:
                return None
            return self._reply_visibility_unavailable(
                reply_id, visibility, validate_reply, e, retried=True
            )
        except AuthenticationError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return self._reply_visibility_unavailable(
                reply_id, visibility, validate_reply, e, retried=False
            )

    @staticmethod
    def _reply_visibility_missing(
        reply_id: str, visibility: str | None, validate_reply: bool
    ) -> tuple[str | None, str | None]:
        msg = "Target note not found"
        if validate_reply:
            logger.warning(f"{msg}; creating a new note instead of a reply: {reply_id}")
            return None, visibility
        logger.warning(
            f"{msg}; keeping replyId without visibility adjustment: {reply_id}"
        )
        return reply_id, visibility

    @staticmethod
    def _reply_visibility_unavailable(
        reply_id: str,
        visibility: str | None,
        validate_reply: bool,
        error: Exception,
        *,
        retried: bool,
    ) -> tuple[str | None, str | None]:
        msg = "Failed to get original note"
        if retried:
            msg += " after retries"
        if validate_reply:
            logger.warning(
                f"{msg}; creating a new note instead of a reply: {reply_id} - {error}"
            )
            return None, visibility
        logger.warning(
            f"{msg}; keeping replyId without visibility adjustment: {reply_id} - {error}"
        )
        return reply_id, visibility

    async def get_note(self, note_id: str) -> dict[str, Any]:
        return await self.make_request("notes/show", {"noteId": note_id})

    async def get_current_user(self) -> dict[str, Any]:
        return await self.make_request("i", {})

    async def list_antennas(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now < self._antennas_cache_expires_at and self._antennas_cache:
            return list(self._antennas_cache)
        async with self._antennas_cache_lock:
            now = time.monotonic()
            if now < self._antennas_cache_expires_at and self._antennas_cache:
                return list(self._antennas_cache)
            result = await self.make_request("antennas/list", {})
            antennas = result if isinstance(result, list) else []
            self._antennas_cache = antennas
            self._antennas_cache_expires_at = time.monotonic() + 30.0
            return list(antennas)

    async def send_message(self, user_id: str, text: str) -> dict[str, Any]:
        result = await self.make_request(
            "chat/messages/create-to-user", {"toUserId": user_id, "text": text}
        )
        logger.debug(
            f"Misskey chat message sent: message_id={result.get('id', 'unknown')}"
        )
        return result

    async def send_room_message(self, room_id: str, text: str) -> dict[str, Any]:
        result = await self.make_request(
            "chat/messages/create-to-room", {"toRoomId": room_id, "text": text}
        )
        logger.debug(
            f"Misskey room message sent: message_id={result.get('id', 'unknown')}"
        )
        return result

    async def create_reaction(self, note_id: str, reaction: str) -> dict[str, Any]:
        if not note_id:
            raise ValueError("note_id cannot be empty")
        if not reaction:
            raise ValueError("reaction cannot be empty")
        return await self.make_request(
            "notes/reactions/create", {"noteId": note_id, "reaction": reaction}
        )

    async def create_renote(
        self,
        note_id: str,
        visibility: str | None = None,
        text: str | None = None,
        local_only: bool | None = None,
    ) -> dict[str, Any]:
        if not note_id:
            raise ValueError("note_id cannot be empty")
        data: dict[str, Any] = {"renoteId": note_id}
        if visibility:
            data["visibility"] = visibility
        if text:
            data["text"] = text
        if local_only is not None:
            data["localOnly"] = bool(local_only)
        result = await self.make_request("notes/create", data)
        logger.debug(
            f"Misskey renote created: note_id={result.get('createdNote', {}).get('id', 'unknown')}"
        )
        return result

    async def get_messages(
        self, user_id: str, limit: int = 10, since_id: str | None = None
    ) -> list[dict[str, Any]]:
        data = {"userId": user_id, "limit": limit}
        if since_id:
            data["sinceId"] = since_id
        return await self.make_request("chat/messages/user-timeline", data)

    async def get_room_messages(
        self, room_id: str, limit: int = 10, since_id: str | None = None
    ) -> list[dict[str, Any]]:
        data = {"roomId": room_id, "limit": limit}
        if since_id:
            data["sinceId"] = since_id
        try:
            return await self.make_request("chat/messages/room-timeline", data)
        except APIBadRequestError as e:
            m = str(e)
            if m and "roomId" not in m and "toRoomId" not in m:
                raise
            data = {"toRoomId": room_id, "limit": limit}
            if since_id:
                data["sinceId"] = since_id
            return await self.make_request("chat/messages/room-timeline", data)
