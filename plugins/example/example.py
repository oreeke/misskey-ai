import re
from typing import Any, Optional

from loguru import logger

from src.plugin import PluginBase


class ExamplePlugin(PluginBase):
    description = "示例插件，展示插件系统的规范写法与常见注意点"

    MENTION_PATTERN = re.compile(r"@\w+\s*")

    def __init__(self, context):
        super().__init__(context)
        self.greeting_enabled = bool(self.config.get("greeting_enabled", True))
        self.auto_post_enabled = bool(self.config.get("auto_post_enabled", False))
        self.allow_media_only = bool(self.config.get("allow_media_only", False))

        self.greeting_keywords = [
            s.strip().lower()
            for s in (self.config.get("greeting_keywords") or ["你好", "hello", "hi"])
            if isinstance(s, str) and s.strip()
        ]
        self.test_keywords = [
            s.strip().lower()
            for s in (self.config.get("test_keywords") or ["插件", "测试", "example"])
            if isinstance(s, str) and s.strip()
        ]

    async def initialize(self) -> bool:
        self._log_plugin_action(
            "初始化完成",
            f"问候: {self.greeting_enabled}, 自动发帖: {self.auto_post_enabled}, 纯媒体: {self.allow_media_only}",
        )
        return True

    async def cleanup(self) -> None:
        await super().cleanup()

    def _normalize_payload(self, data: dict[str, Any], *, kind: str) -> dict[str, Any]:
        if kind == "mention" and isinstance(data.get("note"), dict):
            return data["note"]
        return data

    def _get_text(self, data: dict[str, Any], *, kind: str) -> str:
        data = self._normalize_payload(data, kind=kind)
        text = data.get("text") or data.get("content") or data.get("body") or ""
        return text.strip() if isinstance(text, str) else ""

    def _get_cleaned_text(self, data: dict[str, Any], *, kind: str) -> str:
        text = self._get_text(data, kind=kind)
        if kind == "mention":
            text = self.MENTION_PATTERN.sub("", text)
        return text.strip().lower()

    def _has_media(self, data: dict[str, Any], *, kind: str) -> bool:
        data = self._normalize_payload(data, kind=kind)
        if kind == "chat":
            return bool(data.get("fileId") or data.get("file"))
        return bool(data.get("fileIds") or data.get("files"))

    async def _bump_counter(self, key: str) -> None:
        pm = getattr(self, "persistence_manager", None)
        if not pm:
            return
        try:
            v = await pm.get_plugin_data(self.name, key)
            n = int(v) + 1 if v else 1
            await pm.set_plugin_data(self.name, key, str(n))
        except Exception:
            return

    def _create_response(
        self, response_text: str, *, handled: bool = True
    ) -> Optional[dict[str, Any]]:
        response = {
            "handled": handled,
            "plugin_name": self.name,
            "response": response_text,
        }
        return response if self._validate_plugin_response(response) else None

    async def on_mention(
        self, mention_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not self.greeting_enabled:
            return None
        try:
            text = self._get_cleaned_text(mention_data, kind="mention")
            if not text:
                return None
            if any(k in text for k in self.greeting_keywords):
                username = self._extract_username(mention_data)
                await self._bump_counter("mention_greeting_count")
                self._log_plugin_action("处理问候消息", f"来自 @{username}")
                return self._create_response("你好！这是示例插件的回复。")
            if "example" in text or "示例" in text:
                await self._bump_counter("mention_example_count")
                return self._create_response("示例插件已收到你的提及。")
        except Exception as e:
            logger.exception(f"Example 插件处理提及时发生异常: {e}")
        return None

    async def on_message(
        self, message_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        try:
            text = self._get_cleaned_text(message_data, kind="chat")
            has_media = self._has_media(message_data, kind="chat")
            if not text:
                if self.allow_media_only and has_media:
                    await self._bump_counter("chat_media_only_count")
                    return self._create_response("收到一条纯媒体消息（示例插件）。")
                return None
            if all(k in text for k in self.test_keywords[:2]) or "example" in text:
                username = self._extract_username(message_data)
                await self._bump_counter("chat_test_count")
                self._log_plugin_action("处理测试消息", f"来自 @{username}")
                return self._create_response(
                    "插件系统工作正常！这是来自示例插件的回复。"
                )
        except Exception as e:
            logger.exception(f"Example 插件处理消息时发生异常: {e}")
        return None

    async def on_auto_post(self) -> Optional[dict[str, Any]]:
        if not self.auto_post_enabled:
            return None
        try:
            await self._bump_counter("auto_post_count")
            self._log_plugin_action("生成自动发布内容")
            return {
                "plugin_name": self.name,
                "content": "这是来自示例插件的自动发布内容！",
            }
        except Exception as e:
            logger.exception(f"Example 插件生成自动发布内容时发生异常: {e}")
            return None
