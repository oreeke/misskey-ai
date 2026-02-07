import asyncio
from typing import Any

from loguru import logger

from ..clients.misskey.channels import ChannelSpec, ChannelType
from ..clients.misskey.misskey_api import MisskeyAPI
from ..clients.misskey.streaming import StreamingClient
from ..shared.config import Config
from ..shared.config_keys import ConfigKeys
from ..shared.utils import normalize_tokens
from .handlers import BotHandlers
from .runtime import BotRuntime


class StreamingConnector:
    def __init__(
        self,
        *,
        config: Config,
        misskey: MisskeyAPI,
        streaming: StreamingClient,
        runtime: BotRuntime,
        handlers: BotHandlers,
    ):
        self._config = config
        self._misskey = misskey
        self._streaming = streaming
        self._runtime = runtime
        self._handlers = handlers
        self._timeline_channels = self._load_timeline_channels()

    def load_timeline_channels(self) -> set[str]:
        self._timeline_channels = self._load_timeline_channels()
        return set(self._timeline_channels)

    def _load_timeline_channels(self) -> set[str]:
        if not self._config.get(ConfigKeys.BOT_TIMELINE_ENABLED):
            return set()
        enabled: set[str] = set()
        if self._config.get(ConfigKeys.BOT_TIMELINE_HOME):
            enabled.add(ChannelType.HOME_TIMELINE.value)
        if self._config.get(ConfigKeys.BOT_TIMELINE_LOCAL):
            enabled.add(ChannelType.LOCAL_TIMELINE.value)
        if self._config.get(ConfigKeys.BOT_TIMELINE_HYBRID):
            enabled.add(ChannelType.HYBRID_TIMELINE.value)
        if self._config.get(ConfigKeys.BOT_TIMELINE_GLOBAL):
            enabled.add(ChannelType.GLOBAL_TIMELINE.value)
        return enabled

    def _load_antenna_selectors(self) -> list[str]:
        return normalize_tokens(self._config.get(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS))

    @staticmethod
    def _build_antenna_index(
        antennas: list[dict[str, Any]],
    ) -> tuple[set[str], dict[str, list[str]]]:
        antenna_ids: set[str] = set()
        name_to_ids: dict[str, list[str]] = {}
        for antenna in antennas:
            if not isinstance(antenna, dict):
                continue
            antenna_id = antenna.get("id")
            if isinstance(antenna_id, str) and antenna_id:
                antenna_ids.add(antenna_id)
            name = antenna.get("name")
            if not isinstance(name, str):
                continue
            normalized_name = name.strip()
            if not normalized_name or not isinstance(antenna_id, str) or not antenna_id:
                continue
            name_to_ids.setdefault(normalized_name, []).append(antenna_id)
        return antenna_ids, name_to_ids

    @staticmethod
    def _dedupe_non_empty(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @staticmethod
    def _resolve_antenna_selector(
        selector: str, antenna_ids: set[str], name_to_ids: dict[str, list[str]]
    ) -> str:
        if selector in antenna_ids:
            return selector
        candidates = name_to_ids.get(selector)
        if not candidates:
            logger.warning(f"Antenna not found: {selector}")
            return ""
        if len(candidates) != 1:
            logger.warning(f"Antenna name is ambiguous: {selector}")
            return ""
        return candidates[0]

    async def _resolve_antenna_ids(self, selectors: list[str]) -> list[str]:
        normalized = [s.strip() for s in selectors if isinstance(s, str) and s.strip()]
        if not normalized:
            return []
        antennas = await self._misskey.list_antennas()
        antenna_ids, name_to_ids = self._build_antenna_index(antennas)
        resolved = [
            self._resolve_antenna_selector(s, antenna_ids, name_to_ids)
            for s in normalized
        ]
        return self._dedupe_non_empty(resolved)

    async def get_streaming_channels(self) -> list[ChannelSpec]:
        active = {ChannelType.MAIN.value, *self._timeline_channels}
        ordered = [
            ChannelType.MAIN.value,
            ChannelType.HOME_TIMELINE.value,
            ChannelType.LOCAL_TIMELINE.value,
            ChannelType.HYBRID_TIMELINE.value,
            ChannelType.GLOBAL_TIMELINE.value,
        ]
        result: list[ChannelSpec] = [c for c in ordered if c in active]
        selectors = self._load_antenna_selectors()
        for antenna_id in await self._resolve_antenna_ids(selectors):
            result.append((ChannelType.ANTENNA.value, {"antennaId": antenna_id}))
        return result

    async def restart_streaming(self) -> None:
        if (task := self._runtime.tasks.get("streaming")) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._streaming.disconnect()
        channels = await self.get_streaming_channels()
        await self._streaming.connect_once(channels)
        self._runtime.add_task("streaming", self._streaming.connect(channels))

    async def setup_streaming(self) -> None:
        try:
            self._streaming.on_mention(self._handlers.on_mention)
            self._streaming.on_message(self._handlers.on_message)
            self._streaming.on_notification(self._handlers.on_notification)
            self._streaming.on_note(self._handlers.on_timeline_note)
            channels = await self.get_streaming_channels()
            await self._streaming.connect_once(channels)
            self._runtime.add_task("streaming", self._streaming.connect(channels))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Failed to set up Streaming connection: {e}")
            raise
