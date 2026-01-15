from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..shared.constants import ConfigKeys

if TYPE_CHECKING:
    from .core import MisskeyBot


class AutoPostService:
    def __init__(self, bot: MisskeyBot):
        self.bot = bot
        self.posts_today = 0
        self.last_auto_post_time = bot.runtime.startup_time

    def post_count(self) -> None:
        self.posts_today += 1
        self.last_auto_post_time = datetime.now(timezone.utc)

    def check_post_counter(self, max_posts: int) -> bool:
        if self.posts_today >= max_posts:
            logger.debug(f"Daily post limit reached ({max_posts}); skipping auto-post")
            return False
        return True

    def reset_daily_counters(self) -> None:
        self.posts_today = 0
        logger.debug("Post counter reset")

    async def run(self) -> None:
        if not self.bot.config.get(ConfigKeys.BOT_AUTO_POST_ENABLED):
            return
        max_posts = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY)
        local_only = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_LOCAL_ONLY)
        if not self.bot.runtime.running or not self.check_post_counter(max_posts):
            return
        try:
            plugin_results = await self.bot.plugin_manager.on_auto_post()
            if await self._try_plugin_post(plugin_results, max_posts, local_only):
                return
            await self._generate_ai_post(plugin_results, max_posts, local_only)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error during auto-post: {e}")

    async def _try_plugin_post(
        self, plugin_results: list[Any], max_posts: int, local_only: bool | None
    ) -> bool:
        for result in plugin_results:
            if result and result.get("content"):
                content = result.get("content")
                visibility = result.get(
                    "visibility",
                    self.bot.config.get(ConfigKeys.BOT_AUTO_POST_VISIBILITY),
                )
                await self.bot.misskey.create_note(
                    content, visibility=visibility, local_only=local_only
                )
                self.post_count()
                logger.info(f"Auto-post succeeded: {self.bot.format_log_text(content)}")
                logger.info(f"Daily post count: {self.posts_today}/{max_posts}")
                return True
        return False

    async def _generate_ai_post(
        self, plugin_results: list[Any], max_posts: int, local_only: bool | None
    ) -> None:
        plugin_prompt = ""
        timestamp_override = None
        for result in plugin_results:
            if result and result.get("modify_prompt"):
                if result.get("plugin_prompt"):
                    plugin_prompt = result.get("plugin_prompt")
                if result.get("timestamp"):
                    timestamp_override = result.get("timestamp")
                logger.info(
                    f"Plugin {result.get('plugin_name')} requested prompt modification: {plugin_prompt}"
                )
        post_prompt = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_PROMPT, "")
        try:
            content = await self._generate_post(
                self.bot.system_prompt, post_prompt, plugin_prompt, timestamp_override
            )
        except ValueError as e:
            logger.warning(f"Auto-post failed; skipping this run: {e}")
            return
        visibility = self.bot.config.get(ConfigKeys.BOT_AUTO_POST_VISIBILITY)
        await self.bot.misskey.create_note(
            content, visibility=visibility, local_only=local_only
        )
        self.post_count()
        logger.info(f"Auto-post succeeded: {self.bot.format_log_text(content)}")
        logger.info(f"Daily post count: {self.posts_today}/{max_posts}")

    async def _generate_post(
        self,
        system_prompt: str,
        prompt: str,
        plugin_prompt: str,
        timestamp_override: int | None = None,
    ) -> str:
        if not prompt:
            raise ValueError("Missing prompt")
        timestamp_min = timestamp_override or int(
            datetime.now(timezone.utc).timestamp() // 60
        )
        full_prompt = f"[{timestamp_min}] {plugin_prompt}{prompt}"
        return await self.bot.openai.generate_text(
            full_prompt, system_prompt, **self.bot.ai_config
        )
