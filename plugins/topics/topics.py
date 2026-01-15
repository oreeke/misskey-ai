import asyncio
from pathlib import Path
from typing import Any

import anyio
from loguru import logger

from misskey_ai.plugin import PluginBase


class TopicsPlugin(PluginBase):
    description = "主题插件，为自动发帖插入有序循环的主题关键词"

    def __init__(self, context):
        super().__init__(context)
        self.prefix_template = self.config.get("prefix_template", "以{topic}为主题，")
        self.start_line = self.config.get("start_line", 1)
        self.topics = []

    async def initialize(self) -> bool:
        try:
            if not self.db:
                logger.error("Topics plugin missing db instance")
                return False
            await self._load_topics()
            await self._initialize_plugin_data()
            self._log_plugin_action("initialized", f"Custom topics: {len(self.topics)}")
            return True
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Topics plugin initialization failed: {e}")
            return False

    async def cleanup(self) -> None:
        await super().cleanup()

    async def on_auto_post(self) -> dict[str, Any] | None:
        try:
            topic = await self._get_next_topic()
            return {
                "modify_prompt": True,
                "plugin_prompt": self.prefix_template.format(topic=topic),
                "plugin_name": self.name,
            }
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Topics plugin auto-post hook failed: {e}")
            return None

    async def _initialize_plugin_data(self) -> None:
        try:
            last_used_line = await self.db.get_plugin_data("Topics", "last_used_line")
            if last_used_line is None:
                initial_index = max(0, self.start_line - 1)
                if self.topics:
                    initial_index %= len(self.topics)
                await self.db.set_plugin_data(
                    "Topics", "last_used_line", str(initial_index)
                )
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Topics plugin DB initialization failed: {e}")
            raise

    def _use_default_topics(self) -> None:
        self.topics = ["科技", "生活", "学习", "思考", "创新"]
        logger.info(f"Using default topics: {self.topics}")

    async def _load_topics(self) -> None:
        try:
            topics_file_path = Path(__file__).parent / "topics.txt"
            if not topics_file_path.exists():
                logger.warning(f"Topics file not found: {topics_file_path}")
                self._use_default_topics()
                return
            async with await anyio.open_file(
                topics_file_path, "r", encoding="utf-8"
            ) as f:
                content = await f.read()
            self.topics = [
                line.strip() for line in content.splitlines() if line.strip()
            ]
            if not self.topics:
                logger.warning("Topics file is empty")
                self._use_default_topics()
                return
        except Exception as e:
            logger.warning(f"Failed to load topics file: {e}")
            self._use_default_topics()

    async def _get_next_topic(self) -> str:
        if not self.topics:
            return "生活"
        try:
            last_used_line = await self._get_last_used_line()
            index = last_used_line % len(self.topics)
            topic = self.topics[index]
            await self._update_last_used_line((index + 1) % len(self.topics))
            self._log_plugin_action("selected topic", f"{topic} (line: {index + 1})")
            return topic
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to get next topic: {e}")
            return self.topics[0] if self.topics else "生活"

    async def _get_last_used_line(self) -> int:
        try:
            result = await self.db.get_plugin_data("Topics", "last_used_line")
            return max(0, int(result)) if result else 0
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to get last used line: {e}")
            return 0

    async def _update_last_used_line(self, line_number: int) -> None:
        try:
            await self.db.set_plugin_data("Topics", "last_used_line", str(line_number))
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to update last used line: {e}")
