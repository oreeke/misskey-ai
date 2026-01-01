import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional, Protocol

import aiohttp
from loguru import logger

from .constants import (
    API_MAX_RETRIES,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_NO_CONTENT,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
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
        since_id: Optional[str] = None,
        until_id: Optional[str] = None,
        since_date: Optional[int] = None,
        until_date: Optional[int] = None,
        folder_id: Optional[str] = None,
        type: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...
    async def show_file(self, file_id: str) -> dict[str, Any]: ...
    async def find_files(
        self, name: str, *, folder_id: Optional[str] = None
    ) -> list[dict[str, Any]]: ...
    async def upload_bytes(
        self,
        data: bytes,
        *,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
        comment: Optional[str] = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: Optional[str] = None,
    ) -> dict[str, Any]: ...
    async def upload_path(
        self,
        path: str | Path,
        *,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
        comment: Optional[str] = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: Optional[str] = None,
    ) -> dict[str, Any]: ...
    async def upload_from_url(
        self,
        url: str,
        *,
        folder_id: Optional[str] = None,
        name: Optional[str] = None,
        comment: Optional[str] = None,
        is_sensitive: bool = False,
        force: bool = False,
    ) -> dict[str, Any]: ...
    async def delete_file(self, file_id: str) -> dict[str, Any]: ...
    async def update_file(
        self,
        file_id: str,
        *,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
        comment: Optional[str] = None,
        is_sensitive: Optional[bool] = None,
    ) -> dict[str, Any]: ...
    async def download_bytes(
        self, file_id: str, *, thumbnail: bool = False, max_bytes: Optional[int] = None
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
        since_id: Optional[str] = None,
        until_id: Optional[str] = None,
        since_date: Optional[int] = None,
        until_date: Optional[int] = None,
        folder_id: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...
    async def create_folder(
        self, name: str, *, parent_id: Optional[str] = None
    ) -> dict[str, Any]: ...
    async def find_folders(
        self, name: str, *, parent_id: Optional[str] = None
    ) -> list[dict[str, Any]]: ...
    async def show_folder(self, folder_id: str) -> dict[str, Any]: ...
    async def update_folder(
        self,
        folder_id: str,
        *,
        name: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> dict[str, Any]: ...
    async def delete_folder(self, folder_id: str) -> dict[str, Any]: ...


class MisskeyAPI:
    def __init__(self, instance_url: str, access_token: str):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.transport: TCPClient = ClientSession
        self.drive: MisskeyDrive = MisskeyDrive(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def close(self) -> None:
        await self.transport.close_session(silent=True)
        logger.debug("Misskey API 客户端已关闭")

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.transport.session

    def _handle_response_status(self, response, endpoint: str):
        status = response.status
        if status == HTTP_BAD_REQUEST:
            logger.error(f"API 请求错误: {endpoint}")
            raise APIBadRequestError()
        if status == HTTP_UNAUTHORIZED:
            logger.error(f"API 认证失败: {endpoint}")
            raise AuthenticationError()
        if status == HTTP_FORBIDDEN:
            logger.error(f"API 权限不足: {endpoint}")
            raise AuthenticationError()
        if status == HTTP_TOO_MANY_REQUESTS:
            logger.warning(f"API 频率限制: {endpoint}")
            raise APIRateLimitError()

    async def _process_response(self, response, endpoint: str):
        if response.status in (HTTP_OK, HTTP_NO_CONTENT):
            if response.status == HTTP_NO_CONTENT:
                logger.debug(f"Misskey API 请求成功: {endpoint}")
                return {}
            try:
                result = await response.json()
                logger.debug(f"Misskey API 请求成功: {endpoint}")
                return result
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                if not (await response.read()):
                    logger.debug(f"Misskey API 请求成功: {endpoint}")
                    return {}
                raise APIConnectionError()
        self._handle_response_status(response, endpoint)
        error_text = await response.text()
        logger.error(f"API 请求失败: {response.status} - {error_text}")
        raise APIConnectionError()

    @retry_async(
        max_retries=API_MAX_RETRIES,
        retryable_exceptions=(APIConnectionError, APIRateLimitError),
    )
    async def _make_request(
        self, endpoint: str, data: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        url = f"{self.instance_url}/api/{endpoint}"
        payload = {"i": self.access_token}
        if data:
            payload.update(data)
        try:
            session = self.session
            async with session.post(url, json=payload) as response:
                return await self._process_response(response, endpoint)
        except (
            aiohttp.ClientError,
            json.JSONDecodeError,
        ) as e:
            logger.error(f"HTTP 请求错误: {e}")
            raise APIConnectionError() from e

    @retry_async(
        max_retries=API_MAX_RETRIES,
        retryable_exceptions=(APIConnectionError, APIRateLimitError),
    )
    async def _make_multipart_request(
        self,
        endpoint: str,
        build_form: Callable[[], tuple[aiohttp.FormData, list[Any]]],
    ) -> dict[str, Any]:
        url = f"{self.instance_url}/api/{endpoint}"
        resources: list[Any] = []
        try:
            form, resources = build_form()
            async with self.session.post(url, data=form) as response:
                return await self._process_response(response, endpoint)
        except (aiohttp.ClientError, json.JSONDecodeError) as e:
            logger.error(f"HTTP 请求错误: {e}")
            raise APIConnectionError() from e
        finally:
            for resource in resources:
                try:
                    close = getattr(resource, "close", None)
                    if callable(close):
                        close()
                except (OSError, AttributeError):
                    pass

    def _determine_reply_visibility(
        self, original_visibility: str, visibility: Optional[str]
    ) -> str:
        if visibility is None:
            return original_visibility
        visibility_priority = {
            "specified": 0,
            "followers": 1,
            "home": 2,
            "public": 3,
        }
        original_priority = visibility_priority.get(original_visibility, 3)
        reply_priority = visibility_priority.get(visibility, 3)
        if reply_priority > original_priority:
            logger.debug(
                f"调整回复可见性从 {visibility} 到 {original_visibility} 以匹配原帖"
            )
            return original_visibility
        return visibility

    async def _get_visibility_for_reply(
        self, reply_id: str, visibility: Optional[str]
    ) -> str:
        try:
            original_note = await self.get_note(reply_id)
            original_visibility = original_note.get("visibility", "public")
            return self._determine_reply_visibility(original_visibility, visibility)
        except (
            APIConnectionError,
            APIRateLimitError,
            AuthenticationError,
            ValueError,
        ) as e:
            logger.warning(f"获取原帖可见性失败，使用默认设置: {e}")
            return visibility if visibility is not None else "home"

    async def create_note(
        self,
        text: str,
        visibility: Optional[str] = None,
        reply_id: Optional[str] = None,
        validate_reply: bool = True,
    ) -> dict[str, Any]:
        if reply_id and validate_reply and not await self.note_exists(reply_id):
            logger.warning(f"目标帖子不存在，将创建新帖回复: {reply_id}")
            reply_id = None
        if reply_id:
            visibility = await self._get_visibility_for_reply(reply_id, visibility)
        elif visibility is None:
            visibility = "public"
        data = {"text": text, "visibility": visibility}
        if reply_id:
            data["replyId"] = reply_id
        result = await self._make_request("notes/create", data)
        logger.debug(
            f"Misskey 发帖成功，note_id: {result.get('createdNote', {}).get('id', 'unknown')}"
        )
        return result

    async def get_note(self, note_id: str) -> dict[str, Any]:
        return await self._make_request("notes/show", {"noteId": note_id})

    async def note_exists(self, note_id: str) -> bool:
        try:
            await self.get_note(note_id)
            return True
        except (
            APIBadRequestError,
            APIConnectionError,
            APIRateLimitError,
            AuthenticationError,
        ):
            return False

    async def get_current_user(self) -> dict[str, Any]:
        return await self._make_request("i", {})

    async def send_message(self, user_id: str, text: str) -> dict[str, Any]:
        result = await self._make_request(
            "chat/messages/create-to-user", {"toUserId": user_id, "text": text}
        )
        logger.debug(f"Misskey 聊天发送成功，message_id: {result.get('id', 'unknown')}")
        return result

    async def get_messages(
        self, user_id: str, limit: int = 10, since_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        data = {"userId": user_id, "limit": limit}
        if since_id:
            data["sinceId"] = since_id
        return await self._make_request("chat/messages/user-timeline", data)


class MisskeyDrive:
    def __init__(self, api: MisskeyAPI):
        self._api = api

    async def usage(self) -> dict[str, Any]:
        return await self._api._make_request("drive")

    async def list_files(
        self,
        *,
        limit: int = 10,
        since_id: Optional[str] = None,
        until_id: Optional[str] = None,
        since_date: Optional[int] = None,
        until_date: Optional[int] = None,
        folder_id: Optional[str] = None,
        type: Optional[str] = None,
        sort: Optional[str] = None,
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
        if type is not None:
            data["type"] = type
        if sort is not None:
            data["sort"] = sort
        return await self._api._make_request("drive/files", data)

    async def show_file(self, file_id: str) -> dict[str, Any]:
        return await self._api._make_request("drive/files/show", {"fileId": file_id})

    async def find_files(
        self, name: str, *, folder_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"name": name}
        if folder_id is not None:
            data["folderId"] = folder_id
        return await self._api._make_request("drive/files/find", data)

    async def delete_file(self, file_id: str) -> dict[str, Any]:
        return await self._api._make_request("drive/files/delete", {"fileId": file_id})

    async def update_file(
        self,
        file_id: str,
        *,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
        comment: Optional[str] = None,
        is_sensitive: Optional[bool] = None,
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
        return await self._api._make_request("drive/files/update", data)

    async def upload_from_url(
        self,
        url: str,
        *,
        folder_id: Optional[str] = None,
        name: Optional[str] = None,
        comment: Optional[str] = None,
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
        return await self._api._make_request("drive/files/upload-from-url", data)

    async def upload_bytes(
        self,
        data: bytes,
        *,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
        comment: Optional[str] = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: Optional[str] = None,
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

        return await self._api._make_multipart_request("drive/files/create", build)

    async def upload_path(
        self,
        path: str | Path,
        *,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
        comment: Optional[str] = None,
        is_sensitive: bool = False,
        force: bool = False,
        content_type: Optional[str] = None,
    ) -> dict[str, Any]:
        file_path = Path(path)
        if not file_path.is_file():
            raise ValueError(f"文件不存在: {file_path}")
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

        return await self._api._make_multipart_request("drive/files/create", build)

    async def _fetch_bytes(self, url: str, *, max_bytes: Optional[int] = None) -> bytes:
        try:
            async with self._api.session.get(url) as response:
                if response.status != HTTP_OK:
                    self._api._handle_response_status(response, "drive/files/download")
                    raise APIConnectionError()
                if max_bytes is None:
                    return await response.read()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("文件大小超过限制")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, OSError) as e:
            raise APIConnectionError() from e

    async def download_bytes(
        self, file_id: str, *, thumbnail: bool = False, max_bytes: Optional[int] = None
    ) -> bytes:
        info = await self.show_file(file_id)
        url = info.get("thumbnailUrl") if thumbnail else info.get("url")
        if not url:
            raise APIConnectionError()
        return await self._fetch_bytes(url, max_bytes=max_bytes)

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
            async with self._api.session.get(url) as response:
                if response.status != HTTP_OK:
                    self._api._handle_response_status(response, "drive/files/download")
                    raise APIConnectionError()
                with dest.open("wb") as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        f.write(chunk)
        except (aiohttp.ClientError, OSError) as e:
            raise APIConnectionError() from e
        return dest

    async def list_folders(
        self,
        *,
        limit: int = 10,
        since_id: Optional[str] = None,
        until_id: Optional[str] = None,
        since_date: Optional[int] = None,
        until_date: Optional[int] = None,
        folder_id: Optional[str] = None,
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
        return await self._api._make_request("drive/folders", data)

    async def create_folder(
        self, name: str, *, parent_id: Optional[str] = None
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"name": name}
        if parent_id is not None:
            data["parentId"] = parent_id
        return await self._api._make_request("drive/folders/create", data)

    async def find_folders(
        self, name: str, *, parent_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"name": name}
        if parent_id is not None:
            data["parentId"] = parent_id
        return await self._api._make_request("drive/folders/find", data)

    async def show_folder(self, folder_id: str) -> dict[str, Any]:
        return await self._api._make_request(
            "drive/folders/show", {"folderId": folder_id}
        )

    async def update_folder(
        self,
        folder_id: str,
        *,
        name: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"folderId": folder_id}
        if name is not None:
            data["name"] = name
        if parent_id is not None:
            data["parentId"] = parent_id
        return await self._api._make_request("drive/folders/update", data)

    async def delete_folder(self, folder_id: str) -> dict[str, Any]:
        return await self._api._make_request(
            "drive/folders/delete", {"folderId": folder_id}
        )
