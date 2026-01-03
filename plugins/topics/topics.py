import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from src.plugin import PluginBase


class TopicsPlugin(PluginBase):
    description = "主题插件，为自动发帖插入按顺序循环的主题关键词"

    def __init__(self, context):
        super().__init__(context)
        self.prefix_template = self.config.get("prefix_template", "以{topic}为主题，")
        self.start_line = self.config.get("start_line", 1)
        self.topics = []

    async def initialize(self) -> bool:
        try:
            if not self.persistence_manager:
                logger.error("Topics 插件未获得 persistence_manager 实例")
                return False
            await self._load_topics()
            await self._initialize_plugin_data()
            self._log_plugin_action(
                "初始化完成", f"装载 {len(self.topics)} 个主题关键词"
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Topics 插件初始化失败: {e}")
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Topics 插件处理自动发帖失败: {e}")
            return None

    async def _initialize_plugin_data(self) -> None:
        try:
            last_used_line = await self.persistence_manager.get_plugin_data(
                "Topics", "last_used_line"
            )
            if last_used_line is None:
                await self.persistence_manager.set_plugin_data(
                    "Topics", "last_used_line", str(max(0, self.start_line - 1))
                )
            logger.debug("Topics 插件数据库初始化完成")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Topics 插件数据库初始化失败: {e}")
            raise

    def _use_default_topics(self) -> None:
        self.topics = ["科技", "生活", "学习", "思考", "创新"]
        logger.info(f"使用默认主题关键词: {self.topics}")

    async def _load_topics(self) -> None:
        try:
            topics_file_path = Path(__file__).parent / "topics.txt"
            if not topics_file_path.exists():
                logger.warning(f"主题文件不存在: {topics_file_path}")
                self._use_default_topics()
                return
            with open(topics_file_path, "r", encoding="utf-8") as f:
                self.topics = [line.strip() for line in f if line.strip()]
            if not self.topics:
                logger.warning("主题文件为空")
                self._use_default_topics()
                return
            logger.debug(f"成功加载 {len(self.topics)} 个主题关键词")
        except Exception as e:
            logger.warning(f"加载主题文件失败: {e}")
            self._use_default_topics()

    async def _get_next_topic(self) -> str:
        if not self.topics:
            return "生活"
        try:
            last_used_line = await self._get_last_used_line()
            topic = self.topics[last_used_line % len(self.topics)]
            await self._update_last_used_line(last_used_line + 1)
            self._log_plugin_action("选择主题", f"{topic} (行数: {last_used_line + 1})")
            return topic
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"获取下一个主题失败: {e}")
            return self.topics[0] if self.topics else "生活"

    async def _get_last_used_line(self) -> int:
        try:
            result = await self.persistence_manager.get_plugin_data(
                "Topics", "last_used_line"
            )
            return int(result) if result else 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"获取上次使用行数失败: {e}")
            return 0

    async def _update_last_used_line(self, line_number: int) -> None:
        try:
            await self.persistence_manager.set_plugin_data(
                "Topics", "last_used_line", str(line_number)
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"更新上次使用行数失败: {e}")
