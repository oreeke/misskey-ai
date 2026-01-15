import asyncio
import inspect
from typing import Any

from loguru import logger

from ..shared.utils import extract_user_handle, extract_user_id, extract_username
from .context import PluginContext

__all__ = ("PluginBase",)


class PluginBase:
    def __init__(self, config_or_context):
        self.db = None
        self.plugin_manager = None
        self.global_config = None
        self.misskey = None
        self.drive = None
        self.openai = None
        self.streaming = None
        self.runtime = None
        self.bot = None
        if isinstance(config_or_context, PluginContext):
            context = config_or_context
            self.config = context.config
            self.name = context.name
            for attr_name in dir(context):
                if not attr_name.startswith("_") and attr_name not in (
                    "name",
                    "config",
                ):
                    setattr(self, attr_name, getattr(context, attr_name))
        else:
            self.config = config_or_context
            self.name = self.__class__.__name__
        self.enabled = self.config.get("enabled", False)
        self.priority = self.config.get("priority", 0)
        self._initialized = False
        self._resources_to_cleanup = []

    async def __aenter__(self):
        result = await self.initialize()
        if result:
            self._initialized = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()
        if self._resources_to_cleanup:
            logger.warning(
                f"Plugin {self.name} has uncleaned resources: {len(self._resources_to_cleanup)}"
            )
        self._initialized = False
        return False

    async def initialize(self) -> bool:
        await asyncio.sleep(0)
        return True

    async def cleanup(self) -> None:
        await self._cleanup_registered_resources()

    def get_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "description": getattr(self, "description", "No description available"),
        }

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        logger.info(f"Plugin {self.name} {'enabled' if enabled else 'disabled'}")

    @staticmethod
    def _extract_username(data: dict[str, Any]) -> str:
        return extract_username(data)

    @staticmethod
    def _extract_user_handle(data: dict[str, Any]) -> str | None:
        return extract_user_handle(data)

    @staticmethod
    def _extract_user_id(data: dict[str, Any]) -> str | None:
        return extract_user_id(data)

    def _log_plugin_action(self, action: str, details: str = "") -> None:
        logger.info(f"Plugin {self.name} {action}{': ' + details if details else ''}")

    @staticmethod
    def _validate_plugin_response(response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        required_types = {"handled": bool, "plugin_name": str, "response": str}
        return all(
            isinstance(response.get(k), t)
            for k, t in required_types.items()
            if k in response
        )

    def _register_resource(self, resource: Any, cleanup_method: str = "close") -> None:
        self._resources_to_cleanup.append((resource, cleanup_method))

    async def _cleanup_registered_resources(self) -> None:
        for resource, cleanup_method in self._resources_to_cleanup:
            try:
                if hasattr(resource, cleanup_method):
                    method = getattr(resource, cleanup_method)
                    if inspect.iscoroutinefunction(method):
                        await method()
                    else:
                        method()
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.error(f"Plugin {self.name} resource cleanup failed: {e}")
        self._resources_to_cleanup.clear()
