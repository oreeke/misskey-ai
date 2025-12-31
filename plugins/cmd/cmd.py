from typing import Any, Optional

import aiosqlite
from loguru import logger

from src.plugin import PluginBase
from src.utils import get_memory_usage, get_system_info, health_check


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
                "help": {"description": "帮助信息", "aliases": ["帮助"]},
                "status": {"description": "机器人状态", "aliases": ["状态"]},
                "sysinfo": {"description": "系统信息", "aliases": ["系统"]},
                "memory": {"description": "内存使用情况", "aliases": ["内存"]},
                "plugins": {"description": "插件列表", "aliases": ["插件"]},
                "enable": {
                    "description": "启用插件 (用法: enable <插件名>)",
                    "aliases": ["启用"],
                },
                "disable": {
                    "description": "禁用插件 (用法: disable <插件名>)",
                    "aliases": ["禁用"],
                },
                "dbstats": {"description": "数据库统计", "aliases": ["数据库"]},
                "dbclear": {
                    "description": "清理插件数据 (用法: dbclear <插件名> [键名])",
                    "aliases": ["清理"],
                },
            }

    async def initialize(self) -> bool:
        self._log_plugin_action("初始化完成", f"支持 {len(self.commands)} 个命令")
        return True

    def _is_authorized(self, user_id: str, username: str) -> bool:
        return user_id in self.allowed_users or username in self.allowed_users

    def _find_command(self, cmd: str) -> Optional[str]:
        cmd_lower = cmd.lower()
        if cmd_lower in self.commands:
            return cmd_lower
        for command_name, command_info in self.commands.items():
            if cmd_lower in [
                alias.lower() for alias in command_info.get("aliases", [])
            ]:
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
            "dbstats": self._get_db_stats,
            "dbclear": lambda: self._clear_plugin_data(args),
        }
        if command in commands:
            try:
                result = commands[command]()
                return (
                    await result
                    if callable(getattr(result, "__await__", None))
                    else result
                )
            except (OSError, ValueError, RuntimeError) as e:
                logger.error(f"执行命令 {command} 时出错: {e}")
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

    def _get_system_info(self) -> str:
        info = get_system_info()
        return f"系统信息:\n平台: {info['platform']}\nPython 版本: {info['python_version']}\nCPU 核心数: {info['cpu_count']}\n内存总量: {info['memory_total_gb']} GB\n进程 ID: {info['process_id']}"

    def _get_memory_usage(self) -> str:
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

    def _toggle_plugin(self, plugin_name: str, enable: bool) -> str:
        if not plugin_name.strip():
            return "请指定插件名称"
        name = plugin_name.strip()
        method = (
            self.plugin_manager.enable_plugin
            if enable
            else self.plugin_manager.disable_plugin
        )
        action = "启用" if enable else "禁用"
        past_action = "已启用" if enable else "已禁用"
        if method(name):
            return f"插件 {name} {past_action}"
        return f"插件 {name} 不存在或{action}失败"

    def _enable_plugin(self, plugin_name: str) -> str:
        return self._toggle_plugin(plugin_name, True)

    def _disable_plugin(self, plugin_name: str) -> str:
        return self._toggle_plugin(plugin_name, False)

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
            return "请指定插件名称"
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

    def _create_response(self, response_text: str) -> Optional[dict[str, Any]]:
        try:
            response = {
                "handled": True,
                "plugin_name": self.name,
                "response": response_text,
            }
            return response if self._validate_plugin_response(response) else None
        except ValueError as e:
            logger.error(f"创建响应时出错: {e}")
            return None

    async def on_message(
        self, message_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        try:
            text = message_data.get("text", "").strip()
            if not text.startswith("^"):
                return None
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
                self._log_plugin_action("执行命令", f"@{username}: ^{command_text}")
                result = await self._execute_command(command_name, args)
                return self._create_response(result)
            return self._create_response(
                f"未知命令: {parts[0]}\n使用 ^help 查看可用命令。"
            )
        except (ValueError, KeyError, AttributeError) as e:
            logger.error(f"处理命令时出错: {e}")
            return self._create_response("命令处理失败，请稍后重试。")
