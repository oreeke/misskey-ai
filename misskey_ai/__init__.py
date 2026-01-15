from __future__ import annotations

from typing import TYPE_CHECKING, Any

_MISSKEY_API_MODULE = ".clients.misskey.misskey_api"
_MISSKEY_DRIVE_MODULE = ".clients.misskey.drive"
_CHANNELS_MODULE = ".clients.misskey.channels"
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
    "DBManager",
    "ConnectionPool",
    "PluginBase",
    "PluginContext",
    "PluginManager",
    "TCPClient",
    "ClientSession",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "MisskeyBot": (".bot.core", "MisskeyBot"),
    "BotRunner": (".app.main", "BotRunner"),
    "BotRuntime": (".bot.runtime", "BotRuntime"),
    "Config": (".shared.config", "Config"),
    "ConfigKeys": (".shared.constants", "ConfigKeys"),
    "MisskeyAPI": (_MISSKEY_API_MODULE, "MisskeyAPI"),
    "MisskeyDrive": (_MISSKEY_DRIVE_MODULE, "MisskeyDrive"),
    "DriveIO": (_MISSKEY_DRIVE_MODULE, "DriveIO"),
    "OpenAIAPI": (".clients.openai.openai_api", "OpenAIAPI"),
    "StreamingClient": (".clients.misskey.streaming", "StreamingClient"),
    "ChannelType": (_CHANNELS_MODULE, "ChannelType"),
    "DBManager": (".db.sqlite", "DBManager"),
    "ConnectionPool": (".db.sqlite", "ConnectionPool"),
    "PluginBase": (_PLUGIN_MODULE, "PluginBase"),
    "PluginContext": (_PLUGIN_MODULE, "PluginContext"),
    "PluginManager": (_PLUGIN_MODULE, "PluginManager"),
    "TCPClient": (".clients.misskey.transport", "TCPClient"),
    "ClientSession": (".clients.misskey.transport", "ClientSession"),
}

if TYPE_CHECKING:
    from .app.main import BotRunner
    from .bot.core import MisskeyBot
    from .plugin import PluginBase, PluginContext, PluginManager
    from .bot.runtime import BotRuntime
    from .clients.misskey.misskey_api import MisskeyAPI
    from .clients.misskey.drive import DriveIO, MisskeyDrive
    from .clients.openai.openai_api import OpenAIAPI
    from .clients.misskey.channels import ChannelType
    from .clients.misskey.streaming import StreamingClient
    from .clients.misskey.transport import ClientSession, TCPClient
    from .shared.config import Config
    from .shared.constants import ConfigKeys
    from .db.sqlite import ConnectionPool, DBManager


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
