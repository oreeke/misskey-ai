import asyncio
import importlib.util
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

import yaml
from loguru import logger

from . import utils
from .config import Config
from .utils import extract_user_id, extract_username

__all__ = ("PluginBase", "PluginContext", "PluginManager")


class PluginContext:
    def __init__(self, name: str, config: dict[str, Any], **context_objects):
        self.name = name
        self.config = config
        for key, value in context_objects.items():
            setattr(self, key, value)


class PluginBase:
    def __init__(
        self, config_or_context, utils_provider: dict[str, Callable] | None = None
    ):
        self.persistence_manager = None
        self.plugin_manager = None
        self.global_config = None
        self.misskey = None
        self.drive = None
        self.openai = None
        self.streaming = None
        self.runtime = None
        self.bot = None
        self.utils_provider: dict[str, Callable] = {}
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
            self._utils = self.utils_provider
        else:
            self.config = config_or_context
            self.name = self.__class__.__name__
            self.utils_provider = utils_provider or {}
            self._utils = self.utils_provider
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
                f"插件 {self.name} 存在未清理的资源: {len(self._resources_to_cleanup)} 项"
            )
        self._initialized = False
        return False

    @staticmethod
    async def initialize() -> bool:
        return True

    async def cleanup(self) -> None:
        await self._cleanup_registered_resources()

    @staticmethod
    async def on_startup() -> None:
        return None

    @staticmethod
    async def on_mention(_mention_data: dict[str, Any]) -> dict[str, Any] | None:
        return None

    @staticmethod
    async def on_message(_message_data: dict[str, Any]) -> dict[str, Any] | None:
        return None

    @staticmethod
    async def on_reaction(_reaction_data: dict[str, Any]) -> dict[str, Any] | None:
        return None

    @staticmethod
    async def on_follow(_follow_data: dict[str, Any]) -> dict[str, Any] | None:
        return None

    @staticmethod
    async def on_timeline_note(_note_data: dict[str, Any]) -> dict[str, Any] | None:
        return None

    @staticmethod
    async def on_auto_post() -> dict[str, Any] | None:
        return None

    @staticmethod
    async def on_shutdown() -> None:
        return None

    def get_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "description": getattr(self, "description", "No description available"),
        }

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        logger.info(f"插件 {self.name} {'启用' if enabled else '禁用'}")

    @staticmethod
    def _extract_username(data: dict[str, Any]) -> str:
        return extract_username(data)

    @staticmethod
    def _extract_user_id(data: dict[str, Any]) -> str | None:
        return extract_user_id(data)

    def _log_plugin_action(self, action: str, details: str = "") -> None:
        logger.info(f"{self.name} 插件{action}{': ' + details if details else ''}")

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
                    if asyncio.iscoroutinefunction(method):
                        await method()
                    else:
                        method()
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.error(f"插件 {self.name} 清理资源失败: {e}")
        self._resources_to_cleanup.clear()


