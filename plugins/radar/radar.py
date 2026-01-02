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

    def _normalize_str(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if not isinstance(value, str):
            value = str(value)
        s = value.strip()
        return s or None

    def _parse_bool(self, value: Any, default: bool) -> bool:
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

    def _parse_int(self, value: Any, default: int) -> int:
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

    def _parse_str_set(self, value: Any) -> set[str]:
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

    def _extract_user_variants(self, note: dict[str, Any]) -> set[str]:
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
        if isinstance(note.get("files"), list) and note["files"]:
            return True
        if isinstance(note.get("fileIds"), list) and note["fileIds"]:
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

    def _should_process(self, note: dict[str, Any]) -> bool:
        if not self.has_any_filter:
            return False
        if self.skip_self and hasattr(self, "bot"):
            bot_id = getattr(self.bot, "bot_user_id", None)
            if bot_id and note.get("userId") == bot_id:
                return False
            bot_name = getattr(self.bot, "bot_username", None)
            if isinstance(bot_name, str) and bot_name:
                if bot_name.lower() in self._extract_user_variants(note):
                    return False
        if not self.include_bot_users and self._is_bot_user(note):
            return False
        variants = self._extract_user_variants(note)
        if self.include_users and not (variants & self.include_users):
            return False
        if self.exclude_users and (variants & self.exclude_users):
            return False
        if not self.allow_attachments and self._has_attachments(note):
            return False
        text = self._effective_text(note)
        if not self._match_groups(text, self.include_groups):
            return False
        if self.exclude_groups and self._match_groups(text, self.exclude_groups):
            return False
        return True

    def _format_reply_text(self, template: str, note: dict[str, Any]) -> str:
        if "{username}" not in template:
            return template
        user = note.get("user")
        if isinstance(user, dict) and isinstance(user.get("username"), str):
            username = user["username"].strip() or "unknown"
        else:
            username = "unknown"
        return template.replace("{username}", username)

    async def _generate_ai_reply(self, note: dict[str, Any]) -> str | None:
        if not hasattr(self, "openai"):
            return None
        content = self._effective_text(note)
        if not content:
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
        if not hasattr(self, "openai"):
            return None
        content = self._effective_text(note)
        if not content:
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
        if not isinstance(note_id, str) or not note_id:
            return None
        if note_id in self.dedupe_cache:
            return None
        if not self._should_process(note_data):
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

    async def _act(self, note_data: dict[str, Any], note_id: str, channel: str) -> None:
        if self.reaction and not note_data.get("myReaction"):
            try:
                await self.misskey.create_reaction(note_id, self.reaction)
                self._log_plugin_action(
                    "反应", f"{note_id} {self.reaction} [{channel}]"
                )
            except Exception as e:
                logger.error(f"Radar 反应失败: {repr(e)}")
        if self.reply_enabled:
            text = None
            if self.reply_text:
                text = self._format_reply_text(self.reply_text, note_data).strip()
            if not text and self.reply_ai:
                try:
                    text = await self._generate_ai_reply(note_data)
                except Exception as e:
                    logger.error(f"Radar AI 回复失败: {repr(e)}")
            if text:
                try:
                    await self.misskey.create_note(text=text, reply_id=note_id)
                    self._log_plugin_action("回复", f"{note_id} [{channel}]")
                except Exception as e:
                    logger.error(f"Radar 回复失败: {repr(e)}")
        did_quote = False
        if self.quote_enabled:
            text = None
            if self.quote_text:
                text = self._format_reply_text(self.quote_text, note_data).strip()
            if not text and self.quote_ai:
                try:
                    text = await self._generate_ai_quote(note_data)
                except Exception as e:
                    logger.error(f"Radar AI 引用失败: {repr(e)}")
            if text:
                try:
                    await self.misskey.create_renote(
                        note_id, visibility=self.quote_visibility, text=text
                    )
                    self._log_plugin_action(
                        "引用", f"{note_id} {self.quote_visibility or ''} [{channel}]"
                    )
                    did_quote = True
                except Exception as e:
                    logger.error(f"Radar 引用失败: {repr(e)}")
        if self.renote_enabled and not did_quote:
            try:
                await self.misskey.create_renote(
                    note_id, visibility=self.renote_visibility
                )
                self._log_plugin_action(
                    "转贴", f"{note_id} {self.renote_visibility or ''} [{channel}]"
                )
            except Exception as e:
                logger.error(f"Radar 转贴失败: {repr(e)}")
