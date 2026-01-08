import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import aiohttp
import anyio
from loguru import logger

from .constants import (
    API_MAX_RETRIES,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_NO_CONTENT,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
    MISSKEY_MAX_CONCURRENCY,
)
from .exceptions import (
    APIBadRequestError,
    APIConnectionError,
    APIRateLimitError,
    AuthenticationError,
)
from .transport import ClientSession, TCPClient
from .utils import retry_async

__all__ = ("MisskeyAPI", "MisskeyDrive", "DriveIO")


class DriveIO(Protocol):
    async def usage(self) -> dict[str, Any]: ...

    async def list_files(
        self,
        *,
        limit: int = 10,
        since_id: str | None = None,
        until_id: str | None = None,
        since_date: int | None = None,
        until_date: int | None = None,
        folder_id: str | None = None,
        file_type: str | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def show_file(self, file_id: str) -> dict[str, Any]: ...

    async def find_files(
        self, name: str, *, folder_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def upload_bytes(
        self,
        data: bytes,
        *,
        name: str | None = None,
        folder_id: str | None = None,
        comment: str | None = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: str | None = None,
    ) -> dict[str, Any]: ...

    async def upload_path(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        folder_id: str | None = None,
        comment: str | None = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: str | None = None,
    ) -> dict[str, Any]: ...

    async def upload_from_url(
        self,
        url: str,
        *,
        folder_id: str | None = None,
        name: str | None = None,
        comment: str | None = None,
        is_sensitive: bool = False,
        force: bool = False,
    ) -> dict[str, Any]: ...

    async def delete_file(self, file_id: str) -> dict[str, Any]: ...

    async def update_file(
        self,
        file_id: str,
        *,
        name: str | None = None,
        folder_id: str | None = None,
        comment: str | None = None,
        is_sensitive: bool | None = None,
    ) -> dict[str, Any]: ...

    async def download_bytes(
        self, file_id: str, *, thumbnail: bool = False, max_bytes: int | None = None
    ) -> bytes: ...

    async def download_to_path(
        self,
        file_id: str,
        path: str | Path,
        *,
        thumbnail: bool = False,
        chunk_size: int = 1024 * 1024,
    ) -> Path: ...

    async def list_folders(
        self,
        *,
        limit: int = 10,
        since_id: str | None = None,
        until_id: str | None = None,
        since_date: int | None = None,
        until_date: int | None = None,
        folder_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def create_folder(
        self, name: str, *, parent_id: str | None = None
    ) -> dict[str, Any]: ...

    async def find_folders(
        self, name: str, *, parent_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def show_folder(self, folder_id: str) -> dict[str, Any]: ...

    async def update_folder(
        self,
        folder_id: str,
        *,
        name: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def delete_folder(self, folder_id: str) -> dict[str, Any]: ...


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


class MisskeyDrive:
    def __init__(self, api: MisskeyAPI):
        self._api = api

    async def usage(self) -> dict[str, Any]:
        return await self._api.make_request("drive")

    async def list_files(
        self,
        *,
        limit: int = 10,
        since_id: str | None = None,
        until_id: str | None = None,
        since_date: int | None = None,
        until_date: int | None = None,
        folder_id: str | None = None,
        file_type: str | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"limit": limit}
        if since_id:
            data["sinceId"] = since_id
        if until_id:
            data["untilId"] = until_id
        if since_date is not None:
            data["sinceDate"] = since_date
        if until_date is not None:
            data["untilDate"] = until_date
        if folder_id is not None:
            data["folderId"] = folder_id
        if file_type is not None:
            data["type"] = file_type
        if sort is not None:
            data["sort"] = sort
        return await self._api.make_request("drive/files", data)

    async def show_file(self, file_id: str) -> dict[str, Any]:
        return await self._api.make_request("drive/files/show", {"fileId": file_id})

    async def find_files(
        self, name: str, *, folder_id: str | None = None
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"name": name}
        if folder_id is not None:
            data["folderId"] = folder_id
        return await self._api.make_request("drive/files/find", data)

    async def delete_file(self, file_id: str) -> dict[str, Any]:
        return await self._api.make_request("drive/files/delete", {"fileId": file_id})

    async def update_file(
        self,
        file_id: str,
        *,
        name: str | None = None,
        folder_id: str | None = None,
        comment: str | None = None,
        is_sensitive: bool | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"fileId": file_id}
        if name is not None:
            data["name"] = name
        if folder_id is not None:
            data["folderId"] = folder_id
        if comment is not None:
            data["comment"] = comment
        if is_sensitive is not None:
            data["isSensitive"] = is_sensitive
        return await self._api.make_request("drive/files/update", data)

    async def upload_from_url(
        self,
        url: str,
        *,
        folder_id: str | None = None,
        name: str | None = None,
        comment: str | None = None,
        is_sensitive: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"url": url}
        if folder_id is not None:
            data["folderId"] = folder_id
        if name is not None:
            data["name"] = name
        if comment is not None:
            data["comment"] = comment
        if is_sensitive:
            data["isSensitive"] = True
        if force:
            data["force"] = True
        return await self._api.make_request("drive/files/upload-from-url", data)

    async def upload_bytes(
        self,
        data: bytes,
        *,
        name: str | None = None,
        folder_id: str | None = None,
        comment: str | None = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        filename = name or "file"

        def build():
            form = aiohttp.FormData()
            form.add_field("i", self._api.access_token)
            if folder_id is not None:
                form.add_field("folderId", folder_id)
            if name is not None:
                form.add_field("name", name)
            if comment is not None:
                form.add_field("comment", comment)
            if is_sensitive:
                form.add_field("isSensitive", "true")
            if force:
                form.add_field("force", "true")
            form.add_field(
                "file",
                data,
                filename=filename,
                content_type=content_type or "application/octet-stream",
            )
            return form, []

        return await self._api.make_multipart_request("drive/files/create", build)

    async def upload_path(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        folder_id: str | None = None,
        comment: str | None = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        file_path = Path(path)
        if not file_path.is_file():
            raise ValueError(f"file not found: {file_path}")
        filename = name or file_path.name

        def build():
            f = file_path.open("rb")
            form = aiohttp.FormData()
            form.add_field("i", self._api.access_token)
            if folder_id is not None:
                form.add_field("folderId", folder_id)
            if name is not None:
                form.add_field("name", name)
            if comment is not None:
                form.add_field("comment", comment)
            if is_sensitive:
                form.add_field("isSensitive", "true")
            if force:
                form.add_field("force", "true")
            form.add_field(
                "file",
                f,
                filename=filename,
                content_type=content_type or "application/octet-stream",
            )
            return form, [f]

        return await self._api.make_multipart_request("drive/files/create", build)

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes:
        try:
            session: aiohttp.ClientSession = self._api.session
            async with self._api.semaphore, session.get(url) as response:
                if response.status != HTTP_OK:
                    self._api.handle_response_status(response, "drive/files/download")
                    raise APIConnectionError()
                if max_bytes is None:
                    return await response.read()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("file size exceeds limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, OSError) as e:
            raise APIConnectionError() from e

    async def download_bytes(
        self, file_id: str, *, thumbnail: bool = False, max_bytes: int | None = None
    ) -> bytes:
        info = await self.show_file(file_id)
        url = info.get("thumbnailUrl") if thumbnail else info.get("url")
        if not url:
            raise APIConnectionError()
        return await self.fetch_bytes(url, max_bytes=max_bytes)

    async def download_to_path(
        self,
        file_id: str,
        path: str | Path,
        *,
        thumbnail: bool = False,
        chunk_size: int = 1024 * 1024,
    ) -> Path:
        info = await self.show_file(file_id)
        url = info.get("thumbnailUrl") if thumbnail else info.get("url")
        if not url:
            raise APIConnectionError()
        dest = Path(path)
        if dest.parent and not dest.parent.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            session: aiohttp.ClientSession = self._api.session
            async with self._api.semaphore, session.get(url) as response:
                if response.status != HTTP_OK:
                    self._api.handle_response_status(response, "drive/files/download")
                    raise APIConnectionError()
                async with await anyio.open_file(dest, "wb") as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        await f.write(chunk)
        except (aiohttp.ClientError, OSError) as e:
            raise APIConnectionError() from e
        return dest

    async def list_folders(
        self,
        *,
        limit: int = 10,
        since_id: str | None = None,
        until_id: str | None = None,
        since_date: int | None = None,
        until_date: int | None = None,
        folder_id: str | None = None,
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"limit": limit}
        if since_id:
            data["sinceId"] = since_id
        if until_id:
            data["untilId"] = until_id
        if since_date is not None:
            data["sinceDate"] = since_date
        if until_date is not None:
            data["untilDate"] = until_date
        if folder_id is not None:
            data["folderId"] = folder_id
        return await self._api.make_request("drive/folders", data)

    async def create_folder(
        self, name: str, *, parent_id: str | None = None
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"name": name}
        if parent_id is not None:
            data["parentId"] = parent_id
        return await self._api.make_request("drive/folders/create", data)

    async def find_folders(
        self, name: str, *, parent_id: str | None = None
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"name": name}
        if parent_id is not None:
            data["parentId"] = parent_id
        return await self._api.make_request("drive/folders/find", data)

    async def show_folder(self, folder_id: str) -> dict[str, Any]:
        return await self._api.make_request(
            "drive/folders/show", {"folderId": folder_id}
        )

    async def update_folder(
        self,
        folder_id: str,
        *,
        name: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"folderId": folder_id}
        if name is not None:
            data["name"] = name
        if parent_id is not None:
            data["parentId"] = parent_id
        return await self._api.make_request("drive/folders/update", data)

    async def delete_folder(self, folder_id: str) -> dict[str, Any]:
        return await self._api.make_request(
            "drive/folders/delete", {"folderId": folder_id}
        )