class PluginManager:
    def __init__(
        self,
        config: Config,
        plugins_dir: str = "plugins",
        persistence=None,
        context_objects: dict[str, Any] | None = None,
    ):
        self.config = config
        self.plugins_dir = Path(plugins_dir)
        self.plugins: dict[str, PluginBase] = {}
        self.persistence = persistence
        self.context_objects = context_objects or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup_plugins()
        return False

    async def load_plugins(self) -> None:
        if not self.plugins_dir.exists():
            logger.info(f"插件目录不存在: {self.plugins_dir}")
            return
        for plugin_dir in self.plugins_dir.iterdir():
            if (
                plugin_dir.is_dir()
                and not plugin_dir.name.startswith(".")
                and plugin_dir.name not in {"__pycache__", "example"}
            ):
                await self._load_plugin(
                    plugin_dir, self._load_plugin_config(plugin_dir)
                )
        await self._initialize_plugins()
        enabled_count = sum(plugin.enabled for plugin in self.plugins.values())
        logger.info(f"已发现 {len(self.plugins)} 个插件，{enabled_count} 个已启用")

    @staticmethod
    def _load_plugin_config(plugin_dir: Path) -> dict[str, Any]:
        config_file = plugin_dir / "config.yaml"
        if not config_file.exists():
            return {"enabled": False}
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"加载插件 {plugin_dir.name} 配置文件时出错: {e}")
            return {}

    async def _load_plugin(
        self, plugin_dir: Path, plugin_config: dict[str, Any]
    ) -> None:
        try:
            plugin_file = plugin_dir / f"{plugin_dir.name}.py"
            if not plugin_file.exists():
                logger.warning(
                    f"插件目录 {plugin_dir.name} 中未找到 {plugin_dir.name}.py 文件"
                )
                return
            if not (module := self._load_plugin_module(plugin_dir, plugin_file)):
                return
            if not (plugin_class := self._find_plugin_class(module, plugin_dir.name)):
                return
            plugin_instance = self._create_plugin_instance(
                plugin_class, plugin_dir.name, plugin_config
            )
            self.plugins[plugin_dir.name] = plugin_instance
            status = "启用" if plugin_instance.enabled else "禁用"
            logger.debug(f"已发现插件: {plugin_dir.name} (状态: {status})")
        except Exception as e:
            logger.error(f"加载插件 {plugin_dir.name} 失败: {e}")

    @staticmethod
    def _load_plugin_module(plugin_dir: Path, plugin_file: Path):
        spec = importlib.util.spec_from_file_location(
            f"plugins.{plugin_dir.name}.plugin", plugin_file
        )
        if spec is None or spec.loader is None:
            logger.warning(f"无法加载插件规范: {plugin_dir.name}")
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _find_plugin_class(module, plugin_name):
        candidates = [
            attr
            for attr in (getattr(module, name) for name in dir(module))
            if isinstance(attr, type)
            and issubclass(attr, PluginBase)
            and attr is not PluginBase
        ]
        if not candidates:
            logger.warning(f"插件 {plugin_name.capitalize()} 中未找到有效的插件类")
            return None
        expected = f"{plugin_name.capitalize()}Plugin"
        for cls in candidates:
            if cls.__name__ == expected:
                return cls
        if len(candidates) == 1:
            return candidates[0]
        names = sorted(cls.__name__ for cls in candidates)
        logger.warning(
            f"插件 {plugin_name.capitalize()} 存在多个插件类 {names}，期望 {expected}"
        )
        return None

    def _create_plugin_instance(self, plugin_class, plugin_name, plugin_config):
        utils_provider = {
            "extract_username": utils.extract_username,
            "extract_user_id": utils.extract_user_id,
        }
        context_objects = {
            "persistence_manager": self.persistence,
            "utils_provider": utils_provider,
            "plugin_manager": self,
            "global_config": self.config,
        }
        for k, v in self.context_objects.items():
            context_objects.setdefault(k, v)
        context = PluginContext(
            name=plugin_name.capitalize(),
            config=plugin_config,
            **context_objects,
        )
        return plugin_class(context)

    async def _initialize_plugins(self) -> None:
        for _, plugin in sorted(
            self.plugins.items(), key=lambda x: x[1].priority, reverse=True
        ):
            if not plugin.enabled:
                continue
            try:
                if await plugin.initialize():
                    logger.debug(f"插件 {plugin.name} 初始化完成")
                else:
                    logger.warning(f"插件 {plugin.name} 初始化失败")
                    plugin.set_enabled(False)
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.exception(f"初始化插件 {plugin.name} 时出错: {e}")
                plugin.set_enabled(False)

    async def cleanup_plugins(self) -> None:
        for plugin in self.plugins.values():
            if plugin.enabled:
                try:
                    await plugin.cleanup()
                except Exception as e:
                    if isinstance(e, asyncio.CancelledError):
                        raise
                    logger.exception(f"清理插件 {plugin.name} 时出错: {e}")

    async def on_startup(self) -> None:
        await self.call_plugin_hook("on_startup")

    async def on_mention(self, mention_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_mention", mention_data)

    async def on_message(self, message_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_message", message_data)

    async def on_reaction(self, reaction_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_reaction", reaction_data)

    async def on_follow(self, follow_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_follow", follow_data)

    async def on_timeline_note(self, note_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_timeline_note", note_data)

    async def on_auto_post(self) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_auto_post")

    async def on_shutdown(self) -> None:
        await self.call_plugin_hook("on_shutdown")

    async def call_plugin_hook(self, hook_name: str, *args, **kwargs) -> list[Any]:
        results = []
        enabled_plugins = sorted(
            [p for p in self.plugins.values() if p.enabled],
            key=lambda x: x.priority,
            reverse=True,
        )
        stop_on_handled = hook_name in {"on_message", "on_mention"}
        for plugin in enabled_plugins:
            if not hasattr(plugin, hook_name):
                continue
            try:
                if (
                    result := await getattr(plugin, hook_name)(*args, **kwargs)
                ) is not None:
                    results.append(result)
                    if (
                        stop_on_handled
                        and isinstance(result, dict)
                        and result.get("handled") is True
                    ):
                        break
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.exception(
                    f"调用插件 {plugin.name} 的 {hook_name} hook 时发生未处理异常: {e}"
                )
        return results

    def get_plugin_info(self) -> list[dict[str, Any]]:
        return [plugin.get_info() for plugin in self.plugins.values()]

    def get_plugin(self, name: str) -> PluginBase | None:
        return self.plugins.get(name)

    def _find_plugin_by_name(self, name: str) -> PluginBase | None:
        return self.plugins.get(name) or next(
            (p for n, p in self.plugins.items() if n.lower() == name.lower()), None
        )

    def enable_plugin(self, name: str) -> bool:
        if plugin := self._find_plugin_by_name(name):
            plugin.set_enabled(True)
            return True
        return False

    def disable_plugin(self, name: str) -> bool:
        if plugin := self._find_plugin_by_name(name):
            plugin.set_enabled(False)
            return True
        return False
