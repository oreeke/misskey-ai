#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional
from loguru import logger
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from src.plugin_base import PluginBase
from src.persistence import PersistenceManager

class TopicsPlugin(PluginBase):
    description = "主题插件，为自动发帖添加按顺序循环的主题关键词"
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.prefix_template = config.get("prefix_template", "以{topic}为主题，")
        self.persistence = None
        self.topics = []
        
    async def initialize(self) -> bool:
        try:
            self.persistence = PersistenceManager()
            await self._create_topics_table()
            await self._load_topics()
            logger.info(f"Topics 插件初始化完成，加载了 {len(self.topics)} 个主题关键词")
            return True
        except Exception as e:
            logger.error(f"Topics 插件初始化失败: {e}")
            return False
    
    async def cleanup(self) -> None:
        if self.persistence:
            await self.persistence.close()
    
    async def _create_topics_table(self) -> None:
        try:
            async with self.persistence._lock:
                cursor = self.persistence._connection.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS topics_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        last_used_line INTEGER NOT NULL DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("SELECT COUNT(*) FROM topics_usage")
                count = cursor.fetchone()[0]
                if count == 0:
                    cursor.execute("INSERT INTO topics_usage (last_used_line) VALUES (0)")
                self.persistence._connection.commit()
                logger.debug("Topics 数据库表创建/检查完成")
        except Exception as e:
            logger.error(f"创建 topics 数据库表失败: {e}")
            raise
    
    async def _load_topics(self) -> None:
        try:
            plugin_dir = Path(__file__).parent
            topics_file_path = plugin_dir / "topics.txt"
            if not topics_file_path.exists():
                logger.warning(f"主题文件不存在: {topics_file_path}，将创建示例文件")
                await self._create_example_topics_file(topics_file_path)
            with open(topics_file_path, 'r', encoding='utf-8') as f:
                self.topics = [line.strip() for line in f.readlines() if line.strip()]
            if not self.topics:
                logger.warning("主题文件为空，将创建示例内容")
                await self._create_example_topics_file(topics_file_path)
                with open(topics_file_path, 'r', encoding='utf-8') as f:
                    self.topics = [line.strip() for line in f.readlines() if line.strip()]
            logger.debug(f"成功加载 {len(self.topics)} 个主题关键词")
        except Exception as e:
            logger.error(f"加载主题文件失败: {e}")
            self.topics = ["科技", "生活", "学习", "思考", "创新"]
            logger.info(f"使用默认主题关键词: {self.topics}")
    
    async def _create_example_topics_file(self, file_path: Path) -> None:
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            example_topics = [
                "科技",
                "生活",
                "学习",
                "思考",
                "创新",
                "艺术",
                "音乐",
                "电影",
                "读书",
                "旅行",
                "美食",
                "健康",
                "运动",
                "自然",
                "哲学"
            ]
            with open(file_path, 'w', encoding='utf-8') as f:
                for topic in example_topics:
                    f.write(f"{topic}\n")
            logger.info(f"已创建示例主题文件: {file_path}")
        except Exception as e:
            logger.error(f"创建示例主题文件失败: {e}")
    
    async def _get_last_used_line(self) -> int:
        try:
            async with self.persistence._lock:
                cursor = self.persistence._connection.cursor()
                cursor.execute("SELECT last_used_line FROM topics_usage ORDER BY id DESC LIMIT 1")
                result = cursor.fetchone()
                return result[0] if result else 0
        except Exception as e:
            logger.error(f"获取上次使用行数失败: {e}")
            return 0
    
    async def _update_last_used_line(self, line_number: int) -> None:
        try:
            async with self.persistence._lock:
                cursor = self.persistence._connection.cursor()
                cursor.execute(
                    "UPDATE topics_usage SET last_used_line = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (line_number,)
                )
                self.persistence._connection.commit()
        except Exception as e:
            logger.error(f"更新上次使用行数失败: {e}")
    
    async def _get_next_topic(self) -> str:
        if not self.topics:
            return "生活"
        try:
            last_used_line = await self._get_last_used_line()
            next_line = last_used_line % len(self.topics)
            topic = self.topics[next_line]
            await self._update_last_used_line(last_used_line + 1)
            logger.debug(f"选择主题: {topic} (行数: {last_used_line + 1})")
            return topic
        except Exception as e:
            logger.error(f"获取下一个主题失败: {e}")
            return self.topics[0] if self.topics else "生活"
    
    async def on_auto_post(self) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            topic = await self._get_next_topic()
            plugin_prompt = self.prefix_template.format(topic=topic)
            timestamp_min = int(time.time() // 60)
            return {
                "modify_prompt": True,
                "plugin_prompt": plugin_prompt,
                "timestamp": timestamp_min,
                "plugin_name": "Topics"
            }
        except Exception as e:
            logger.error(f"Topics 插件处理自动发帖失败: {e}")
            return None
    
    async def on_startup(self) -> None:
        logger.info("Topics 插件已启动")
    
    async def on_shutdown(self) -> None:
        await self.cleanup()