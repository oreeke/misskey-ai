#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import Dict, Any, Optional
from loguru import logger
from src.plugin_base import PluginBase


class ExamplePlugin(PluginBase):
    description = "示例插件，展示插件系统的基本用法"

    def __init__(self, config: Dict[str, Any], utils_provider=None):
        super().__init__(config, utils_provider)
        self.greeting_enabled = config.get("greeting_enabled", True)
        self.auto_post_enabled = config.get("auto_post_enabled", False)

    async def initialize(self) -> bool:
        logger.info(
            f"Example 插件初始化完成，问候功能: {'启用' if self.greeting_enabled else '禁用'}"
        )
        return True

    async def cleanup(self) -> None:
        pass

    async def on_mention(
        self, mention_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        try:
            if not self.greeting_enabled:
                return None
            username = self._extract_username(mention_data)
            text = mention_data.get("text", "").lower()
            if "你好" in text or "hello" in text or "hi" in text:
                self._log_plugin_action("处理问候消息", f"来自 @{username}")
                response = {
                    "handled": True,
                    "plugin_name": "Example",
                    "response": "你好！我是示例插件，很高兴见到你！",
                }
                if self._validate_plugin_response(response):
                    return response
                else:
                    logger.error("Example 插件响应验证失败")
                    return None
            return None
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            logger.error(f"Example 插件处理提及时出错: {e}")
            return None

    async def on_message(
        self, message_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        try:
            if not self.greeting_enabled:
                return None
            username = self._extract_username(message_data)
            text = message_data.get("text", "").lower()
            if "插件" in text and "测试" in text:
                self._log_plugin_action("处理测试消息", f"来自 @{username}")
                response = {
                    "handled": True,
                    "plugin_name": "Example",
                    "response": "插件系统工作正常！这是来自示例插件的回复。",
                }
                if self._validate_plugin_response(response):
                    return response
                else:
                    logger.error("Example 插件响应验证失败")
                    return None
            return None
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            logger.error(f"Example 插件处理消息时出错: {e}")
            return None

    async def on_auto_post(self) -> Optional[Dict[str, Any]]:
        try:
            if not self.auto_post_enabled:
                return None
            self._log_plugin_action("生成自动发布内容")
            response = {
                "handled": True,
                "plugin_name": "Example",
                "content": "这是来自示例插件的自动发布内容！",
            }
            if self._validate_plugin_response(response):
                return response
            else:
                logger.error("Example 插件响应验证失败")
                return None
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Example 插件生成自动发布内容时出错: {e}")
            return None
