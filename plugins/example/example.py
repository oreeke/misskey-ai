from typing import Any, Optional

from loguru import logger

from src.plugin import PluginBase


class ExamplePlugin(PluginBase):
    description = "示例插件，展示插件系统的基本用法"

    def __init__(self, context):
        super().__init__(context)
        self.greeting_enabled = self.config.get("greeting_enabled", True)
        self.auto_post_enabled = self.config.get("auto_post_enabled", False)

    async def initialize(self) -> bool:
        self._log_plugin_action(
            "初始化完成", f"问候功能: {'启用' if self.greeting_enabled else '禁用'}"
        )
        return True

    async def cleanup(self) -> None:
        await super().cleanup()

    def _create_response(
        self, response_text: str, content_key: str = "response"
    ) -> dict[str, Any]:
        try:
            response = {
                "handled": True,
                "plugin_name": self.name,
                content_key: response_text,
            }
            return response if self._validate_plugin_response(response) else None
        except ValueError as e:
            logger.error(f"创建响应时出错: {e}")
            return None

    async def on_mention(
        self, mention_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not self.greeting_enabled:
            return None
        try:
            text = mention_data.get("text", "").lower()
            if any(greeting in text for greeting in ["你好", "hello", "hi"]):
                username = self._extract_username(mention_data)
                self._log_plugin_action("处理问候消息", f"来自 @{username}")
                return self._create_response("你好！我是示例插件，很高兴见到你！")
        except (ValueError, KeyError) as e:
            logger.error(f"Example 插件处理提及时出错: {e}")
        return None

    async def on_message(
        self, message_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not self.greeting_enabled:
            return None
        try:
            text = message_data.get("text", "").lower()
            if "插件" in text and "测试" in text:
                username = self._extract_username(message_data)
                self._log_plugin_action("处理测试消息", f"来自 @{username}")
                return self._create_response(
                    "插件系统工作正常！这是来自示例插件的回复。"
                )
        except (ValueError, KeyError) as e:
            logger.error(f"Example 插件处理消息时出错: {e}")
        return None

    async def on_auto_post(self) -> Optional[dict[str, Any]]:
        if not self.auto_post_enabled:
            return None
        try:
            self._log_plugin_action("生成自动发布内容")
            return self._create_response("这是来自示例插件的自动发布内容！", "content")
        except ValueError as e:
            logger.error(f"Example 插件生成自动发布内容时出错: {e}")
        return None
