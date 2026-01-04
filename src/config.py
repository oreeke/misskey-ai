import os
from functools import reduce
from pathlib import Path
from typing import Any

import anyio
import yaml
from loguru import logger

from .constants import ConfigKeys
from .exceptions import ConfigurationError

__all__ = ("Config",)

_MISSING = object()


class Config:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or os.environ.get("CONFIG_PATH", "config.yaml")
        self.data: dict[str, Any] = {}

    async def load(self) -> None:
        config_path = Path(self.config_path)
        if not config_path.exists():
            raise ConfigurationError()
        try:
            async with await anyio.open_file(config_path, "r", encoding="utf-8") as f:
                content = await f.read()
            self.data = yaml.safe_load(content) or {}
            if not isinstance(self.data, dict):
                raise ConfigurationError("config file root node must be an object")
            logger.debug(f"Loaded config file: {config_path}")
            self._override_from_env()
            self._validate_config()
        except yaml.YAMLError as e:
            logger.error(f"YAML config parse error: {e}")
            raise ConfigurationError() from e
        except OSError as e:
            logger.error(f"Config file read error: {e}")
            raise ConfigurationError() from e
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Config processing error: {e}")
            raise ConfigurationError() from e

    def _override_from_env(self) -> None:
        env_mappings = {
            "MISSKEY_INSTANCE_URL": (ConfigKeys.MISSKEY_INSTANCE_URL, str),
            "MISSKEY_ACCESS_TOKEN": (ConfigKeys.MISSKEY_ACCESS_TOKEN, str),
            "OPENAI_API_KEY": (ConfigKeys.OPENAI_API_KEY, str),
            "OPENAI_MODEL": (ConfigKeys.OPENAI_MODEL, str),
            "OPENAI_API_BASE": (ConfigKeys.OPENAI_API_BASE, str),
            "OPENAI_API_MODE": (ConfigKeys.OPENAI_API_MODE, str),
            "OPENAI_MAX_TOKENS": (ConfigKeys.OPENAI_MAX_TOKENS, int),
            "OPENAI_TEMPERATURE": (ConfigKeys.OPENAI_TEMPERATURE, float),
            "BOT_SYSTEM_PROMPT": (ConfigKeys.BOT_SYSTEM_PROMPT, str),
            "BOT_AUTO_POST_ENABLED": (ConfigKeys.BOT_AUTO_POST_ENABLED, bool),
            "BOT_AUTO_POST_INTERVAL": (ConfigKeys.BOT_AUTO_POST_INTERVAL, int),
            "BOT_AUTO_POST_MAX_PER_DAY": (ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY, int),
            "BOT_AUTO_POST_VISIBILITY": (ConfigKeys.BOT_AUTO_POST_VISIBILITY, str),
            "BOT_AUTO_POST_LOCAL_ONLY": (ConfigKeys.BOT_AUTO_POST_LOCAL_ONLY, bool),
            "BOT_AUTO_POST_PROMPT": (ConfigKeys.BOT_AUTO_POST_PROMPT, str),
            "BOT_RESPONSE_MENTION_ENABLED": (
                ConfigKeys.BOT_RESPONSE_MENTION_ENABLED,
                bool,
            ),
            "BOT_RESPONSE_CHAT_ENABLED": (ConfigKeys.BOT_RESPONSE_CHAT_ENABLED, bool),
            "BOT_RESPONSE_CHAT_MEMORY": (ConfigKeys.BOT_RESPONSE_CHAT_MEMORY, int),
            "BOT_TIMELINE_ENABLED": (ConfigKeys.BOT_TIMELINE_ENABLED, bool),
            "BOT_TIMELINE_HOME": (ConfigKeys.BOT_TIMELINE_HOME, bool),
            "BOT_TIMELINE_LOCAL": (ConfigKeys.BOT_TIMELINE_LOCAL, bool),
            "BOT_TIMELINE_HYBRID": (ConfigKeys.BOT_TIMELINE_HYBRID, bool),
            "BOT_TIMELINE_GLOBAL": (ConfigKeys.BOT_TIMELINE_GLOBAL, bool),
            "DB_PATH": (ConfigKeys.DB_PATH, str),
            "LOG_PATH": (ConfigKeys.LOG_PATH, str),
            "LOG_LEVEL": (ConfigKeys.LOG_LEVEL, str),
            "LOG_DUMP_EVENTS": (ConfigKeys.LOG_DUMP_EVENTS, bool),
        }
        for env_key, (config_path, value_type) in env_mappings.items():
            if env_value := os.environ.get(env_key):
                self._set_config_value(config_path, env_value, value_type)

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
            project_root = Path(__file__).resolve().parents[1]
            path = Path(file_path)
            if not path.is_absolute():
                path = Path(self.config_path).parent / path
            try:
                resolved = path.resolve()
            except OSError:
                logger.debug(f"Failed to resolve config file path: {file_path}")
                return file_path
            if not resolved.is_relative_to(project_root):
                logger.debug(
                    f"Refusing to read config file outside project root: {file_path}"
                )
                return file_path
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                logger.debug(f"Loaded config value from file: {file_path}")
                return content
        except (OSError, UnicodeDecodeError) as e:
            logger.debug(
                f"Failed to load config from file {file_path}; using raw value: {e}"
            )
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
        except (KeyError, TypeError) as e:
            if default is not _MISSING:
                return default
            builtin_default = self._get_builtin_default(key)
            if builtin_default is not None:
                return builtin_default
            logger.error(f"Invalid config format: {e}")
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
        builtin_defaults = {
            ConfigKeys.MISSKEY_INSTANCE_URL: None,
            ConfigKeys.MISSKEY_ACCESS_TOKEN: None,
            ConfigKeys.OPENAI_API_KEY: None,
            ConfigKeys.OPENAI_MODEL: "deepseek-chat",
            ConfigKeys.OPENAI_API_BASE: "https://api.deepseek.com/v1",
            ConfigKeys.OPENAI_API_MODE: "auto",
            ConfigKeys.OPENAI_MAX_TOKENS: 1000,
            ConfigKeys.OPENAI_TEMPERATURE: 0.8,
            ConfigKeys.BOT_SYSTEM_PROMPT: None,
            ConfigKeys.BOT_AUTO_POST_ENABLED: True,
            ConfigKeys.BOT_AUTO_POST_INTERVAL: 180,
            ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY: 8,
            ConfigKeys.BOT_AUTO_POST_VISIBILITY: "public",
            ConfigKeys.BOT_AUTO_POST_LOCAL_ONLY: False,
            ConfigKeys.BOT_AUTO_POST_PROMPT: None,
            ConfigKeys.BOT_RESPONSE_MENTION_ENABLED: True,
            ConfigKeys.BOT_RESPONSE_CHAT_ENABLED: True,
            ConfigKeys.BOT_RESPONSE_CHAT_MEMORY: 10,
            ConfigKeys.BOT_TIMELINE_ENABLED: False,
            ConfigKeys.BOT_TIMELINE_HOME: False,
            ConfigKeys.BOT_TIMELINE_LOCAL: False,
            ConfigKeys.BOT_TIMELINE_HYBRID: False,
            ConfigKeys.BOT_TIMELINE_GLOBAL: False,
            ConfigKeys.DB_PATH: "data/misskey_ai.db",
            ConfigKeys.LOG_PATH: "logs/misskey_ai.log",
            ConfigKeys.LOG_LEVEL: "INFO",
            ConfigKeys.LOG_DUMP_EVENTS: False,
        }
        return builtin_defaults.get(key)

    def _validate_config(self) -> None:
        self._validate_required_configs()
        self._validate_types_and_ranges()
        self._validate_file_paths()
        logger.debug("Config validation completed")

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
        for key, types, desc in (
            (ConfigKeys.MISSKEY_INSTANCE_URL, (str,), "Misskey instance URL"),
            (ConfigKeys.MISSKEY_ACCESS_TOKEN, (str,), "Misskey access token"),
            (ConfigKeys.OPENAI_API_KEY, (str,), "OpenAI API key"),
            (ConfigKeys.OPENAI_MODEL, (str,), "OpenAI model"),
            (ConfigKeys.OPENAI_API_BASE, (str,), "OpenAI API base URL"),
            (ConfigKeys.BOT_AUTO_POST_ENABLED, (bool,), "auto-post enabled"),
            (ConfigKeys.BOT_AUTO_POST_LOCAL_ONLY, (bool,), "auto-post localOnly"),
            (
                ConfigKeys.BOT_RESPONSE_MENTION_ENABLED,
                (bool,),
                "mention response enabled",
            ),
            (ConfigKeys.BOT_RESPONSE_CHAT_ENABLED, (bool,), "chat response enabled"),
            (ConfigKeys.BOT_TIMELINE_ENABLED, (bool,), "timeline subscription enabled"),
            (
                ConfigKeys.BOT_TIMELINE_HOME,
                (bool,),
                "timeline home subscription enabled",
            ),
            (
                ConfigKeys.BOT_TIMELINE_LOCAL,
                (bool,),
                "timeline local subscription enabled",
            ),
            (
                ConfigKeys.BOT_TIMELINE_HYBRID,
                (bool,),
                "timeline hybrid subscription enabled",
            ),
            (
                ConfigKeys.BOT_TIMELINE_GLOBAL,
                (bool,),
                "timeline global subscription enabled",
            ),
            (ConfigKeys.DB_PATH, (str,), "database path"),
            (ConfigKeys.LOG_PATH, (str,), "log path"),
            (ConfigKeys.LOG_LEVEL, (str,), "log level"),
            (ConfigKeys.LOG_DUMP_EVENTS, (bool,), "event dump enabled"),
        ):
            self._require_type(key, types, desc)

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
                    logger.error(f"Failed to create directory for path {path}: {e}")
                    raise ConfigurationError(f"failed to create {desc}: {path}") from e
