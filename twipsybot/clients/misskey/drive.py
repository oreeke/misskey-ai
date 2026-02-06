from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import anyio

from ...shared.constants import HTTP_OK
from ...shared.exceptions import APIConnectionError

if TYPE_CHECKING:
    from .misskey_api import MisskeyAPI

__all__ = ("MisskeyDrive",)


class MisskeyDrive:
    def __init__(self, api: "MisskeyAPI"):
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
