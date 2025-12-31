from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "MisskeyBot",
    "BotRunner",
    "BotRuntime",
    "Config",
    "ConfigKeys",
    "MisskeyAPI",
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
    "MisskeyAPI": (".misskey_api", "MisskeyAPI"),
    "OpenAIAPI": (".openai_api", "OpenAIAPI"),
    "StreamingClient": (".streaming", "StreamingClient"),
    "ChannelType": (".streaming", "ChannelType"),
    "PersistenceManager": (".persistence", "PersistenceManager"),
    "ConnectionPool": (".persistence", "ConnectionPool"),
    "PluginBase": (".plugin", "PluginBase"),
    "PluginContext": (".plugin", "PluginContext"),
    "PluginManager": (".plugin", "PluginManager"),
    "TCPClient": (".transport", "TCPClient"),
    "ClientSession": (".transport", "ClientSession"),
}

if TYPE_CHECKING:
    from .bot import MisskeyBot as MisskeyBot
    from .config import Config as Config
    from .constants import ConfigKeys as ConfigKeys
    from .main import BotRunner as BotRunner
    from .misskey_api import MisskeyAPI as MisskeyAPI
    from .openai_api import OpenAIAPI as OpenAIAPI
    from .persistence import ConnectionPool as ConnectionPool
    from .persistence import PersistenceManager as PersistenceManager
    from .plugin import PluginBase as PluginBase
    from .plugin import PluginContext as PluginContext
    from .plugin import PluginManager as PluginManager
    from .runtime import BotRuntime as BotRuntime
    from .streaming import ChannelType as ChannelType
    from .streaming import StreamingClient as StreamingClient
    from .transport import ClientSession as ClientSession
    from .transport import TCPClient as TCPClient


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
