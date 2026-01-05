import asyncio
from typing import Any

import aiosqlite
from loguru import logger

from src.constants import ConfigKeys
from src.plugin import PluginBase
from src.streaming import ChannelType
from src.utils import get_memory_usage, get_system_info, health_check

_MSG_SPECIFY_PLUGIN_NAME = "请指定插件名称"


class CmdPlugin(PluginBase):
    description = "命令插件，在聊天中使用 ^ 开头的命令管理机器人"

    def __init__(self, context):
        super().__init__(context)
        self.allowed_users = self.config.get("allowed_users", [])
        self.commands = self.config.get("commands", {})
        self._setup_default_commands()

    def _setup_default_commands(self):
        if not self.commands:
            self.commands = {
                "help": {"description": "帮助信息", "aliases": []},
                "status": {"description": "机器人状态", "aliases": []},
                "sysinfo": {"description": "系统信息", "aliases": []},
                "memory": {"description": "内存使用情况", "aliases": []},
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
                "model": {
                    "description": "查看/切换模型 (用法: model [模型名]|reset)",
                    "aliases": [],
                },
                "timeline": {
                    "description": "查看/切换时间线订阅 (用法: timeline [status|add|del|set|clear|reset] ...)",
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
        if not getattr(self, "persistence_manager", None) or not getattr(
            self, "openai", None
        ):
            return
        model = await self.persistence_manager.get_plugin_data(
            self.name, ConfigKeys.OPENAI_MODEL
        )
        if not model:
            return
        self.openai.model = model
        self._set_global_config_value(ConfigKeys.OPENAI_MODEL, model)
        self._log_plugin_action("applied model override", model)

    def _is_authorized(self, user_id: str, username: str) -> bool:
        return user_id in self.allowed_users or username in self.allowed_users

    def _find_command(self, cmd: str) -> str | None:
        cmd_lower = cmd.lower()
        if cmd_lower in self.commands:
            return cmd_lower
        for command_name, command_info in self.commands.items():
            if cmd_lower in (
                alias.lower() for alias in command_info.get("aliases", [])
            ):
                return command_name
        return None

    async def _execute_command(self, command: str, args: str = "") -> str:
        commands = {
            "help": self._get_help_text,
            "status": self._get_status_text,
            "sysinfo": self._get_system_info,
            "memory": self._get_memory_usage,
            "plugins": self._get_plugins_info,
            "enable": lambda: self._enable_plugin(args),
            "disable": lambda: self._disable_plugin(args),
            "reload": lambda: self._reload_plugin(args),
            "model": lambda: self._handle_model(args),
            "timeline": lambda: self._handle_timeline(args),
            "dbstats": self._get_db_stats,
            "dbclear": lambda: self._clear_plugin_data(args),
        }
        if command in commands:
            try:
                result = commands[command]()
                return await result if asyncio.iscoroutine(result) else result
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.error(f"Error executing command {command}: {e}")
                return f"命令执行失败: {str(e)}"
        return f"未知命令: {command}"

    def _get_help_text(self) -> str:
        help_lines = ["可用命令:"]
        for cmd_name, cmd_info in self.commands.items():
            desc = cmd_info.get("description", "无描述")
            aliases = cmd_info.get("aliases", [])
            alias_text = f" ({', '.join(aliases)})" if aliases else ""
            help_lines.append(f"  ^{cmd_name}{alias_text} - {desc}")
        return "\n".join(help_lines)

    def _get_status_text(self) -> str:
        status = "运行中" if health_check() else "异常"
        allowed_count = len(self.allowed_users)
        return f"机器人状态: {status}\n授权用户数: {allowed_count}"

    @staticmethod
    def _get_system_info() -> str:
        info = get_system_info()
        return f"系统信息:\n平台: {info['platform']}\nPython 版本: {info['python_version']}\nCPU 核心数: {info['cpu_count']}\n内存总量: {info['memory_total_gb']} GB\n进程 ID: {info['process_id']}"

    @staticmethod
    def _get_memory_usage() -> str:
        process_usage = get_memory_usage()
        return f"内存使用: {process_usage['rss_mb']} MB"

    def _get_plugins_info(self) -> str:
        plugins = self.plugin_manager.get_plugin_info()
        if not plugins:
            return "当前没有加载任何插件"
        info_lines = ["插件信息:"]
        for plugin in plugins:
            status = "已启用" if plugin.get("enabled", False) else "已禁用"
            info_lines.append(
                f"  {plugin['name']} - {plugin.get('description', '无描述')} [{status}]"
            )
        return "\n".join(info_lines)

    async def _toggle_plugin(self, plugin_name: str, enable: bool) -> str:
        if not plugin_name.strip():
            return _MSG_SPECIFY_PLUGIN_NAME
        name = plugin_name.strip()
        action = "启用" if enable else "禁用"
        past_action = "已启用" if enable else "已禁用"
        if hasattr(self.plugin_manager, "set_plugin_enabled"):
            if await self.plugin_manager.set_plugin_enabled(name, enable):
                return f"插件 {name} {past_action}"
            return f"插件 {name} 不存在或{action}失败"
        method = (
            self.plugin_manager.enable_plugin
            if enable
            else self.plugin_manager.disable_plugin
        )
        if method(name):
            return f"插件 {name} {past_action}"
        return f"插件 {name} 不存在或{action}失败"

    async def _enable_plugin(self, plugin_name: str) -> str:
        return await self._toggle_plugin(plugin_name, True)

    async def _disable_plugin(self, plugin_name: str) -> str:
        return await self._toggle_plugin(plugin_name, False)

    async def _reload_plugin(self, plugin_name: str) -> str:
        if not plugin_name.strip():
            return _MSG_SPECIFY_PLUGIN_NAME
        if not getattr(self, "plugin_manager", None) or not hasattr(
            self.plugin_manager, "reload_plugin"
        ):
            return "插件管理器不支持重启插件"
        name = plugin_name.strip()
        if await self.plugin_manager.reload_plugin(name):
            return f"插件 {name} 已重启并重读配置"
        return f"插件 {name} 不存在或重启失败"

    async def _get_db_stats(self) -> str:
        try:
            stats = await self.persistence_manager.get_table_stats()
            if not stats:
                return "数据库为空"
            info_lines = ["数据库统计:"]
            for table, table_info in stats.items():
                row_count = table_info.get("row_count", 0)
                size_kb = table_info.get("size_kb", 0)
                size_mb = table_info.get("size_mb", 0)
                size_str = f"{size_mb} MB" if size_mb >= 1 else f"{size_kb} KB"
                info_lines.append(f"  {table} {size_str} ({row_count} recs)")
            return "\n".join(info_lines)
        except aiosqlite.Error as e:
            return f"获取数据库统计失败: {str(e)}"

    async def _clear_plugin_data(self, args: str) -> str:
        if not args.strip():
            return _MSG_SPECIFY_PLUGIN_NAME
        parts = args.strip().split()
        plugin_name = parts[0]
        key = parts[1] if len(parts) > 1 else None
        try:
            count = await self.persistence_manager.delete_plugin_data(plugin_name, key)
            if key:
                return (
                    f"已删除插件 {plugin_name} 的 {key} 数据"
                    if count > 0
                    else "未找到指定数据"
                )
            return (
                f"已删除插件 {plugin_name} 的 {count} 条数据"
                if count > 0
                else "未找到指定插件数据"
            )
        except aiosqlite.Error as e:
            return f"删除数据失败: {str(e)}"

    def _set_global_config_value(self, path: str, value: Any) -> None:
        keys = path.split(".")
        config = self.global_config.data
        for key in keys[:-1]:
            if not isinstance(config.get(key), dict):
                config[key] = {}
            config = config[key]
        config[keys[-1]] = value

    async def _handle_model(self, args: str) -> str:
        arg = args.strip()
        if not getattr(self, "openai", None):
            return "OpenAI 客户端未初始化"
        if not arg:
            saved = await self.persistence_manager.get_plugin_data(
                self.name, ConfigKeys.OPENAI_MODEL
            )
            return (
                f"当前模型: {self.openai.model}"
                if not saved
                else f"当前模型: {self.openai.model}\n已保存覆盖: {saved}"
            )
        if arg.lower() in {"reset", "default"}:
            await self.persistence_manager.delete_plugin_data(
                self.name, ConfigKeys.OPENAI_MODEL
            )
            await self.global_config.load()
            model = self.global_config.get(ConfigKeys.OPENAI_MODEL)
            self.openai.model = model
            self._set_global_config_value(ConfigKeys.OPENAI_MODEL, model)
            return f"已恢复默认模型: {model}"
        model = arg
        self.openai.model = model
        self._set_global_config_value(ConfigKeys.OPENAI_MODEL, model)
        await self.persistence_manager.set_plugin_data(
            self.name, ConfigKeys.OPENAI_MODEL, model
        )
        return f"已切换模型: {model}"

    @staticmethod
    def _timeline_name_map() -> dict[str, str]:
        mapped = {
            "home": ChannelType.HOME_TIMELINE.value,
            "local": ChannelType.LOCAL_TIMELINE.value,
            "hybrid": ChannelType.HYBRID_TIMELINE.value,
            "global": ChannelType.GLOBAL_TIMELINE.value,
        }
        for v in tuple(mapped.values()):
            mapped[v.lower()] = v
        return mapped

    def _format_timeline_status(self) -> str:
        bot = getattr(self, "bot", None)
        if not bot:
            return "Bot 未注入，无法管理时间线订阅"
        desired = getattr(bot, "timeline_channels", set()) or set()
        desired_text = ", ".join(sorted(desired)) if desired else "(空)"
        connected = getattr(bot, "streaming", None)
        if connected and getattr(connected, "channels", None):
            connected_names = sorted(
                {
                    info.get("name")
                    for info in connected.channels.values()
                    if info.get("name")
                }
            )
        else:
            connected_names = []
        connected_text = ", ".join(connected_names) if connected_names else "(空)"
        return f"期望订阅: {desired_text}\n当前连接: {connected_text}"

    async def _handle_timeline(self, args: str) -> str:
        bot = getattr(self, "bot", None)
        if not bot:
            return "Bot 未注入，无法管理时间线订阅"
        tokens = args.strip().split()
        if not tokens or tokens[0].lower() in {"status", "show"}:
            return self._format_timeline_status()
        action = tokens[0].lower()
        name_map = self._timeline_name_map()
        if action in {"reset", "default"}:
            bot.timeline_channels = bot.load_timeline_channels()
            await bot.restart_streaming()
            return "已重置时间线订阅\n" + self._format_timeline_status()
        if action in {"clear", "off"}:
            bot.timeline_channels = set()
            await bot.restart_streaming()
            return "已清空时间线订阅\n" + self._format_timeline_status()
        if action not in {"add", "enable", "del", "remove", "disable", "set"}:
            return (
                "用法: ^timeline [status|add|del|set|clear|reset] ...\n"
                "示例: ^timeline add home\n"
                "示例: ^timeline set home local\n"
                "可选: home/local/hybrid/global"
            )
        raw_names = [t.strip().lower() for t in tokens[1:] if t.strip()]
        if not raw_names:
            return "请指定时间线名称: home/local/hybrid/global"
        resolved: set[str] = set()
        for raw in raw_names:
            if raw in name_map:
                resolved.add(name_map[raw])
            else:
                return f"未知时间线: {raw}\n可选: home/local/hybrid/global"
        current = set(getattr(bot, "timeline_channels", set()) or set())
        if action in {"add", "enable"}:
            bot.timeline_channels = current | resolved
        elif action in {"del", "remove", "disable"}:
            bot.timeline_channels = current - resolved
        else:
            bot.timeline_channels = resolved
        await bot.restart_streaming()
        return "已更新时间线订阅\n" + self._format_timeline_status()

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
            if not user_id:
                return None
            if not self._is_authorized(user_id, username):
                return self._create_response("您没有权限使用命令。")
            command_text = text[1:].strip()
            parts = command_text.split(maxsplit=1)
            command_name = self._find_command(parts[0])
            args = parts[1] if len(parts) > 1 else ""
            if command_name:
                self._log_plugin_action("ran command", f"@{username}: ^{command_text}")
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
