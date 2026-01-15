import asyncio
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from misskey_ai.plugin import PluginBase
from misskey_ai.shared.constants import ConfigKeys
from misskey_ai.shared.utils import normalize_tokens

from .handlers import CmdHandlersMixin


class CmdPlugin(CmdHandlersMixin, PluginBase):
    description = "命令插件，在聊天中使用 ^ 开头的命令管理机器人"

    def __init__(self, context):
        super().__init__(context)
        self.allowed_users = self.config.get("allowed_users", [])
        self.commands = self.config.get("commands", {})
        self._setup_default_commands()
        self._init_baselines()
        self._command_alias_index = self._build_command_alias_index()
        self._command_handlers = self._build_command_handlers()

    def _global_get(self, key: str, default: Any) -> Any:
        cfg = getattr(self, "global_config", None)
        return cfg.get(key) if cfg else default

    def _init_baselines(self) -> None:
        self._baseline_response_whitelist = normalize_tokens(
            self._global_get(ConfigKeys.BOT_RESPONSE_WHITELIST, []), lower=True
        )
        self._baseline_response_blacklist = normalize_tokens(
            self._global_get(ConfigKeys.BOT_RESPONSE_BLACKLIST, []), lower=True
        )
        self._baseline_antenna_selectors = normalize_tokens(
            self._global_get(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS, [])
        )

    def _build_command_alias_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for name, info in self.commands.items():
            if not isinstance(name, str) or not name:
                continue
            aliases = info.get("aliases", []) if isinstance(info, dict) else []
            for alias in aliases or []:
                if isinstance(alias, str) and (a := alias.strip()):
                    index.setdefault(a.lower(), name)
        return index

    def _build_command_handlers(self) -> dict[str, Any]:
        return {
            "help": lambda args: self._get_help_text(),
            "status": lambda args: self._get_status_text(),
            "sysinfo": lambda args: self._get_system_info(),
            "model": self._handle_model,
            "autopost": lambda args: self._handle_set_bool(
                "autopost", ConfigKeys.BOT_AUTO_POST_ENABLED, args
            ),
            "mention": lambda args: self._handle_set_bool(
                "mention", ConfigKeys.BOT_RESPONSE_MENTION, args
            ),
            "chat": lambda args: self._handle_set_bool(
                "chat", ConfigKeys.BOT_RESPONSE_CHAT, args
            ),
            "plugins": lambda args: self._get_plugins_info(),
            "enable": self._enable_plugin,
            "disable": self._disable_plugin,
            "reload": self._reload_plugin,
            "timeline": self._handle_timeline,
            "antenna": self._handle_antenna,
            "cache": lambda args: self._get_memory_usage(),
            "cacheclear": self._clear_memory_caches,
            "whitelist": lambda args: self._handle_response_user_list(
                "whitelist",
                ConfigKeys.BOT_RESPONSE_WHITELIST,
                args,
                self._baseline_response_whitelist,
            ),
            "blacklist": lambda args: self._handle_response_user_list(
                "blacklist",
                ConfigKeys.BOT_RESPONSE_BLACKLIST,
                args,
                self._baseline_response_blacklist,
            ),
            "dbstats": lambda args: self._get_db_stats(),
            "dbclear": self._clear_plugin_data,
        }

    def _setup_default_commands(self):
        if not self.commands:
            self.commands = {
                "help": {"description": "帮助信息", "aliases": []},
                "status": {"description": "机器人状态", "aliases": []},
                "sysinfo": {"description": "系统信息", "aliases": []},
                "model": {
                    "description": "查看/切换模型 (用法: model [模型名]|reset)",
                    "aliases": [],
                },
                "autopost": {
                    "description": "自动发帖开关 (用法: autopost on|off)",
                    "aliases": [],
                },
                "mention": {
                    "description": "响应提及开关 (用法: mention on|off)",
                    "aliases": [],
                },
                "chat": {
                    "description": "响应聊天开关 (用法: chat on|off)",
                    "aliases": [],
                },
                "plugins": {"description": "插件列表", "aliases": []},
                "enable": {
                    "description": "启用插件 (用法: enable <插件名>)",
                    "aliases": [],
                },
                "disable": {
                    "description": "禁用插件 (用法: disable <插件名>)",
                    "aliases": [],
                },
                "reload": {
                    "description": "重启插件 (用法: reload <插件名>)",
                    "aliases": [],
                },
                "timeline": {
                    "description": "查看/切换时间线订阅 (用法: timeline [status|add|del|set|clear|reset])",
                    "aliases": [],
                },
                "antenna": {
                    "description": "查看/切换天线订阅 (用法: antenna [status|list|add|del|set|clear|reset])",
                    "aliases": [],
                },
                "cache": {"description": "内存使用情况", "aliases": []},
                "cacheclear": {
                    "description": "清理内存缓存 (用法: cacheclear [chat|locks|events|all])",
                    "aliases": [],
                },
                "whitelist": {
                    "description": "查看/修改白名单 (用法: whitelist [list|add|del|set|clear|reset])",
                    "aliases": [],
                },
                "blacklist": {
                    "description": "查看/修改黑名单 (用法: blacklist [list|add|del|set|clear|reset])",
                    "aliases": [],
                },
                "dbstats": {"description": "数据库统计", "aliases": []},
                "dbclear": {
                    "description": "清理插件数据 (用法: dbclear <插件名> [键名])",
                    "aliases": [],
                },
            }

    async def initialize(self) -> bool:
        self._log_plugin_action("initialized", f"Command groups: {len(self.commands)}")
        return True

    async def on_startup(self) -> None:
        if not getattr(self, "db", None):
            return
        if getattr(self, "openai", None):
            model = await self.db.get_plugin_data(self.name, ConfigKeys.OPENAI_MODEL)
            if model:
                self.openai.model = model
                self._set_global_config_value(ConfigKeys.OPENAI_MODEL, model)
                self._log_plugin_action("applied model override", model)
        await self._apply_saved_response_user_list(ConfigKeys.BOT_RESPONSE_WHITELIST)
        await self._apply_saved_response_user_list(ConfigKeys.BOT_RESPONSE_BLACKLIST)

    def _set_global_config_value(self, path: str, value: Any) -> None:
        keys = path.split(".")
        config = self.global_config.data
        for key in keys[:-1]:
            if not isinstance(config.get(key), dict):
                config[key] = {}
            config = config[key]
        config[keys[-1]] = value

    def _is_authorized(self, user_id: str, handle: str | None) -> bool:
        return user_id in self.allowed_users or (
            handle is not None and handle in self.allowed_users
        )

    def _canonical_handle(self, username: str, handle: str | None) -> str | None:
        if isinstance(handle, str) and (h := handle.strip()):
            if "@" in h:
                return h
            if username and h != username:
                return None
        if not username or username == "unknown":
            return None
        misskey = getattr(self, "misskey", None)
        instance_url = getattr(misskey, "instance_url", None) if misskey else None
        if not isinstance(instance_url, str) or not instance_url:
            return None
        host = urlparse(instance_url).hostname
        return f"{username}@{host}" if host else None

    def _find_command(self, cmd: str) -> str | None:
        cmd_lower = cmd.lower()
        if cmd_lower in self.commands:
            return cmd_lower
        return self._command_alias_index.get(cmd_lower)

    async def _execute_command(self, command: str, args: str = "") -> str:
        handler = self._command_handlers.get(command)
        if not handler:
            return f"未知命令: {command}"
        try:
            result = handler(args)
            return await result if asyncio.iscoroutine(result) else result
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error executing command {command}: {e}")
            return f"命令执行失败: {str(e)}"

    def _create_response(self, response_text: str) -> dict[str, Any] | None:
        try:
            response = {
                "handled": True,
                "plugin_name": self.name,
                "response": response_text,
            }
            return response if self._validate_plugin_response(response) else None
        except Exception as e:
            logger.error(f"Error creating response: {e}")
            return None

    async def on_message(self, message_data: dict[str, Any]) -> dict[str, Any] | None:
        raw = message_data.get("text") or message_data.get("content") or ""
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text.startswith("^"):
            return None
        try:
            user_id = self._extract_user_id(message_data)
            username = self._extract_username(message_data)
            handle = self._canonical_handle(
                username, self._extract_user_handle(message_data)
            )
            if not user_id:
                return None
            if not self._is_authorized(user_id, handle):
                return self._create_response("您没有权限使用命令。")
            command_text = text[1:].strip()
            parts = command_text.split(maxsplit=1)
            command_name = self._find_command(parts[0])
            args = parts[1] if len(parts) > 1 else ""
            if command_name:
                who = handle or username
                self._log_plugin_action("ran command", f"@{who}: ^{command_text}")
                result = await self._execute_command(command_name, args)
                return self._create_response(result)
            return self._create_response(
                f"未知命令: {parts[0]}\n使用 ^help 查看可用命令。"
            )
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Error handling command: {e}")
            return self._create_response("命令处理失败，请稍后重试。")
