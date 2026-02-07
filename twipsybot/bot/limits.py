import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cachetools import TTLCache

from ..db.sqlite import DBManager
from ..shared.config import Config
from ..shared.config_keys import ConfigKeys
from ..shared.constants import RESPONSE_LIMIT_CACHE_MAX, RESPONSE_LIMIT_CACHE_TTL
from ..shared.utils import normalize_tokens

_DURATION_PART_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]+)?", re.IGNORECASE)
_DURATION_UNITS: dict[str, int] = {
    "h": 3600,
    "m": 60,
    "s": 1,
}


@dataclass(slots=True)
class _ResponseLimitState:
    last_reply_ts: float | None = None
    turns: int = 0
    blocked_until_ts: float | None = None


class ResponseLimiter:
    def __init__(
        self,
        *,
        config: Config,
        db: DBManager,
        instance_url: str | None,
    ):
        self._config = config
        self._db = db
        self._instance_url = instance_url
        self._response_limits: TTLCache[str, _ResponseLimitState] = TTLCache(
            maxsize=RESPONSE_LIMIT_CACHE_MAX,
            ttl=RESPONSE_LIMIT_CACHE_TTL,
        )

    @staticmethod
    def _parse_user_list(value: Any) -> set[str]:
        return set(normalize_tokens(value, lower=True))

    def _load_response_whitelist_users(self) -> set[str]:
        return self._parse_user_list(
            self._config.get(ConfigKeys.BOT_RESPONSE_WHITELIST)
        )

    def _load_response_blacklist_users(self) -> set[str]:
        return self._parse_user_list(
            self._config.get(ConfigKeys.BOT_RESPONSE_BLACKLIST)
        )

    def _canonicalize_user_handle(self, username: str) -> str | None:
        if not isinstance(self._instance_url, str) or not self._instance_url:
            return None
        host = urlparse(self._instance_url).hostname
        return f"{username}@{host}" if host else None

    def _user_candidates(self, *, user_id: str, handle: str | None) -> set[str]:
        candidates = {user_id.lower()}
        if handle:
            normalized = handle.lower().lstrip("@").strip()
            if normalized:
                candidates.add(normalized)
                candidates.add(f"@{normalized}")
                if "@" not in normalized and (
                    canonical := self._canonicalize_user_handle(normalized)
                ):
                    candidates.add(canonical)
                    candidates.add(f"@{canonical}")
        return candidates

    def _is_response_whitelisted_user(
        self, *, user_id: str, handle: str | None
    ) -> bool:
        whitelist = self._load_response_whitelist_users()
        if not whitelist:
            return False
        return any(
            c in whitelist
            for c in self._user_candidates(user_id=user_id, handle=handle)
        )

    def is_response_blacklisted_user(self, *, user_id: str, handle: str | None) -> bool:
        blacklist = self._load_response_blacklist_users()
        if not blacklist:
            return False
        return any(
            c in blacklist
            for c in self._user_candidates(user_id=user_id, handle=handle)
        )

    @staticmethod
    def _parse_duration_seconds(value: Any) -> int | None:
        parsed = ResponseLimiter._parse_duration_number(value)
        if parsed is not None:
            return parsed
        if not isinstance(value, str):
            return None
        s = value.strip().lower()
        if not s:
            return None
        if s in {"-1", "unlimited", "none", "off"}:
            return -1
        return ResponseLimiter._parse_duration_string(s)

    @staticmethod
    def _parse_duration_number(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return None

    @staticmethod
    def _parse_duration_string(s: str) -> int | None:
        total = 0
        pos = 0
        while pos < len(s):
            m = _DURATION_PART_RE.match(s, pos)
            if not m:
                return None
            num = float(m.group(1))
            unit = (m.group(2) or "").lower()
            end = m.end()
            if not unit:
                return int(num) if end == len(s) and total == 0 else None
            mul = _DURATION_UNITS.get(unit)
            if not mul:
                return None
            total += int(num * mul)
            pos = end
        return total

    def _duration_config_seconds(self, key: str) -> int:
        seconds = self._parse_duration_seconds(self._config.get(key))
        if seconds is None:
            return -1
        return seconds

    async def _get_response_limit_state(self, user_id: str) -> _ResponseLimitState:
        if user_id in self._response_limits:
            return self._response_limits[user_id]
        last_reply_ts = None
        turns = 0
        blocked_until_ts = None
        row = await self._db.get_response_limit_state(user_id)
        if row:
            last_reply_ts, turns, blocked_until_ts = row
            if blocked_until_ts == -1:
                blocked_until_ts = float("inf")
        state = _ResponseLimitState(
            last_reply_ts=last_reply_ts,
            turns=int(turns or 0),
            blocked_until_ts=blocked_until_ts,
        )
        self._response_limits[user_id] = state
        return state

    async def _save_response_limit_state(
        self, user_id: str, state: _ResponseLimitState
    ) -> None:
        blocked_until_ts = state.blocked_until_ts
        if blocked_until_ts == float("inf"):
            blocked_until_ts = -1
        await self._db.set_response_limit_state(
            user_id=user_id,
            last_reply_ts=state.last_reply_ts,
            turns=state.turns,
            blocked_until_ts=blocked_until_ts,
        )

    async def get_response_block_reply(
        self, *, user_id: str, handle: str | None
    ) -> str | None:
        if self._is_response_whitelisted_user(user_id=user_id, handle=handle):
            return None
        now = time.time()
        state = await self._get_response_limit_state(user_id)
        if (
            state.blocked_until_ts is not None
            and state.blocked_until_ts != float("inf")
            and now >= state.blocked_until_ts
        ):
            state.turns = 0
            state.blocked_until_ts = None
            await self._save_response_limit_state(user_id, state)
        if state.blocked_until_ts is not None and now < state.blocked_until_ts:
            return self._config.get(ConfigKeys.BOT_RESPONSE_MAX_TURNS_REPLY)
        interval = self._duration_config_seconds(ConfigKeys.BOT_RESPONSE_RATE_LIMIT)
        if (
            interval > 0
            and state.last_reply_ts is not None
            and now - state.last_reply_ts < interval
        ):
            return self._config.get(ConfigKeys.BOT_RESPONSE_RATE_LIMIT_REPLY)
        max_turns = self._config.get(ConfigKeys.BOT_RESPONSE_MAX_TURNS)
        if isinstance(max_turns, int) and max_turns >= 0 and state.turns >= max_turns:
            release = self._duration_config_seconds(
                ConfigKeys.BOT_RESPONSE_MAX_TURNS_RELEASE
            )
            if release < 0:
                state.blocked_until_ts = float("inf")
            else:
                state.blocked_until_ts = now + release
            await self._save_response_limit_state(user_id, state)
            return self._config.get(ConfigKeys.BOT_RESPONSE_MAX_TURNS_REPLY)
        return None

    async def maybe_send_blocked_reply(
        self,
        *,
        user_id: str,
        handle: str | None,
        send_reply: Callable[[str], Awaitable[None]],
    ) -> bool:
        blocked = await self.get_response_block_reply(user_id=user_id, handle=handle)
        if not blocked:
            return False
        await send_reply(blocked)
        await self.record_response(user_id, count_turn=False)
        return True

    async def record_response(self, user_id: str, *, count_turn: bool) -> None:
        state = await self._get_response_limit_state(user_id)
        state.last_reply_ts = time.time()
        if count_turn:
            state.turns += 1
        await self._save_response_limit_state(user_id, state)
