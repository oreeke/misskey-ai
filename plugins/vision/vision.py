import base64
import re
from typing import Any

from loguru import logger

from misskey_ai.plugin import PluginBase
from misskey_ai.shared.config_keys import ConfigKeys


class VisionPlugin(PluginBase):
    description = "视觉插件，识别提及（@）或聊天中的图片并回复"

    def __init__(self, context):
        super().__init__(context)
        self.max_images = int(self.config.get("max_images", 3))
        self.max_bytes = self._parse_size(self.config.get("max_bytes"), 6 * 1024 * 1024)
        self.use_thumbnail = bool(self.config.get("use_thumbnail", True))
        self.default_prompt = str(
            self.config.get("default_prompt", "请描述图片内容并回答用户的问题。")
        )

    @staticmethod
    def _normalize_image_mime(value: Any) -> str | None:
        return value if isinstance(value, str) and value.startswith("image/") else None

    def _select_direct_url(self, file_like: dict[str, Any]) -> str | None:
        a, b = (
            ("thumbnailUrl", "url") if self.use_thumbnail else ("url", "thumbnailUrl")
        )
        return self._normalize_url(file_like.get(a)) or self._normalize_url(
            file_like.get(b)
        )

    async def _try_fetch_bytes_by_url(self, direct_url: str | None) -> bytes | None:
        if not direct_url or not hasattr(self, "misskey"):
            return None
        try:
            return await self.misskey.drive.fetch_bytes(
                direct_url, max_bytes=self.max_bytes
            )
        except Exception as e:
            logger.error(f"Vision failed to download image: {repr(e)}")
            return None

    async def _ensure_image_mime(self, fid: str, mime: str | None) -> str | None:
        if mime:
            return mime
        try:
            info = await self.drive.show_file(fid)
        except Exception as e:
            logger.error(f"Vision failed to read file info: {repr(e)}")
            return None
        return self._normalize_image_mime(info.get("type"))

    async def _try_download_bytes_by_id(self, fid: str) -> bytes | None:
        try:
            return await self.drive.download_bytes(
                fid, thumbnail=self.use_thumbnail, max_bytes=self.max_bytes
            )
        except Exception as e:
            logger.error(f"Vision failed to download image: {repr(e)}")
            return None

    @staticmethod
    def _make_image_part(mime: str, data: bytes) -> dict[str, Any]:
        b64 = base64.b64encode(data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}

    @staticmethod
    def _normalize_url(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        url = value.strip().replace("`", "").strip()
        return url or None

    @staticmethod
    def _normalize_payload(data: dict[str, Any], *, kind: str) -> dict[str, Any]:
        if kind != "chat" and isinstance(data.get("note"), dict):
            return data["note"]
        return data

    @staticmethod
    def _parse_size(value: Any, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if not isinstance(value, str):
            return default
        s = value.strip().lower().replace("_", "")
        if not s:
            return default
        if s.isdigit():
            return int(s)
        if not (m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?b?)", s)):
            return default
        n = float(m.group(1))
        u = m.group(2) or "b"
        unit = u[0] if u[0] in {"k", "m", "g", "t"} else "b"
        mul = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[unit]
        return max(0, int(n * mul))

    async def initialize(self) -> bool:
        self._log_plugin_action("initialized")
        return True

    async def on_mention(self, mention_data: dict[str, Any]) -> dict[str, Any] | None:
        if not (parts := await self._build_user_content(mention_data, kind="mention")):
            return None
        reply = await self._call_vision(parts, call_type="mention image")
        return self._create_response(reply)

    async def on_message(self, message_data: dict[str, Any]) -> dict[str, Any] | None:
        if not (parts := await self._build_user_content(message_data, kind="chat")):
            return None
        reply = await self._call_vision(parts, call_type="chat image")
        return self._create_response(reply)

    def _create_response(self, response_text: str) -> dict[str, Any] | None:
        response = {
            "handled": True,
            "plugin_name": self.name,
            "response": response_text,
        }
        return response if self._validate_plugin_response(response) else None

    @staticmethod
    def _extract_text(data: dict[str, Any], *, kind: str) -> str:
        data = VisionPlugin._normalize_payload(data, kind=kind)
        if kind == "chat":
            return (
                data.get("text") or data.get("content") or data.get("body") or ""
            ).strip()
        parts: list[str] = []
        for v in (data.get("cw"), data.get("text") or data.get("body")):
            if isinstance(v, str) and (s := v.strip()):
                parts.append(s)
        return "\n\n".join(parts).strip()

    @staticmethod
    def _extract_files(data: dict[str, Any], *, kind: str) -> list[dict[str, Any]]:
        data = VisionPlugin._normalize_payload(data, kind=kind)
        files: list[dict[str, Any]] = []
        if kind == "chat":
            if isinstance(data.get("file"), dict):
                files.append(data["file"])
            if fid := data.get("fileId"):
                files.append({"id": fid})
            return VisionPlugin._dedupe_files(files)
        if isinstance(data.get("files"), list):
            files.extend([f for f in data["files"] if isinstance(f, dict)])
        if isinstance(data.get("fileIds"), list):
            files.extend(
                [{"id": fid} for fid in data["fileIds"] if isinstance(fid, str)]
            )
        return VisionPlugin._dedupe_files(files)

    @staticmethod
    def _dedupe_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for f in files:
            fid = f.get("id")
            if not isinstance(fid, str) or fid in seen:
                continue
            seen.add(fid)
            out.append(f)
        return out

    async def _build_user_content(
        self, data: dict[str, Any], *, kind: str
    ) -> list[dict[str, Any]]:
        if not hasattr(self, "drive") or not hasattr(self, "openai"):
            return []
        text = self._extract_text(data, kind=kind)
        files = self._extract_files(data, kind=kind)[: self.max_images]
        images: list[dict[str, Any]] = []
        for f in files:
            if not (item := await self._to_image_part(f)):
                continue
            images.append(item)
        if not images:
            return []
        prompt = text or self.default_prompt
        return [{"type": "text", "text": prompt}, *images]

    async def _to_image_part(self, file_like: dict[str, Any]) -> dict[str, Any] | None:
        fid = file_like.get("id")
        if not isinstance(fid, str):
            return None
        mime = self._normalize_image_mime(file_like.get("type"))
        data = await self._try_fetch_bytes_by_url(self._select_direct_url(file_like))
        if data is not None and not mime:
            mime = await self._ensure_image_mime(fid, mime)
        if data is None or not mime:
            mime = await self._ensure_image_mime(fid, mime)
            if not mime:
                return None
            data = await self._try_download_bytes_by_id(fid)
            if data is None:
                return None
        if not mime or data is None:
            return None
        return self._make_image_part(mime, data)

    async def _call_vision(
        self, user_content: list[dict[str, Any]], *, call_type: str
    ) -> str:
        system_prompt = (
            self.global_config.get(ConfigKeys.BOT_SYSTEM_PROMPT, "") or ""
        ).strip()
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_content})
        reply = await self.openai.generate_chat(
            messages,
            max_tokens=self.global_config.get(ConfigKeys.OPENAI_MAX_TOKENS),
            temperature=self.global_config.get(ConfigKeys.OPENAI_TEMPERATURE),
        )
        logger.debug(f"Vision {call_type} reply generated; length: {len(reply)}")
        return reply
