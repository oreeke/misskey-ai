import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from twipsybot.clients.misskey.channels import ChannelType
from twipsybot.shared.config_keys import ConfigKeys
from twipsybot.shared.utils import (
    get_memory_usage,
    get_system_info,
    normalize_tokens,
)

_MSG_SPECIFY_PLUGIN_NAME = "请指定插件名称"
_MSG_BOT_NOT_INJECTED_ANTENNA = "Bot 未注入，无法管理天线订阅"
_MSG_GLOBAL_CONFIG_NOT_INJECTED = "全局配置未注入"


class CmdHandlersMixin:
    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = int(max(0, seconds))
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        base = f"{hours:02}:{minutes:02}:{seconds:02}"
        return f"{days}d {base}" if days else base

    def _get_uptime_text(self, bot: Any) -> str | None:
        runtime = getattr(bot, "runtime", None)
        if runtime is None:
            return None
        started_at = getattr(runtime, "startup_time", None)
        if not isinstance(started_at, datetime):
            return None
        seconds = (datetime.now(UTC) - started_at).total_seconds()
        return self._format_duration(seconds)

    def _get_feature_toggle_text(self) -> str:
        cfg = getattr(self, "global_config", None)
        cfg_get = cfg.get if cfg else (lambda k, d=None: d)
        chat_on = "on" if bool(cfg_get(ConfigKeys.BOT_RESPONSE_CHAT)) else "off"
        mention_on = "on" if bool(cfg_get(ConfigKeys.BOT_RESPONSE_MENTION)) else "off"
        autopost_on = "on" if bool(cfg_get(ConfigKeys.BOT_AUTO_POST_ENABLED)) else "off"
        return f"开关: chat={chat_on} mention={mention_on} autopost={autopost_on}"

    def _get_plugin_status_text(self) -> str | None:
        plugin_manager = getattr(self, "plugin_manager", None)
        if plugin_manager is None or not hasattr(plugin_manager, "get_plugin_info"):
            return None
        plugins = plugin_manager.get_plugin_info()
        if not plugins:
            return None
        plugin_enabled = sum(1 for p in plugins if p.get("enabled") is True)
        return f"插件: {plugin_enabled}/{len(plugins)} 已启用"

    @staticmethod
    def _get_bot_account_text(bot: Any) -> str | None:
        bot_username = getattr(bot, "bot_username", None)
        if not bot_username:
            return None
        bot_user_id = getattr(bot, "bot_user_id", None)
        suffix = f" ({bot_user_id})" if bot_user_id else ""
        return f"Bot: @{bot_username}{suffix}"

    @staticmethod
    def _get_model_text(bot: Any) -> str | None:
        model = getattr(getattr(bot, "openai", None), "model", None)
        return f"模型: {model}" if model else None

    def _handle_set_bool(self, label: str, key: str, args: str) -> str:
        if not getattr(self, "global_config", None):
            return _MSG_GLOBAL_CONFIG_NOT_INJECTED
        action = (args or "").strip().lower()
        if action not in {"on", "off"}:
            return f"用法: ^{label} on|off"
        value = action == "on"
        self._set_global_config_value(key, value)
        return f"{label}: {'on' if value else 'off'}"

    def _get_help_text(self) -> str:
        entries = []
        for cmd_name, cmd_info in self.commands.items():
            desc = cmd_info.get("description", "无描述")
            aliases = cmd_info.get("aliases", [])
            alias_text = f" ({', '.join(aliases)})" if aliases else ""
            entries.append((f"^{cmd_name}{alias_text}", desc))
        max_width = max((len(left) for left, _ in entries), default=0)
        help_lines = []
        for left, desc in entries:
            help_lines.append(f"  {left.ljust(max_width)} - {desc}")
        return "\n".join(help_lines)

    def _get_status_text(self) -> str:
        bot = getattr(self, "bot", None)
        allowed_count = len(self.allowed_users)
        if not bot:
            return f"机器人状态: 未注入\n授权用户数: {allowed_count}"

        runtime_running = bool(getattr(getattr(bot, "runtime", None), "running", False))
        status = "运行中" if runtime_running else "未运行"

        parts = [f"机器人状态: {status}"]
        if uptime := self._get_uptime_text(bot):
            parts.append(f"运行时长: {uptime}")
        if bot_account := self._get_bot_account_text(bot):
            parts.append(bot_account)
        if model := self._get_model_text(bot):
            parts.append(model)
        parts.append(self._get_feature_toggle_text())
        if plugin_status := self._get_plugin_status_text():
            parts.append(plugin_status)
        parts.append(f"授权用户数: {allowed_count}")
        return "\n".join(parts)

    @staticmethod
    def _get_system_info() -> str:
        info = get_system_info()
        return f"系统信息:\n平台: {info['platform']}\nPython 版本: {info['python_version']}\nCPU 核心数: {info['cpu_count']}\n内存总量: {info['memory_total_gb']} GB\n进程 ID: {info['process_id']}"

    @staticmethod
    def _get_memory_usage() -> str:
        process_usage = get_memory_usage()
        return f"内存使用: {process_usage['rss_mb']} MB"

    @staticmethod
    def _clear_cache(cache) -> int | None:
        if cache is None:
            return None
        before = len(cache)
        cache.clear()
        return before

    def _clear_memory_caches(self, args: str) -> str:
        target = (args or "").strip().lower()
        bot = getattr(self, "bot", None)
        if not bot:
            return "Bot 未注入，无法清理缓存"

        cleared = []
        getters = {
            "chat": lambda b: getattr(b, "_chat_histories", None),
            "locks": lambda b: getattr(b, "_user_locks", None),
            "events": lambda b: getattr(
                getattr(b, "streaming", None), "processed_events", None
            ),
        }
        selected = (
            ("chat", "locks", "events") if not target or target == "all" else (target,)
        )
        for key in selected:
            getter = getters.get(key)
            if not getter:
                continue
            if (before := self._clear_cache(getter(bot))) is not None:
                cleared.append(f"{key}:{before}")

        if not cleared:
            return "未清理任何缓存"
        return "已清理缓存: " + ", ".join(cleared)

    def _get_plugins_info(self) -> str:
        plugins = self.plugin_manager.get_plugin_info()
        if not plugins:
            return "当前没有加载任何插件"
        entries = []
        for plugin in plugins:
            name = str(plugin.get("name") or "")
            desc = plugin.get("description", "无描述")
            status = "已启用" if plugin.get("enabled", False) else "已禁用"
            entries.append((name, desc, status))
        max_width = max((len(name) for name, _, _ in entries), default=0)
        info_lines = []
        for name, desc, status in entries:
            info_lines.append(f"  {name.ljust(max_width)} - [{status}] {desc}")
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
            stats = await self.db.get_table_stats()
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
            return f"获取数据库统计失败: {e!s}"

    async def _clear_plugin_data(self, args: str) -> str:
        if not args.strip():
            return _MSG_SPECIFY_PLUGIN_NAME
        parts = args.strip().split()
        plugin_name = parts[0]
        key = parts[1] if len(parts) > 1 else None
        try:
            count = await self.db.delete_plugin_data(plugin_name, key)
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
            return f"删除数据失败: {e!s}"

    async def _handle_model(self, args: str) -> str:
        arg = args.strip()
        if not getattr(self, "openai", None):
            return "OpenAI 客户端未初始化"
        if not arg:
            saved = await self.db.get_plugin_data(self.name, ConfigKeys.OPENAI_MODEL)
            return (
                f"当前模型: {self.openai.model}"
                if not saved
                else f"当前模型: {self.openai.model}\n已保存覆盖: {saved}"
            )
        if arg.lower() in {"reset", "default"}:
            await self.db.delete_plugin_data(self.name, ConfigKeys.OPENAI_MODEL)
            await self.global_config.load()
            model = self.global_config.get(ConfigKeys.OPENAI_MODEL)
            self.openai.model = model
            self._set_global_config_value(ConfigKeys.OPENAI_MODEL, model)
            return f"已恢复默认模型: {model}"
        model = arg
        self.openai.model = model
        self._set_global_config_value(ConfigKeys.OPENAI_MODEL, model)
        await self.db.set_plugin_data(self.name, ConfigKeys.OPENAI_MODEL, model)
        return f"已切换模型: {model}"

    @staticmethod
    def _format_code_block(title: str, lines: list[str]) -> str:
        t = (title or "").strip()
        if not t:
            t = "输出"
        if not t.endswith((":", "：")):
            t += ":"
        body = [line for line in lines if isinstance(line, str)]
        if not body:
            body = ["(空)"]
        return "\n".join([t, "```", *body, "```"])

    @staticmethod
    def _format_plain_list(items: list[str]) -> str:
        return "\n".join(items) if items else "(空)"

    @staticmethod
    def _format_plain_list_update(message: str, items: list[str]) -> str:
        body = [message, "", *(items if items else ["(空)"])]
        return "\n".join(body)

    async def _apply_saved_response_user_list(self, key: str) -> None:
        if not getattr(self, "db", None):
            return
        saved = await self.db.get_plugin_data(self.name, key)
        if not saved:
            return
        try:
            decoded = json.loads(saved)
        except json.JSONDecodeError:
            decoded = saved
        normalized = normalize_tokens(decoded, lower=True)
        self._set_global_config_value(key, normalized)
        self._log_plugin_action("applied config override", f"{key}={len(normalized)}")

    async def _save_response_user_list(self, key: str, items: list[str]) -> None:
        self._set_global_config_value(key, items)
        if not getattr(self, "db", None):
            return
        await self.db.set_plugin_data(
            self.name, key, json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        )

    async def _reset_response_user_list(self, key: str, baseline: list[str]) -> None:
        self._set_global_config_value(key, list(baseline))
        if not getattr(self, "db", None):
            return
        await self.db.delete_plugin_data(self.name, key)

    async def _handle_response_user_list(
        self, label: str, key: str, args: str, baseline: list[str]
    ) -> str:
        if not getattr(self, "global_config", None):
            return _MSG_GLOBAL_CONFIG_NOT_INJECTED
        raw = args.strip()
        current = normalize_tokens(self.global_config.get(key), lower=True)
        if not raw:
            return self._format_plain_list(current)
        parts = raw.split(maxsplit=1)
        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if action in {"list", "status", "show"}:
            return self._format_plain_list(current)
        if action in {"clear", "empty"}:
            await self._save_response_user_list(key, [])
            return self._format_plain_list_update(f"已清空 {label}", [])
        if action in {"reset", "default"}:
            await self._reset_response_user_list(key, baseline)
            return self._format_plain_list_update(f"已恢复 {label}", list(baseline))
        if action in {"add", "+", "append"}:
            items = normalize_tokens(rest, lower=True)
            if not items:
                return f"用法: ^{label} add <username@host|userId>"
            s = set(current)
            updated = current + [i for i in items if i not in s]
            await self._save_response_user_list(key, updated)
            return self._format_plain_list_update(f"已更新 {label}", updated)
        if action in {"del", "remove", "-"}:
            items = set(normalize_tokens(rest, lower=True))
            if not items:
                return f"用法: ^{label} del <username@host|userId>"
            updated = [i for i in current if i not in items]
            await self._save_response_user_list(key, updated)
            return self._format_plain_list_update(f"已更新 {label}", updated)
        if action in {"set", "="}:
            items = normalize_tokens(rest, lower=True)
            await self._save_response_user_list(key, items)
            return self._format_plain_list_update(f"已更新 {label}", items)
        return (
            f"用法: ^{label} [list|add|del|set|clear|reset]\n"
            f"示例: ^{label} add admin@example.com user-id-123\n"
            f"示例: ^{label} del user-id-123"
        )

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
                "用法: ^timeline [status|add|del|set|clear|reset]\n"
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

    @staticmethod
    def _build_antenna_index(
        antennas: Any,
    ) -> tuple[set[str], dict[str, list[str]], dict[str, str]]:
        if not isinstance(antennas, list):
            return set(), {}, {}
        antenna_ids: set[str] = set()
        name_to_ids: dict[str, list[str]] = {}
        id_to_name: dict[str, str] = {}
        for antenna in antennas:
            if not isinstance(antenna, dict):
                continue
            antenna_id = antenna.get("id")
            if not isinstance(antenna_id, str) or not antenna_id:
                continue
            antenna_ids.add(antenna_id)
            name = antenna.get("name")
            if isinstance(name, str) and (normalized := name.strip()):
                name_to_ids.setdefault(normalized, []).append(antenna_id)
                id_to_name[antenna_id] = normalized
        return antenna_ids, name_to_ids, id_to_name

    @staticmethod
    def _resolve_antenna_selector(
        selector: str, antenna_ids: set[str], name_to_ids: dict[str, list[str]]
    ) -> tuple[str, str | None, str | None]:
        if selector in antenna_ids:
            return selector, None, None
        if selector in name_to_ids:
            candidates = name_to_ids[selector]
        else:
            lowered = selector.lower()
            merged: list[str] = []
            for name, ids in name_to_ids.items():
                if name.lower() == lowered and ids:
                    merged.extend(ids)
            candidates = list(dict.fromkeys(merged))
        if not candidates:
            return "", None, "not_found"
        if len(candidates) != 1:
            return "", None, "ambiguous"
        return candidates[0], selector, None

    async def _format_antenna_status(self) -> str:
        bot = getattr(self, "bot", None)
        if not bot:
            return _MSG_BOT_NOT_INJECTED_ANTENNA
        selectors = normalize_tokens(
            bot.config.get(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS)
        )
        desired_text = ", ".join(selectors) if selectors else "(空)"
        antenna_id = self._get_connected_antenna_id(bot)
        current_text = await self._format_connected_antenna(antenna_id)
        return f"期望订阅: {desired_text}\n当前连接: {current_text}"

    @staticmethod
    def _get_connected_antenna_id(bot) -> str | None:
        connected = getattr(bot, "streaming", None)
        channels = getattr(connected, "channels", None) if connected else None
        if not channels:
            return None
        for info in channels.values():
            if info.get("name") == ChannelType.ANTENNA.value:
                antenna_id = (info.get("params") or {}).get("antennaId")
                return (
                    antenna_id if isinstance(antenna_id, str) and antenna_id else None
                )
        return None

    async def _format_connected_antenna(self, antenna_id: str | None) -> str:
        if not antenna_id:
            return "(空)"
        misskey = getattr(self, "misskey", None)
        if not misskey:
            return antenna_id
        antennas = await misskey.list_antennas()
        _, _, id_to_name = self._build_antenna_index(antennas)
        if name := id_to_name.get(antenna_id):
            return f"{name} ({antenna_id})"
        return antenna_id

    async def _list_antennas(self) -> str:
        if not getattr(self, "misskey", None):
            return "Misskey 客户端未注入，无法获取天线列表"
        antennas = await self.misskey.list_antennas()
        _, _, id_to_name = self._build_antenna_index(antennas)
        if not id_to_name:
            return "没有可用天线"
        lines = ["天线列表:"]
        for antenna_id, name in sorted(id_to_name.items(), key=lambda x: x[1]):
            lines.append(f"  {name} - {antenna_id}")
        return "\n".join(lines)

    async def _resolve_antenna_selectors(
        self, selectors: list[str], *, strict: bool
    ) -> tuple[list[str], str | None]:
        misskey = getattr(self, "misskey", None)
        if not misskey:
            deduped = [s.strip() for s in selectors if s.strip()]
            return list(dict.fromkeys(deduped)), None
        antennas = await misskey.list_antennas()
        antenna_ids, name_to_ids, _ = self._build_antenna_index(antennas)
        error_templates = {
            "not_found": "未知天线: {selector}\n使用 ^antenna list 查看可选天线",
            "ambiguous": "天线名称不唯一: {selector}\n请使用天线 ID",
        }
        resolved: list[str] = []
        for selector in (s.strip() for s in selectors):
            if not selector:
                continue
            antenna_id, _, err = self._resolve_antenna_selector(
                selector, antenna_ids, name_to_ids
            )
            if err:
                if strict and (template := error_templates.get(err)):
                    return [], template.format(selector=selector)
                continue
            if antenna_id:
                resolved.append(antenna_id)
        return list(dict.fromkeys(resolved)), None

    async def _apply_antenna_selectors(self, selectors: list[str], message: str) -> str:
        bot = getattr(self, "bot", None)
        if not bot:
            return _MSG_BOT_NOT_INJECTED_ANTENNA
        self._set_global_config_value(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS, selectors)
        await bot.restart_streaming()
        return message + "\n" + await self._format_antenna_status()

    async def _reset_antenna(self) -> str:
        selectors = list(self._baseline_antenna_selectors)
        resolved, _ = await self._resolve_antenna_selectors(selectors, strict=False)
        return await self._apply_antenna_selectors(resolved, "已重置天线订阅")

    async def _update_antenna_selectors(self, args: str, *, mode: str) -> str:
        bot = getattr(self, "bot", None)
        if not bot:
            return _MSG_BOT_NOT_INJECTED_ANTENNA
        if not getattr(self, "global_config", None):
            return _MSG_GLOBAL_CONFIG_NOT_INJECTED
        raw = args.strip()
        selectors = normalize_tokens(raw)
        if not selectors:
            return "请指定天线名称或 ID"
        current_selectors = normalize_tokens(
            self.global_config.get(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS)
        )
        current_ids, _ = await self._resolve_antenna_selectors(
            current_selectors, strict=False
        )
        target_ids, err = await self._resolve_antenna_selectors(selectors, strict=True)
        if err:
            return err
        if mode == "add":
            merged = list(dict.fromkeys([*current_ids, *target_ids]))
            return await self._apply_antenna_selectors(merged, "已添加天线订阅")
        if mode == "del":
            remaining = [i for i in current_ids if i not in set(target_ids)]
            return await self._apply_antenna_selectors(remaining, "已移除天线订阅")
        return await self._apply_antenna_selectors(target_ids, "已设置天线订阅")

    async def _set_antenna_selector(self, selector: str) -> str:
        bot = getattr(self, "bot", None)
        if not bot:
            return _MSG_BOT_NOT_INJECTED_ANTENNA
        selector = selector.strip()
        if not selector:
            return "请指定天线名称或 ID"
        if getattr(self, "misskey", None):
            antennas = await self.misskey.list_antennas()
            antenna_ids, name_to_ids, id_to_name = self._build_antenna_index(antennas)
            resolved_id, _, err = self._resolve_antenna_selector(
                selector, antenna_ids, name_to_ids
            )
            if err == "not_found":
                return f"未知天线: {selector}\n使用 ^antenna list 查看可选天线"
            if err == "ambiguous":
                return f"天线名称不唯一: {selector}\n请使用天线 ID"
            if resolved_id:
                selector = resolved_id
                display = id_to_name.get(resolved_id, "")
            else:
                display = ""
        else:
            display = ""
        self._set_global_config_value(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS, [selector])
        await bot.restart_streaming()
        if display:
            return (
                f"已切换天线: {display} ({selector})\n"
                + await self._format_antenna_status()
            )
        return f"已切换天线: {selector}\n" + await self._format_antenna_status()

    async def _clear_antenna(self) -> str:
        bot = getattr(self, "bot", None)
        if not bot:
            return _MSG_BOT_NOT_INJECTED_ANTENNA
        return await self._apply_antenna_selectors([], "已清空天线订阅")

    async def _handle_antenna(self, args: str) -> str:
        tokens = args.strip().split()
        if not tokens or tokens[0].lower() in {"status", "show"}:
            return await self._format_antenna_status()
        action = tokens[0].lower()
        if action in {"reset", "default"}:
            return await self._reset_antenna()
        if action in {"list", "ls"}:
            return await self._list_antennas()
        if action in {"clear", "off", "disable"}:
            return await self._clear_antenna()
        if action in {"add", "enable"}:
            rest = " ".join(tokens[1:]).strip()
            return await self._update_antenna_selectors(rest, mode="add")
        if action in {"del", "remove"}:
            rest = " ".join(tokens[1:]).strip()
            return await self._update_antenna_selectors(rest, mode="del")
        if action == "set":
            rest = " ".join(tokens[1:]).strip()
            return await self._update_antenna_selectors(rest, mode="set")
        if action in {"switch", "to"}:
            rest = " ".join(tokens[1:]).strip()
            return await self._set_antenna_selector(rest)
        if action in {"help", "usage"}:
            return (
                "用法: ^antenna [status|list|add|del|set|clear|reset]\n"
                "示例: ^antenna list\n"
                "示例: ^antenna add 天线A\n"
                "示例: ^antenna del 天线A\n"
                "示例: ^antenna set 天线A,天线B\n"
                "可选: 天线名或 ID（多个用空格/逗号分隔）"
            )
        return await self._set_antenna_selector(args)
