from __future__ import annotations

from typing import Any

from cachetools import TTLCache
from loguru import logger

from src.constants import ConfigKeys
from src.plugin import PluginBase


class RadarPlugin(PluginBase):
    description = "雷达插件：在订阅时间线中匹配感兴趣的帖子并自动互动"

    DEFAULT_REPLY_AI_PROMPT = (
        "根据帖子内容写一句自然回复，不要复述原文，不要加引号，不超过30字：\n{content}"
    )
    DEFAULT_QUOTE_AI_PROMPT = (
        "根据帖子内容写一句简短感想，不要复述原文，不要加引号，不超过30字：\n{content}"
    )

    def __init__(self, context):
        super().__init__(context)
        self.include_users = self._parse_str_set(self.config.get("include_users"))
        self.exclude_users = self._parse_str_set(self.config.get("exclude_users"))
        self.keyword_case_sensitive = self._parse_bool(
            self.config.get("keyword_case_sensitive"), False
        )
        self.include_groups = self._parse_keyword_groups(
            self.config.get("include_keywords")
        )
        self.exclude_groups = self._parse_keyword_groups(
            self.config.get("exclude_keywords")
        )
        self.has_any_filter = bool(
            self.include_users
            or self.exclude_users
            or self.include_groups
            or self.exclude_groups
        )
        self.allow_attachments = self._parse_bool(
            self.config.get("allow_attachments"), True
        )
        self.include_bot_users = self._parse_bool(
            self.config.get("include_bot_users", self.config.get("include_bot_user")),
            False,
        )
        self.reaction = self._normalize_str(self.config.get("reaction"))
        self.reply_enabled = self._parse_bool(self.config.get("reply_enabled"), False)
        self.reply_text = self._normalize_str(self.config.get("reply_text"))
        self.reply_ai = self._parse_bool(self.config.get("reply_ai"), False)
        self.reply_ai_prompt = self._normalize_str(self.config.get("reply_ai_prompt"))
        self.quote_enabled = self._parse_bool(self.config.get("quote_enabled"), False)
        self.quote_text = self._normalize_str(self.config.get("quote_text"))
        self.quote_ai = self._parse_bool(self.config.get("quote_ai"), False)
        self.quote_ai_prompt = self._normalize_str(self.config.get("quote_ai_prompt"))
        self.quote_visibility = self._normalize_visibility(
            self.config.get("quote_visibility")
        )
        self.renote_enabled = self._parse_bool(self.config.get("renote_enabled"), False)
        self.renote_visibility = self._normalize_visibility(
            self.config.get("renote_visibility")
        )
        self.skip_self = True
        self.dedupe_cache = TTLCache(
            maxsize=self._parse_int(self.config.get("dedupe_maxsize"), 2000),
            ttl=self._parse_int(self.config.get("dedupe_ttl_seconds"), 600),
        )

    async def initialize(self) -> bool:
        self._log_plugin_action("初始化完成")
        return True

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

    @staticmethod
    def _parse_int(value: Any, default: int) -> int:
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            s = value.strip()
            if s and s.lstrip("+-").isdigit():
                return int(s)
        return default

    @staticmethod
    def _parse_str_set(value: Any) -> set[str]:
        if value is None or isinstance(value, bool):
            return set()
        items: list[str] = []
        if isinstance(value, str):
            s = value.replace(",", " ").replace("\t", " ").strip()
            items.extend([x.strip() for x in s.split() if x.strip()])
            return {x.lower() for x in items if x}
        if isinstance(value, list):
            for v in value:
                if isinstance(v, str) and v.strip():
                    items.append(v.strip())
                elif v is not None and not isinstance(v, bool):
                    items.append(str(v).strip())
        return {x.lower() for x in items if x}

    def _parse_keyword_groups(self, value: Any) -> list[list[str]]:
        if value is None or isinstance(value, bool):
            return []
        if isinstance(value, list):
            text = "\n".join(str(v) for v in value if v is not None)
        else:
            text = str(value)
        groups: list[list[str]] = []
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            tokens = [t.strip() for t in raw.split() if t.strip()]
            if not self.keyword_case_sensitive:
                tokens = [t.lower() for t in tokens]
            if tokens:
                groups.append(tokens)
        return groups

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

    def _is_bot_user(self, note: dict[str, Any]) -> bool:
        user = note.get("user")
        if not isinstance(user, dict):
            return False
        value = user.get("isBot")
        if value is None:
            value = user.get("is_bot")
        if value is None:
            value = user.get("bot")
        return self._parse_bool(value, False)

    def _has_attachments(self, note: dict[str, Any]) -> bool:
        files = note.get("files")
        file_ids = note.get("fileIds")
        has_files = isinstance(files, list) and bool(files)
        has_file_ids = isinstance(file_ids, list) and bool(file_ids)
        if has_files or has_file_ids:
            return True
        renote = note.get("renote")
        if isinstance(renote, dict):
            return self._has_attachments(renote)
        return False

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

    def _match_groups(self, text: str, groups: list[list[str]]) -> bool:
        if not groups:
            return True
        t = text if self.keyword_case_sensitive else text.lower()
        return any(all(token in t for token in group) for group in groups)

    def _should_skip_self(self, note: dict[str, Any], variants: set[str]) -> bool:
        if not self.skip_self or not hasattr(self, "bot"):
            return False
        bot_id = getattr(self.bot, "bot_user_id", None)
        if bot_id and note.get("userId") == bot_id:
            return True
        bot_name = getattr(self.bot, "bot_username", None)
        return isinstance(bot_name, str) and bot_name and bot_name.lower() in variants

    def _should_skip_user(self, variants: set[str]) -> bool:
        if self.include_users and variants.isdisjoint(self.include_users):
            return True
        return bool(self.exclude_users) and not variants.isdisjoint(self.exclude_users)

    def _should_skip_keywords(self, text: str) -> bool:
        if not self._match_groups(text, self.include_groups):
            return True
        return bool(self.exclude_groups) and self._match_groups(
            text, self.exclude_groups
        )

    def _should_process(self, note: dict[str, Any]) -> bool:
        if not self.has_any_filter:
            return False
        variants = self._extract_user_variants(note)
        if self._should_skip_self(note, variants):
            return False
        if not self.include_bot_users and self._is_bot_user(note):
            return False
        if self._should_skip_user(variants):
            return False
        if not self.allow_attachments and self._has_attachments(note):
            return False
        text = self._effective_text(note)
        if self._should_skip_keywords(text):
            return False
        return True

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
        note_id = note_data.get("id")
        should_skip = (
            not isinstance(note_id, str)
            or not note_id
            or note_id in self.dedupe_cache
            or not self._should_process(note_data)
        )
        if should_skip:
            return None
        self.dedupe_cache[note_id] = True
        username = (
            str((note_data.get("user", {}) or {}).get("username", "unknown")).strip()
            or "unknown"
        )
        channel = note_data.get("streamingChannel", "unknown")
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
            logger.error(f"Radar 互动失败: {repr(e)}")
        return None

    async def _maybe_react(
        self, note_data: dict[str, Any], note_id: str, channel: str
    ) -> None:
        if not self.reaction or note_data.get("myReaction"):
            return
        try:
            await self.misskey.create_reaction(note_id, self.reaction)
            self._log_plugin_action("反应", f"{note_id} {self.reaction} [{channel}]")
        except Exception as e:
            logger.error(f"Radar 反应失败: {repr(e)}")

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
            logger.error(f"Radar AI 回复失败: {repr(e)}")
            return None

    async def _maybe_reply(
        self, note_data: dict[str, Any], note_id: str, channel: str
    ) -> None:
        if not self.reply_enabled:
            return
        if not (text := await self._build_reply_text(note_data)):
            return
        try:
            await self.misskey.create_note(text=text, reply_id=note_id)
            self._log_plugin_action("回复", f"{note_id} [{channel}]")
        except Exception as e:
            logger.error(f"Radar 回复失败: {repr(e)}")

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
            logger.error(f"Radar AI 引用失败: {repr(e)}")
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
                note_id, visibility=self.quote_visibility, text=text
            )
            self._log_plugin_action(
                "引用", f"{note_id} {self.quote_visibility or ''} [{channel}]"
            )
            return True
        except Exception as e:
            logger.error(f"Radar 引用失败: {repr(e)}")
            return False

    async def _maybe_renote(self, note_id: str, channel: str) -> None:
        if not self.renote_enabled:
            return
        try:
            await self.misskey.create_renote(note_id, visibility=self.renote_visibility)
            self._log_plugin_action(
                "转贴", f"{note_id} {self.renote_visibility or ''} [{channel}]"
            )
        except Exception as e:
            logger.error(f"Radar 转贴失败: {repr(e)}")

    async def _act(self, note_data: dict[str, Any], note_id: str, channel: str) -> None:
        await self._maybe_react(note_data, note_id, channel)
        await self._maybe_reply(note_data, note_id, channel)
        did_quote = await self._maybe_quote(note_data, note_id, channel)
        if not did_quote:
            await self._maybe_renote(note_id, channel)
