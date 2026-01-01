import base64
import re
from typing import Any, Optional

from loguru import logger

from src.constants import ConfigKeys
from src.plugin import PluginBase


class VisionPlugin(PluginBase):
    description = "视觉插件，识别提及或聊天中的图片并回复"

    def _normalize_url(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        url = value.strip().replace("`", "").strip()
        return url or None

    def _normalize_payload(self, data: dict[str, Any], *, kind: str) -> dict[str, Any]:
        if kind != "chat" and isinstance(data.get("note"), dict):
            return data["note"]
        return data

    def _parse_size(self, value: Any, default: int) -> int:
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
        self.max_images = int(self.config.get("max_images", 3))
        self.max_bytes = self._parse_size(self.config.get("max_bytes"), 6 * 1024 * 1024)
        self.use_thumbnail = bool(self.config.get("use_thumbnail", True))
        self.default_prompt = str(
            self.config.get("default_prompt", "请描述图片内容并回答用户的问题。")
        )
        self._log_plugin_action("初始化完成")
        return True

    async def on_mention(
        self, mention_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not (parts := await self._build_user_content(mention_data, kind="mention")):
            return None
        reply = await self._call_vision(parts, call_type="提及图片")
        return self._create_response(reply)

    async def on_message(
        self, message_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not (parts := await self._build_user_content(message_data, kind="chat")):
            return None
        reply = await self._call_vision(parts, call_type="聊天图片")
        return self._create_response(reply)

    def _create_response(self, response_text: str) -> Optional[dict[str, Any]]:
        response = {
            "handled": True,
            "plugin_name": self.name,
            "response": response_text,
        }
        return response if self._validate_plugin_response(response) else None

    def _extract_text(self, data: dict[str, Any], *, kind: str) -> str:
        data = self._normalize_payload(data, kind=kind)
        if kind == "chat":
            return (
                data.get("text") or data.get("content") or data.get("body") or ""
            ).strip()
        return (data.get("text") or data.get("body") or "").strip()

    def _extract_files(
        self, data: dict[str, Any], *, kind: str
    ) -> list[dict[str, Any]]:
        data = self._normalize_payload(data, kind=kind)
        files: list[dict[str, Any]] = []
        if kind == "chat":
            if isinstance(data.get("file"), dict):
                files.append(data["file"])
            if fid := data.get("fileId"):
                files.append({"id": fid})
            return self._dedupe_files(files)
        if isinstance(data.get("files"), list):
            files.extend([f for f in data["files"] if isinstance(f, dict)])
        if isinstance(data.get("fileIds"), list):
            files.extend(
                [{"id": fid} for fid in data["fileIds"] if isinstance(fid, str)]
            )
        return self._dedupe_files(files)

    def _dedupe_files(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    async def _to_image_part(
        self, file_like: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        fid = file_like.get("id")
        if not isinstance(fid, str):
            return None
        mime = file_like.get("type")
        if not isinstance(mime, str) or not mime.startswith("image/"):
            mime = None
        direct_url = None
        if self.use_thumbnail:
            direct_url = self._normalize_url(
                file_like.get("thumbnailUrl")
            ) or self._normalize_url(file_like.get("url"))
        else:
            direct_url = self._normalize_url(
                file_like.get("url")
            ) or self._normalize_url(file_like.get("thumbnailUrl"))

        data = None
        if direct_url and hasattr(self, "misskey"):
            try:
                data = await self.misskey.drive._fetch_bytes(
                    direct_url, max_bytes=self.max_bytes
                )
            except Exception as e:
                logger.error(f"Vision 下载图片失败: {repr(e)}")
                data = None
        if data is None:
            if mime is None:
                try:
                    info = await self.drive.show_file(fid)
                except Exception as e:
                    logger.error(f"Vision 读取文件信息失败: {repr(e)}")
                    return None
                mime = info.get("type")
                if not isinstance(mime, str) or not mime.startswith("image/"):
                    return None
            try:
                data = await self.drive.download_bytes(
                    fid, thumbnail=self.use_thumbnail, max_bytes=self.max_bytes
                )
            except Exception as e:
                logger.error(f"Vision 下载图片失败: {repr(e)}")
                return None
        b64 = base64.b64encode(data).decode("ascii")
        url = f"data:{mime};base64,{b64}"
        return {"type": "image_url", "image_url": {"url": url}}

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
        logger.debug(f"Vision {call_type} 回复成功，长度: {len(reply)}")
        return reply
