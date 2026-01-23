import os
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import Any

import anyio
import yaml

from .config_keys import ConfigKeys
from .exceptions import ConfigurationError

__all__ = ("Config",)

_MISSING = object()


@dataclass(frozen=True, slots=True)
class _ConfigItem:
    key: str
    types: tuple[type, ...]
    desc: str
    default: Any = None
    env: str | None = None
    env_type: type = str


_CONFIG_ITEMS = (
    _ConfigItem(
        ConfigKeys.MISSKEY_INSTANCE_URL,
        (str,),
        "Misskey instance URL",
        env="MISSKEY_INSTANCE_URL",
    ),
    _ConfigItem(
        ConfigKeys.MISSKEY_ACCESS_TOKEN,
        (str,),
        "Misskey access token",
        env="MISSKEY_ACCESS_TOKEN",
    ),
    _ConfigItem(
        ConfigKeys.OPENAI_API_KEY,
        (str,),
        "OpenAI API key",
        env="OPENAI_API_KEY",
    ),
    _ConfigItem(
        ConfigKeys.OPENAI_MODEL,
        (str,),
        "OpenAI model",
        "deepseek-chat",
        env="OPENAI_MODEL",
    ),
    _ConfigItem(
        ConfigKeys.OPENAI_API_BASE,
        (str,),
        "OpenAI API base URL",
        "https://api.deepseek.com/v1",
        env="OPENAI_API_BASE",
    ),
    _ConfigItem(
        ConfigKeys.OPENAI_API_MODE,
        (str,),
        "OpenAI API mode",
        "auto",
        env="OPENAI_API_MODE",
    ),
    _ConfigItem(
        ConfigKeys.OPENAI_MAX_TOKENS,
        (int,),
        "max tokens",
        1000,
        env="OPENAI_MAX_TOKENS",
        env_type=int,
    ),
    _ConfigItem(
        ConfigKeys.OPENAI_TEMPERATURE,
        (int, float),
        "temperature",
        0.8,
        env="OPENAI_TEMPERATURE",
        env_type=float,
    ),
    _ConfigItem(
        ConfigKeys.BOT_SYSTEM_PROMPT, (str,), "system prompt", env="BOT_SYSTEM_PROMPT"
    ),
    _ConfigItem(
        ConfigKeys.BOT_AUTO_POST_ENABLED,
        (bool,),
        "auto-post enabled",
        True,
        env="BOT_AUTO_POST_ENABLED",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_AUTO_POST_INTERVAL,
        (int,),
        "auto-post interval (minutes)",
        180,
        env="BOT_AUTO_POST_INTERVAL",
        env_type=int,
    ),
    _ConfigItem(
        ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY,
        (int,),
        "max auto-posts per day",
        8,
        env="BOT_AUTO_POST_MAX_PER_DAY",
        env_type=int,
    ),
    _ConfigItem(
        ConfigKeys.BOT_AUTO_POST_VISIBILITY,
        (str,),
        "post visibility",
        "public",
        env="BOT_AUTO_POST_VISIBILITY",
    ),
    _ConfigItem(
        ConfigKeys.BOT_AUTO_POST_LOCAL_ONLY,
        (bool,),
        "auto-post localOnly",
        False,
        env="BOT_AUTO_POST_LOCAL_ONLY",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_AUTO_POST_PROMPT,
        (str,),
        "auto-post prompt",
        env="BOT_AUTO_POST_PROMPT",
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_MENTION,
        (bool,),
        "mention response enabled",
        True,
        env="BOT_RESPONSE_MENTION",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_CHAT,
        (bool,),
        "chat response enabled",
        True,
        env="BOT_RESPONSE_CHAT",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_CHAT_MEMORY,
        (int,),
        "chat context memory length",
        10,
        env="BOT_RESPONSE_CHAT_MEMORY",
        env_type=int,
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_RATE_LIMIT,
        (int, str),
        "response rate limit",
        -1,
        env="BOT_RESPONSE_RATE_LIMIT",
        env_type=str,
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_RATE_LIMIT_REPLY,
        (str,),
        "response rate limit reply",
        "我需要休息一下...",
        env="BOT_RESPONSE_RATE_LIMIT_REPLY",
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_MAX_TURNS,
        (int,),
        "response max turns",
        -1,
        env="BOT_RESPONSE_MAX_TURNS",
        env_type=int,
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_MAX_TURNS_REPLY,
        (str,),
        "response max turns reply",
        "我要回家了...",
        env="BOT_RESPONSE_MAX_TURNS_REPLY",
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_MAX_TURNS_RELEASE,
        (int, str),
        "response max turns release",
        -1,
        env="BOT_RESPONSE_MAX_TURNS_RELEASE",
        env_type=str,
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_WHITELIST,
        (list, str),
        "response whitelist",
        [],
        env="BOT_RESPONSE_WHITELIST",
    ),
    _ConfigItem(
        ConfigKeys.BOT_RESPONSE_BLACKLIST,
        (list, str),
        "response blacklist",
        [],
        env="BOT_RESPONSE_BLACKLIST",
    ),
    _ConfigItem(
        ConfigKeys.BOT_TIMELINE_ENABLED,
        (bool,),
        "timeline subscription enabled",
        False,
        env="BOT_TIMELINE_ENABLED",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_TIMELINE_HOME,
        (bool,),
        "timeline home subscription enabled",
        False,
        env="BOT_TIMELINE_HOME",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_TIMELINE_LOCAL,
        (bool,),
        "timeline local subscription enabled",
        False,
        env="BOT_TIMELINE_LOCAL",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_TIMELINE_HYBRID,
        (bool,),
        "timeline hybrid subscription enabled",
        False,
        env="BOT_TIMELINE_HYBRID",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_TIMELINE_GLOBAL,
        (bool,),
        "timeline global subscription enabled",
        False,
        env="BOT_TIMELINE_GLOBAL",
        env_type=bool,
    ),
    _ConfigItem(
        ConfigKeys.BOT_TIMELINE_ANTENNA_IDS,
        (list, str),
        "timeline antenna selectors",
        [],
        env="BOT_TIMELINE_ANTENNA_IDS",
    ),
    _ConfigItem(
        ConfigKeys.DB_PATH, (str,), "database path", "data/misskey_ai.db", env="DB_PATH"
    ),
    _ConfigItem(
        ConfigKeys.DB_CLEAR,
        (int, str),
        "database clear days",
        30,
        env="DB_CLEAR",
        env_type=int,
    ),
    _ConfigItem(
        ConfigKeys.LOG_PATH, (str,), "log path", "logs/misskey_ai.log", env="LOG_PATH"
    ),
    _ConfigItem(ConfigKeys.LOG_LEVEL, (str,), "log level", "INFO", env="LOG_LEVEL"),
    _ConfigItem(
        ConfigKeys.LOG_DUMP_EVENTS,
        (bool,),
        "event dump enabled",
        False,
        env="LOG_DUMP_EVENTS",
        env_type=bool,
    ),
)

