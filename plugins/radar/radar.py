from __future__ import annotations

from typing import Any

from loguru import logger

from misskey_ai.plugin import PluginBase
from misskey_ai.clients.misskey.channels import ChannelType
from misskey_ai.shared.config_keys import ConfigKeys


class RadarPlugin(PluginBase):
    description = "雷达插件：与天线推送的帖子互动（反应、回复、转发、引用）"

    DEFAULT_REPLY_AI_PROMPT = (
        "根据帖子内容写一句自然回复，不要复述原文，不要加引号，不超过30字：\n{content}"
    )
    DEFAULT_QUOTE_AI_PROMPT = (
        "根据帖子内容写一句简短感想，不要复述原文，不要加引号，不超过30字：\n{content}"
    )

    def __init__(self, context):
        super().__init__(context)
        self.reaction = self._normalize_str(self.config.get("reaction"))
        self.reply_enabled = self._parse_bool(self.config.get("reply"), False)
        self.reply_text = self._normalize_str(self.config.get("reply_text"))
        self.reply_ai = self._parse_bool(self.config.get("reply_ai"), False)
        self.reply_ai_prompt = self._normalize_str(self.config.get("reply_ai_prompt"))
        self.reply_local_only = self._parse_bool(
            self.config.get("reply_local_only"), False
        )
        self.quote_enabled = self._parse_bool(self.config.get("quote"), False)
        self.quote_text = self._normalize_str(self.config.get("quote_text"))
        self.quote_ai = self._parse_bool(self.config.get("quote_ai"), False)
        self.quote_ai_prompt = self._normalize_str(self.config.get("quote_ai_prompt"))
        self.quote_visibility = self._normalize_visibility(
            self.config.get("quote_visibility")
        )
        self.quote_local_only = self._parse_bool(
            self.config.get("quote_local_only"), False
        )
        self.renote_enabled = self._parse_bool(self.config.get("renote"), False)
        self.renote_visibility = self._normalize_visibility(
            self.config.get("renote_visibility")
        )
        self.renote_local_only = self._parse_bool(
            self.config.get("renote_local_only"), False
        )
        self.skip_self = True

    async def initialize(self) -> bool:
        self._log_plugin_action("initialized", await self._format_antenna_sources())
        return True

    async def _format_antenna_sources(self) -> str:
        bot = self._get_bot_with_config()
        if not bot:
            return "Antenna: (unknown)"
        selectors = self._get_antenna_selectors(bot)
        if not selectors:
            return "Antenna: (empty)"
        id_to_name = await self._get_antenna_name_map()
        resolved_ids = await self._resolve_antenna_ids_safe(bot, selectors)
        return self._format_antenna_source_display(selectors, resolved_ids, id_to_name)

    def _get_bot_with_config(self):
        bot = getattr(self, "bot", None)
        if not bot or not getattr(bot, "config", None):
            return None
        return bot

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))

    def _get_antenna_selectors(self, bot) -> list[str]:
        selectors = self._get_selectors_from_bot(bot)
        if selectors:
            return selectors
        return self._get_selectors_from_config(bot)

    def _get_selectors_from_bot(self, bot) -> list[str]:
        if not hasattr(bot, "_load_antenna_selectors"):
            return []
        try:
            raw = bot._load_antenna_selectors() or []
        except Exception:
            return []
        selectors = [str(v).strip() for v in raw if v is not None and str(v).strip()]
        return self._dedupe(selectors)

    def _get_selectors_from_config(self, bot) -> list[str]:
        raw = bot.config.get(ConfigKeys.BOT_TIMELINE_ANTENNA_IDS)
        if raw is None or isinstance(raw, bool):
            return []
        if isinstance(raw, str):
            tokens = [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
            return self._dedupe(tokens)
        if isinstance(raw, list):
            tokens = [str(v).strip() for v in raw if v is not None and str(v).strip()]
            return self._dedupe(tokens)
        s = str(raw).strip()
        return [s] if s else []

    async def _get_antenna_name_map(self) -> dict[str, str]:
        misskey = getattr(self, "misskey", None)
        if not misskey:
            return {}
        antennas = await misskey.list_antennas()
        return self._build_antenna_id_name_map(antennas)

    @staticmethod
    def _build_antenna_id_name_map(antennas: Any) -> dict[str, str]:
        if not isinstance(antennas, list):
            return {}
        mapping: dict[str, str] = {}
        for antenna in antennas:
            if not isinstance(antenna, dict):
                continue
            antenna_id = antenna.get("id")
            name = antenna.get("name")
            if not isinstance(antenna_id, str) or not antenna_id:
                continue
            if not isinstance(name, str) or not name.strip():
                continue
            mapping[antenna_id] = name.strip()
        return mapping

    async def _resolve_antenna_ids_safe(self, bot, selectors: list[str]) -> list[str]:
        if not hasattr(bot, "_resolve_antenna_ids"):
            return []
        try:
            ids = await bot._resolve_antenna_ids(selectors)
        except Exception:
            return []
        ids = [v.strip() for v in ids if isinstance(v, str) and v.strip()]
        return self._dedupe(ids)

    def _format_antenna_source_display(
        self,
        selectors: list[str],
        resolved_ids: list[str],
        id_to_name: dict[str, str],
    ) -> str:
        if not resolved_ids:
            return f"Antenna: {', '.join(selectors)}"
        display = [
            id_to_name.get(antenna_id, antenna_id) for antenna_id in resolved_ids
        ]
        display = self._dedupe(display)
        return f"Antenna: {', '.join(display)}"

    @staticmethod
    def _normalize_str(value: Any) -> str | None:
        if value is None or isinstance(value, bool):
            return None
        if not isinstance(value, str):
            value = str(value)
        s = value.strip()
        return s or None

    @staticmethod
    def _parse_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, float):
            return bool(int(value))
        if isinstance(value, str):
            s = value.strip().lower()
            if s in {"true", "1", "yes", "y", "on"}:
                return True
            if s in {"false", "0", "no", "n", "off"}:
                return False
        return default

    def _normalize_visibility(self, value: Any) -> str | None:
        s = self._normalize_str(value)
        if not s:
            return None
        v = s.lower()
        if v in {"public", "home", "followers"}:
            return v
        return None

    @staticmethod
    def _extract_user_variants(note: dict[str, Any]) -> set[str]:
        user = note.get("user")
        if not isinstance(user, dict):
            return set()
        username = user.get("username")
        if not isinstance(username, str) or not username.strip():
            return set()
        base = username.strip()
        variants = {base.lower()}
        host = user.get("host")
        if isinstance(host, str) and host.strip():
            variants.add(f"{base}@{host.strip()}".lower())
        return variants

    def _effective_text(self, note: dict[str, Any]) -> str:
        parts: list[str] = []
        for k in ("cw", "text"):
            v = note.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        renote = note.get("renote")
        if isinstance(renote, dict):
            parts.append(self._effective_text(renote))
        return "\n".join(p for p in parts if p).strip()

    def _should_skip_self(self, note: dict[str, Any], variants: set[str]) -> bool:
        if not self.skip_self or not hasattr(self, "bot"):
            return False
        bot_id = getattr(self.bot, "bot_user_id", None)
        if bot_id and note.get("userId") == bot_id:
            return True
        bot_name = getattr(self.bot, "bot_username", None)
        return isinstance(bot_name, str) and bot_name and bot_name.lower() in variants

    @staticmethod
    def _format_reply_text(template: str, note: dict[str, Any]) -> str:
        if "{username}" not in template:
            return template
        user = note.get("user")
        if isinstance(user, dict) and isinstance(user.get("username"), str):
            username = user["username"].strip() or "unknown"
        else:
            username = "unknown"
        return template.replace("{username}", username)

    async def _generate_ai_reply(self, note: dict[str, Any]) -> str | None:
        if not hasattr(self, "openai") or not (content := self._effective_text(note)):
            return None
        prompt = (self.reply_ai_prompt or self.DEFAULT_REPLY_AI_PROMPT).format(
            content=content
        )
        system_prompt = (
            self.global_config.get(ConfigKeys.BOT_SYSTEM_PROMPT, "") or ""
        ).strip()
        reply = await self.openai.generate_text(
            prompt,
            system_prompt or None,
            max_tokens=self.global_config.get(ConfigKeys.OPENAI_MAX_TOKENS),
            temperature=self.global_config.get(ConfigKeys.OPENAI_TEMPERATURE),
        )
        return reply.strip() or None

    async def _generate_ai_quote(self, note: dict[str, Any]) -> str | None:
        if not hasattr(self, "openai") or not (content := self._effective_text(note)):
            return None
        prompt = (self.quote_ai_prompt or self.DEFAULT_QUOTE_AI_PROMPT).format(
            content=content
        )
        system_prompt = (
            self.global_config.get(ConfigKeys.BOT_SYSTEM_PROMPT, "") or ""
        ).strip()
        reply = await self.openai.generate_text(
            prompt,
            system_prompt or None,
            max_tokens=self.global_config.get(ConfigKeys.OPENAI_MAX_TOKENS),
            temperature=self.global_config.get(ConfigKeys.OPENAI_TEMPERATURE),
        )
        return reply.strip() or None

    async def on_timeline_note(
        self, note_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not hasattr(self, "misskey"):
            return None
        channel = note_data.get("streamingChannel")
        if not isinstance(channel, str) or channel != ChannelType.ANTENNA.value:
            return None
        note_id = note_data.get("id")
        if not isinstance(note_id, str) or not note_id:
            return None
        variants = self._extract_user_variants(note_data)
        if self._should_skip_self(note_data, variants):
            return None
        username = (
            str((note_data.get("user", {}) or {}).get("username", "unknown")).strip()
            or "unknown"
        )
        try:
            lock_ctx = None
            if hasattr(self, "bot"):
                lock_ctx = self.bot.lock_actor(note_data.get("userId"), username)
            if lock_ctx:
                async with lock_ctx:
                    await self._act(note_data, note_id, channel)
            else:
                await self._act(note_data, note_id, channel)
        except Exception as e:
            logger.error(f"Radar interaction failed: {repr(e)}")
        return None

    async def _maybe_react(
        self, note_data: dict[str, Any], note_id: str, channel: str
    ) -> None:
        if not self.reaction or note_data.get("myReaction"):
            return
        try:
            await self.misskey.create_reaction(note_id, self.reaction)
            self._log_plugin_action("reacted", f"{note_id} {self.reaction} [{channel}]")
        except Exception as e:
            logger.error(f"Radar reaction failed: {repr(e)}")

    async def _build_reply_text(self, note_data: dict[str, Any]) -> str | None:
        if self.reply_text:
            text = self._format_reply_text(self.reply_text, note_data).strip()
            if text:
                return text
        if not self.reply_ai:
            return None
        try:
            return await self._generate_ai_reply(note_data)
        except Exception as e:
            logger.error(f"Radar AI reply failed: {repr(e)}")
            return None

    async def _maybe_reply(
        self, note_data: dict[str, Any], note_id: str, channel: str
    ) -> None:
        if not self.reply_enabled:
            return
        if not (text := await self._build_reply_text(note_data)):
            return
        try:
            await self.misskey.create_note(
                text=text, reply_id=note_id, local_only=self.reply_local_only
            )
            self._log_plugin_action("replied", f"{note_id} [{channel}]")
        except Exception as e:
            logger.error(f"Radar reply failed: {repr(e)}")

    async def _build_quote_text(self, note_data: dict[str, Any]) -> str | None:
        if self.quote_text:
            text = self._format_reply_text(self.quote_text, note_data).strip()
            if text:
                return text
        if not self.quote_ai:
            return None
        try:
            return await self._generate_ai_quote(note_data)
        except Exception as e:
            logger.error(f"Radar AI quote failed: {repr(e)}")
            return None

    async def _maybe_quote(
        self, note_data: dict[str, Any], note_id: str, channel: str
    ) -> bool:
        if not self.quote_enabled:
            return False
        if not (text := await self._build_quote_text(note_data)):
            return False
        try:
            await self.misskey.create_renote(
                note_id,
                visibility=self.quote_visibility,
                text=text,
                local_only=self.quote_local_only,
            )
            self._log_plugin_action(
                "quoted", f"{note_id} {self.quote_visibility or ''} [{channel}]"
            )
            return True
        except Exception as e:
            logger.error(f"Radar quote failed: {repr(e)}")
            return False

    async def _maybe_renote(self, note_id: str, channel: str) -> None:
        if not self.renote_enabled:
            return
        try:
            await self.misskey.create_renote(
                note_id,
                visibility=self.renote_visibility,
                local_only=self.renote_local_only,
            )
            self._log_plugin_action(
                "renoted", f"{note_id} {self.renote_visibility or ''} [{channel}]"
            )
        except Exception as e:
            logger.error(f"Radar renote failed: {repr(e)}")

    async def _act(self, note_data: dict[str, Any], note_id: str, channel: str) -> None:
        await self._maybe_react(note_data, note_id, channel)
        await self._maybe_reply(note_data, note_id, channel)
        did_quote = await self._maybe_quote(note_data, note_id, channel)
        if not did_quote:
            await self._maybe_renote(note_id, channel)
