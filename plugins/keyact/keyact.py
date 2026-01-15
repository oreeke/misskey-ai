import asyncio
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from misskey_ai.plugin import PluginBase


_MENTION_TOKEN_RE = re.compile(r"@[\w.@-]+\s*")


@dataclass(slots=True)
class _Rule:
    keywords: tuple[str, ...]
    response: str
    case_sensitive: bool
    enabled: bool


class KeyActPlugin(PluginBase):
    description = "KeyAct 插件：匹配自定义关键词直接回复，绕过 AI"

    def __init__(self, context):
        super().__init__(context)
        self.mention_enabled = bool(self.config.get("mention_enabled", True))
        self.chat_enabled = bool(self.config.get("chat_enabled", True))
        self.default_case_sensitive = bool(self.config.get("case_sensitive", False))
        self.rules: tuple[_Rule, ...] = ()

    async def initialize(self) -> bool:
        self.rules = self._load_rules()
        self._log_plugin_action(
            "initialized",
            f"rules={len(self.rules)}, mention={self.mention_enabled}, chat={self.chat_enabled}",
        )
        return True

    @staticmethod
    def _normalize_payload(data: dict[str, Any], *, kind: str) -> dict[str, Any]:
        if kind == "mention" and isinstance(data.get("note"), dict):
            return data["note"]
        return data

    def _get_text(self, data: dict[str, Any], *, kind: str) -> str:
        data = self._normalize_payload(data, kind=kind)
        text = data.get("text") or data.get("content") or data.get("body") or ""
        return text.strip() if isinstance(text, str) else ""

    def _clean_text(self, text: str, *, case_sensitive: bool) -> str:
        text = _MENTION_TOKEN_RE.sub("", text).strip()
        return text if case_sensitive else text.lower()

    @staticmethod
    def _as_keywords(v: Any) -> tuple[str, ...]:
        if isinstance(v, str):
            s = v.strip()
            return (s,) if s else ()
        if isinstance(v, list):
            out: list[str] = []
            for item in v:
                if isinstance(item, str) and (s := item.strip()):
                    out.append(s)
            return tuple(out)
        return ()

    def _parse_rule_item(self, item: dict[str, Any]) -> _Rule | None:
        if not bool(item.get("enabled", True)):
            return None
        response = item.get("response")
        if not isinstance(response, str) or not (response := response.strip()):
            return None
        keywords = self._as_keywords(item.get("keywords") or item.get("keyword"))
        if not keywords:
            return None
        case_sensitive = bool(item.get("case_sensitive", self.default_case_sensitive))
        return _Rule(
            keywords=keywords,
            response=response,
            case_sensitive=case_sensitive,
            enabled=True,
        )

    def _load_rules(self) -> tuple[_Rule, ...]:
        raw = self.config.get("rules")
        if not isinstance(raw, list) or not raw:
            return ()
        rules = (self._parse_rule_item(item) for item in raw if isinstance(item, dict))
        return tuple(r for r in rules if r is not None)

    def _match_rule(self, rule: _Rule, *, text_clean: str) -> bool:
        for k in rule.keywords:
            kk = k if rule.case_sensitive else k.lower()
            if text_clean == kk:
                return True
        return False

    def _create_response(self, response_text: str) -> dict[str, Any] | None:
        response = {
            "handled": True,
            "plugin_name": self.name,
            "response": response_text,
        }
        return response if self._validate_plugin_response(response) else None

    def _handle(self, data: dict[str, Any], *, kind: str) -> dict[str, Any] | None:
        if not self.rules:
            return None
        text_raw = self._get_text(data, kind=kind)
        if not text_raw:
            return None
        for rule in self.rules:
            text_clean = self._clean_text(
                text_raw,
                case_sensitive=rule.case_sensitive,
            )
            if not text_clean:
                continue
            if self._match_rule(rule, text_clean=text_clean):
                return self._create_response(rule.response)
        return None

    async def on_mention(self, mention_data: dict[str, Any]) -> dict[str, Any] | None:
        if not self.mention_enabled:
            return None
        try:
            await asyncio.sleep(0)
            return self._handle(mention_data, kind="mention")
        except Exception as e:
            logger.exception(f"KeyAct plugin exception while handling mention: {e}")
            return None

    async def on_message(self, message_data: dict[str, Any]) -> dict[str, Any] | None:
        if not self.chat_enabled:
            return None
        try:
            await asyncio.sleep(0)
            return self._handle(message_data, kind="chat")
        except Exception as e:
            logger.exception(f"KeyAct plugin exception while handling message: {e}")
            return None