_CONFIG_DEFAULTS = {item.key: item.default for item in _CONFIG_ITEMS}


class Config:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or os.environ.get("CONFIG_PATH", "config.yaml")
        self.data: dict[str, Any] = {}

    async def load(self) -> None:
        config_path = Path(self.config_path)
        loaded_from_file = False
        if config_path.exists():
            try:
                if not config_path.is_file():
                    raise ConfigurationError(
                        f"config path is not a file: {config_path}"
                    )
                async with await anyio.open_file(
                    config_path, "r", encoding="utf-8"
                ) as f:
                    content = await f.read()
                self.data = yaml.safe_load(content) or {}
                if not isinstance(self.data, dict):
                    raise ConfigurationError("config file root node must be an object")
                loaded_from_file = True
            except UnicodeDecodeError as e:
                raise ConfigurationError(f"Config file decode error: {e}") from e
            except yaml.YAMLError as e:
                raise ConfigurationError(f"YAML config parse error: {e}") from e
            except OSError as e:
                raise ConfigurationError(f"Config file read error: {e}") from e
        else:
            self.data = {}
        if not loaded_from_file or not self.data:
            self._override_from_env()
        self._validate_config()

    def _override_from_env(self) -> None:
        for item in _CONFIG_ITEMS:
            if item.env and (env_value := os.environ.get(item.env)):
                self._set_config_value(item.key, env_value, item.env_type)

    def _set_config_value(self, path: str, value: str, value_type: type) -> None:
        keys = path.split(".")
        config = self.data
        for key in keys[:-1]:
            config = config.setdefault(key, {})
        converters = {
            bool: lambda v: v.lower() in ("true", "yes"),
            int: int,
            float: float,
        }
        config[keys[-1]] = converters.get(
            value_type, lambda v: self._process_string_value(v, path)
        )(value)

    def _process_string_value(self, value: Any, config_path: str) -> Any:
        if not isinstance(value, str):
            return value
        if value.startswith("file://"):
            return self._load_from_file(value[7:])
        if self._is_prompt_config(config_path) and self._looks_like_file_path(value):
            return self._load_from_file(value)
        return value

    def _load_from_file(self, file_path: str) -> str:
        try:
            project_root = Path(__file__).resolve().parents[2]
            path = Path(file_path)
            if not path.is_absolute():
                path = Path(self.config_path).parent / path
            try:
                resolved = path.resolve()
            except OSError:
                return file_path
            if not resolved.is_relative_to(project_root):
                return file_path
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
                return content
        except (OSError, UnicodeDecodeError):
            return file_path

    @staticmethod
    def _looks_like_file_path(value: str) -> bool:
        return len(value) <= 200 and (value.endswith(".txt") or "prompts" in value)

    @staticmethod
    def _is_prompt_config(config_path: str) -> bool:
        prompt_configs = [ConfigKeys.BOT_SYSTEM_PROMPT, ConfigKeys.BOT_AUTO_POST_PROMPT]
        return config_path in prompt_configs

    def get(self, key: str, default: Any = _MISSING) -> Any:
        try:
            return reduce(lambda d, k: d[k], key.split("."), self.data)
        except (KeyError, TypeError):
            if default is not _MISSING:
                return default
            if key in _CONFIG_DEFAULTS:
                return _CONFIG_DEFAULTS[key]
            return None

    def get_required(self, key: str, desc: str | None = None) -> Any:
        value = self.get(key)
        if value is None:
            raise ConfigurationError(f"missing required config: {desc or key}")
        if isinstance(value, str) and not value.strip():
            raise ConfigurationError(f"missing required config: {desc or key}")
        return value

    @staticmethod
    def _get_builtin_default(key: str) -> Any:
        return _CONFIG_DEFAULTS.get(key)

    def _validate_config(self) -> None:
        self._validate_required_configs()
        self._validate_types_and_ranges()
        self._validate_file_paths()

    def _validate_required_configs(self) -> None:
        self.get_required(ConfigKeys.MISSKEY_INSTANCE_URL, "Misskey instance URL")
        self.get_required(ConfigKeys.MISSKEY_ACCESS_TOKEN, "Misskey access token")
        self.get_required(ConfigKeys.OPENAI_API_KEY, "OpenAI API key")

    def _require_type(self, key: str, types: tuple[type, ...], desc: str) -> Any:
        value = self.get(key)
        if value is None:
            return None
        if not isinstance(value, types):
            raise ConfigurationError(f"invalid config type: {desc}")
        return value

    @staticmethod
    def _validate_predicate(value: Any, predicate, message: str) -> None:
        if value is not None and not predicate(value):
            raise ConfigurationError(message)

    @staticmethod
    def _normalize_lower(value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    def _validate_types_and_ranges(self) -> None:
        for item in _CONFIG_ITEMS:
            self._require_type(item.key, item.types, item.desc)
        mode = self._normalize_lower(
            self._require_type(ConfigKeys.OPENAI_API_MODE, (str,), "OpenAI API mode")
        )
        self._validate_predicate(
            mode,
            lambda v: v in {"auto", "chat", "responses"},
            "OpenAI API mode must be auto/chat/responses",
        )
        max_tokens = self._require_type(
            ConfigKeys.OPENAI_MAX_TOKENS, (int,), "max tokens"
        )
        self._validate_predicate(max_tokens, lambda v: v > 0, "max tokens must be > 0")
        temperature = self._require_type(
            ConfigKeys.OPENAI_TEMPERATURE, (int, float), "temperature"
        )
        self._validate_predicate(
            temperature,
            lambda v: 0 <= float(v) <= 2,
            "temperature must be between 0 and 2",
        )
        interval = self._require_type(
            ConfigKeys.BOT_AUTO_POST_INTERVAL, (int,), "auto-post interval (minutes)"
        )
        self._validate_predicate(
            interval, lambda v: v > 0, "auto-post interval must be > 0"
        )
        max_per_day = self._require_type(
            ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY, (int,), "max auto-posts per day"
        )
        self._validate_predicate(
            max_per_day, lambda v: v >= 0, "max auto-posts per day must be >= 0"
        )
        chat_memory = self._require_type(
            ConfigKeys.BOT_RESPONSE_CHAT_MEMORY, (int,), "chat context memory length"
        )
        self._validate_predicate(
            chat_memory, lambda v: v >= 0, "chat context memory length must be >= 0"
        )
        max_turns = self._require_type(
            ConfigKeys.BOT_RESPONSE_MAX_TURNS, (int,), "response max turns"
        )
        self._validate_predicate(
            max_turns, lambda v: v >= -1, "response max turns must be >= -1"
        )
        for key, desc in (
            (ConfigKeys.BOT_RESPONSE_RATE_LIMIT, "response rate limit"),
            (ConfigKeys.BOT_RESPONSE_MAX_TURNS_RELEASE, "response max turns release"),
            (ConfigKeys.DB_CLEAR, "database clear days"),
        ):
            value = self.get(key)
            if value is None:
                continue
            if isinstance(value, int):
                self._validate_predicate(
                    value, lambda v: v >= -1, f"{desc} must be >= -1"
                )
            elif isinstance(value, str):
                self._validate_predicate(
                    bool(value.strip()), lambda v: v, f"{desc} must not be empty"
                )
        visibility = self._require_type(
            ConfigKeys.BOT_AUTO_POST_VISIBILITY, (str,), "post visibility"
        )
        self._validate_predicate(
            visibility,
            lambda v: v in {"public", "home", "followers"},
            "post visibility must be public/home/followers",
        )

    def _validate_file_paths(self) -> None:
        paths = [
            (self.get(ConfigKeys.DB_PATH), "database directory"),
            (self.get(ConfigKeys.LOG_PATH), "log directory"),
        ]
        for path, desc in paths:
            if path:
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise ConfigurationError(f"failed to create {desc}: {path}") from e
