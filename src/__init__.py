from __future__ import annotations

from typing import TYPE_CHECKING, Any

_MISSKEY_API_MODULE = ".misskey_api"
_PLUGIN_MODULE = ".plugin"

__all__ = [
    "MisskeyBot",
    "BotRunner",
    "BotRuntime",
    "Config",
    "ConfigKeys",
    "MisskeyAPI",
    "MisskeyDrive",
    "DriveIO",
    "OpenAIAPI",
    "StreamingClient",
    "ChannelType",
    "PersistenceManager",
    "ConnectionPool",
    "PluginBase",
    "PluginContext",
    "PluginManager",
    "TCPClient",
    "ClientSession",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "MisskeyBot": (".bot", "MisskeyBot"),
    "BotRunner": (".main", "BotRunner"),
    "BotRuntime": (".runtime", "BotRuntime"),
    "Config": (".config", "Config"),
    "ConfigKeys": (".constants", "ConfigKeys"),
    "MisskeyAPI": (_MISSKEY_API_MODULE, "MisskeyAPI"),
    "MisskeyDrive": (_MISSKEY_API_MODULE, "MisskeyDrive"),
    "DriveIO": (_MISSKEY_API_MODULE, "DriveIO"),
    "OpenAIAPI": (".openai_api", "OpenAIAPI"),
    "StreamingClient": (".streaming", "StreamingClient"),
    "ChannelType": (".streaming", "ChannelType"),
    "PersistenceManager": (".persistence", "PersistenceManager"),
    "ConnectionPool": (".persistence", "ConnectionPool"),
    "PluginBase": (_PLUGIN_MODULE, "PluginBase"),
    "PluginContext": (_PLUGIN_MODULE, "PluginContext"),
    "PluginManager": (_PLUGIN_MODULE, "PluginManager"),
    "TCPClient": (".transport", "TCPClient"),
    "ClientSession": (".transport", "ClientSession"),
}

if TYPE_CHECKING:
    from .bot import MisskeyBot
    from .config import Config
    from .constants import ConfigKeys
    from .main import BotRunner
    from .misskey_api import DriveIO, MisskeyAPI, MisskeyDrive
    from .openai_api import OpenAIAPI
    from .persistence import ConnectionPool, PersistenceManager
    from .plugin import PluginBase, PluginContext, PluginManager
    from .runtime import BotRuntime
    from .streaming import ChannelType, StreamingClient
    from .transport import ClientSession, TCPClient


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attr_name = _EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()).union(_EXPORTS))
