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
from .utils import extract_user_handle, extract_user_id, extract_username

__all__ = ("PluginBase", "PluginContext", "PluginManager")

_PLUGIN_CONFIG_FILENAME = "config.yaml"


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
                    if asyncio.iscoroutinefunction(method):
                        await method()
                    else:
                        method()
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.error(f"Plugin {self.name} resource cleanup failed: {e}")
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
        self.discovered_plugins: dict[str, dict[str, Any]] = {}
        self.persistence = persistence
        self.context_objects = context_objects or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup_plugins()
        return False

    def _iter_plugin_dirs(self):
        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            if plugin_dir.name.startswith("."):
                continue
            if plugin_dir.name in {"__pycache__", "example"}:
                continue
            yield plugin_dir

    def _discover_plugin_dir(
        self, plugin_dir: Path, plugin_config: dict[str, Any]
    ) -> tuple[bool, bool]:
        configured = (plugin_dir / _PLUGIN_CONFIG_FILENAME).exists()
        enabled = bool(plugin_config.get("enabled", False))
        key = plugin_dir.name
        self.discovered_plugins[key] = {
            "name": key.capitalize(),
            "enabled": enabled,
            "priority": plugin_config.get("priority", 0),
            "configured": configured,
        }
        if configured:
            status = "enabled" if enabled else "disabled"
            logger.debug(f"Discovered plugin: {plugin_dir.name} (status: {status})")
        return configured, enabled

    def _maybe_load_plugin_dir(
        self,
        plugin_dir: Path,
        plugin_config: dict[str, Any],
        *,
        configured: bool,
        enabled: bool,
    ) -> None:
        if configured and enabled:
            self._load_plugin(plugin_dir, plugin_config)

    async def load_plugins(self) -> None:
        if not self.plugins_dir.exists():
            logger.info(f"Plugins directory not found: {self.plugins_dir}")
            return
        for plugin_dir in self._iter_plugin_dirs():
            plugin_config = self._load_plugin_config(plugin_dir)
            configured, enabled = self._discover_plugin_dir(plugin_dir, plugin_config)
            self._maybe_load_plugin_dir(
                plugin_dir, plugin_config, configured=configured, enabled=enabled
            )
        await self._initialize_plugins()
        enabled_count = sum(plugin.enabled for plugin in self.plugins.values())
        logger.info(
            f"Found {len(self.discovered_plugins)} plugins; {enabled_count} enabled"
        )

    @staticmethod
    def _load_plugin_config(plugin_dir: Path) -> dict[str, Any]:
        config_file = plugin_dir / _PLUGIN_CONFIG_FILENAME
        if not config_file.exists():
            return {"enabled": False}
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Error loading plugin config for {plugin_dir.name}: {e}")
            return {}

    def _load_plugin(self, plugin_dir: Path, plugin_config: dict[str, Any]) -> None:
        try:
            plugin_file = plugin_dir / f"{plugin_dir.name}.py"
            if not plugin_file.exists():
                logger.warning(
                    f"Missing plugin file in {plugin_dir.name}: {plugin_dir.name}.py"
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
        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_dir.name}: {e}")

    @staticmethod
    def _load_plugin_module(plugin_dir: Path, plugin_file: Path):
        spec = importlib.util.spec_from_file_location(
            f"plugins.{plugin_dir.name}.plugin", plugin_file
        )
        if spec is None or spec.loader is None:
            logger.warning(f"Failed to load plugin spec: {plugin_dir.name}")
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
            logger.warning(f"No valid plugin class found in {plugin_name.capitalize()}")
            return None
        expected = f"{plugin_name.capitalize()}Plugin"
        for cls in candidates:
            if cls.__name__ == expected:
                return cls
        if len(candidates) == 1:
            return candidates[0]
        names = sorted(cls.__name__ for cls in candidates)
        logger.warning(
            f"Multiple plugin classes found in {plugin_name.capitalize()}: {names}; expected {expected}"
        )
        return None

    def _create_plugin_instance(self, plugin_class, plugin_name, plugin_config):
        utils_provider = {
            "extract_username": utils.extract_username,
            "extract_user_handle": utils.extract_user_handle,
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
                if not await plugin.initialize():
                    logger.warning(f"Plugin {plugin.name} initialization failed")
                    plugin.set_enabled(False)
                    plugin._initialized = False
                    continue
                plugin._initialized = True
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.exception(f"Error initializing plugin {plugin.name}: {e}")
                plugin.set_enabled(False)
                plugin._initialized = False

    async def cleanup_plugins(self) -> None:
        for plugin in self.plugins.values():
            if plugin.enabled:
                try:
                    await plugin.cleanup()
                except Exception as e:
                    if isinstance(e, asyncio.CancelledError):
                        raise
                    logger.exception(f"Error cleaning up plugin {plugin.name}: {e}")

    async def on_startup(self) -> None:
        await self.call_plugin_hook("on_startup")

    async def on_mention(self, mention_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_mention", mention_data)

    async def on_message(self, message_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_message", message_data)

    async def on_reaction(self, reaction_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_reaction", reaction_data)

    async def on_renote(self, renote_data: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_renote", renote_data)

    async def on_notification(
        self, notification_data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await self.call_plugin_hook("on_notification", notification_data)

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
                    f"Unhandled exception in plugin {plugin.name} hook {hook_name}: {e}"
                )
        return results

    def get_plugin_info(self) -> list[dict[str, Any]]:
        loaded = {name: plugin.get_info() for name, plugin in self.plugins.items()}
        configured = {
            name: info
            for name, info in self.discovered_plugins.items()
            if info.get("configured") and name not in loaded
        }
        result: list[dict[str, Any]] = []
        for name in sorted(loaded.keys() | configured.keys()):
            if name in loaded:
                result.append(loaded[name])
            else:
                result.append(configured[name])
        return result

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

    def _find_plugin_dir(self, name: str) -> Path | None:
        if not self.plugins_dir.exists():
            return None
        lowered = name.lower()
        for plugin_dir in self.plugins_dir.iterdir():
            if (
                plugin_dir.is_dir()
                and not plugin_dir.name.startswith(".")
                and plugin_dir.name not in {"__pycache__", "example"}
                and plugin_dir.name.lower() == lowered
            ):
                return plugin_dir
        return None

    async def _cleanup_plugin_instance(self, plugin: PluginBase | None) -> None:
        if not plugin or not getattr(plugin, "_initialized", False):
            return
        try:
            await plugin.cleanup()
            plugin._initialized = False
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.exception(f"Error cleaning up plugin {plugin.name}: {e}")

    @staticmethod
    def _unload_plugin_module(key: str) -> None:
        sys.modules.pop(f"plugins.{key}.plugin", None)

    def _load_plugin_from_dir(self, plugin_dir: Path) -> PluginBase | None:
        self._load_plugin(plugin_dir, self._load_plugin_config(plugin_dir))
        return self.plugins.get(plugin_dir.name)

    @staticmethod
    async def _start_plugin_instance(plugin: PluginBase) -> bool:
        if not plugin.enabled:
            return True
        try:
            if not await plugin.initialize():
                plugin.set_enabled(False)
                plugin._initialized = False
                return False
            if (on_startup := getattr(plugin, "on_startup", None)) is not None:
                await on_startup()
            plugin._initialized = True
            return True
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.exception(f"Error initializing plugin {plugin.name}: {e}")
            plugin.set_enabled(False)
            plugin._initialized = False
            return False

    async def set_plugin_enabled(self, name: str, enabled: bool) -> bool:
        if not (plugin_dir := self._find_plugin_dir(name)):
            return False
        if not (plugin_dir / _PLUGIN_CONFIG_FILENAME).exists():
            return False
        key = plugin_dir.name
        if enabled:
            plugin = self._find_plugin_by_name(key)
            if not plugin:
                self._unload_plugin_module(key)
                if not (plugin := self._load_plugin_from_dir(plugin_dir)):
                    return False
            plugin.set_enabled(True)
            if key in self.discovered_plugins:
                self.discovered_plugins[key]["enabled"] = True
            return await self._start_plugin_instance(plugin)
        plugin = self._find_plugin_by_name(key)
        await self._cleanup_plugin_instance(plugin)
        if plugin:
            plugin.set_enabled(False)
        self.plugins.pop(key, None)
        self._unload_plugin_module(key)
        if key in self.discovered_plugins:
            self.discovered_plugins[key]["enabled"] = False
        return True

    async def reload_plugin(self, name: str) -> bool:
        if not (plugin_dir := self._find_plugin_dir(name)):
            return False
        key = plugin_dir.name
        await self._cleanup_plugin_instance(self._find_plugin_by_name(key))
        self.plugins.pop(key, None)
        self._unload_plugin_module(key)
        if not (plugin := self._load_plugin_from_dir(plugin_dir)):
            return False
        if not plugin.enabled:
            self.plugins.pop(key, None)
            self._unload_plugin_module(key)
            return True
        return await self._start_plugin_instance(plugin)
