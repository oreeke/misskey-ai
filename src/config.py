import os
from functools import reduce
from pathlib import Path
from typing import Any

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
            with open(config_path, "r", encoding="utf-8") as f:
                self.data = yaml.safe_load(f) or {}
            if not isinstance(self.data, dict):
                raise ConfigurationError("配置文件根节点必须为对象")
            logger.debug(f"已加载配置文件: {config_path}")
            self._override_from_env()
            self._validate_config()
        except yaml.YAMLError as e:
            logger.error(f"YAML 配置文件解析错误: {e}")
            raise ConfigurationError() from e
        except OSError as e:
            logger.error(f"配置文件读取错误: {e}")
            raise ConfigurationError() from e
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"配置处理错误: {e}")
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
                logger.debug(f"无法解析配置文件路径: {file_path}")
                return file_path
            if not resolved.is_relative_to(project_root):
                logger.debug(f"禁止从项目目录外读取配置文件: {file_path}")
                return file_path
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                logger.debug(f"从文件加载配置: {file_path}")
                return content
        except (OSError, UnicodeDecodeError) as e:
            logger.debug(f"无法从文件加载配置 {file_path}，使用原始值: {e}")
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
            logger.error(f"配置文件格式错误: {e}")
            return None

    def get_required(self, key: str, desc: str | None = None) -> Any:
        value = self.get(key)
        if value is None:
            raise ConfigurationError(f"缺少必要配置项: {desc or key}")
        if isinstance(value, str) and not value.strip():
            raise ConfigurationError(f"缺少必要配置项: {desc or key}")
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
        }
        return builtin_defaults.get(key)

    def _validate_config(self) -> None:
        self._validate_required_configs()
        self._validate_types_and_ranges()
        self._validate_file_paths()
        logger.debug("配置验证完成")

    def _validate_required_configs(self) -> None:
        self.get_required(ConfigKeys.MISSKEY_INSTANCE_URL, "Misskey 实例 URL")
        self.get_required(ConfigKeys.MISSKEY_ACCESS_TOKEN, "Misskey 访问令牌")
        self.get_required(ConfigKeys.OPENAI_API_KEY, "OpenAI API 密钥")

    def _validate_types_and_ranges(self) -> None:
        def require_type(key: str, types: tuple[type, ...], desc: str) -> Any:
            value = self.get(key)
            if value is None:
                return None
            if not isinstance(value, types):
                raise ConfigurationError(f"配置项类型错误: {desc}")
            return value

        require_type(ConfigKeys.MISSKEY_INSTANCE_URL, (str,), "Misskey 实例 URL")
        require_type(ConfigKeys.MISSKEY_ACCESS_TOKEN, (str,), "Misskey 访问令牌")
        require_type(ConfigKeys.OPENAI_API_KEY, (str,), "OpenAI API 密钥")
        require_type(ConfigKeys.OPENAI_MODEL, (str,), "OpenAI 模型名称")
        require_type(ConfigKeys.OPENAI_API_BASE, (str,), "OpenAI API 端点")
        mode = require_type(ConfigKeys.OPENAI_API_MODE, (str,), "OpenAI API 模式")
        require_type(ConfigKeys.BOT_AUTO_POST_ENABLED, (bool,), "自动发帖开关")
        require_type(ConfigKeys.BOT_RESPONSE_MENTION_ENABLED, (bool,), "提及响应开关")
        require_type(ConfigKeys.BOT_RESPONSE_CHAT_ENABLED, (bool,), "聊天响应开关")
        require_type(ConfigKeys.BOT_TIMELINE_ENABLED, (bool,), "时间线订阅开关")
        require_type(ConfigKeys.BOT_TIMELINE_HOME, (bool,), "时间线订阅 Home 开关")
        require_type(ConfigKeys.BOT_TIMELINE_LOCAL, (bool,), "时间线订阅 Local 开关")
        require_type(ConfigKeys.BOT_TIMELINE_HYBRID, (bool,), "时间线订阅 Hybrid 开关")
        require_type(ConfigKeys.BOT_TIMELINE_GLOBAL, (bool,), "时间线订阅 Global 开关")
        require_type(ConfigKeys.DB_PATH, (str,), "数据库路径")
        require_type(ConfigKeys.LOG_PATH, (str,), "日志路径")
        require_type(ConfigKeys.LOG_LEVEL, (str,), "日志级别")

        if mode is not None and mode.strip().lower() not in {
            "auto",
            "chat",
            "responses",
        }:
            raise ConfigurationError("OpenAI API 模式必须是 auto/chat/responses")

        max_tokens = require_type(
            ConfigKeys.OPENAI_MAX_TOKENS, (int,), "最大生成 token 数"
        )
        if max_tokens is not None and max_tokens <= 0:
            raise ConfigurationError("最大生成 token 数必须大于 0")
        temperature = require_type(
            ConfigKeys.OPENAI_TEMPERATURE, (int, float), "温度参数"
        )
        if temperature is not None and not 0 <= float(temperature) <= 2:
            raise ConfigurationError("温度参数必须在 0~2 之间")
        interval = require_type(
            ConfigKeys.BOT_AUTO_POST_INTERVAL, (int,), "发帖间隔（分钟）"
        )
        if interval is not None and interval <= 0:
            raise ConfigurationError("发帖间隔必须大于 0")
        max_per_day = require_type(
            ConfigKeys.BOT_AUTO_POST_MAX_PER_DAY, (int,), "每日最大发帖数量"
        )
        if max_per_day is not None and max_per_day < 0:
            raise ConfigurationError("每日最大发帖数量不能小于 0")
        chat_memory = require_type(
            ConfigKeys.BOT_RESPONSE_CHAT_MEMORY, (int,), "聊天上下文记忆长度"
        )
        if chat_memory is not None and chat_memory < 0:
            raise ConfigurationError("聊天上下文记忆长度不能小于 0")
        visibility = require_type(
            ConfigKeys.BOT_AUTO_POST_VISIBILITY, (str,), "发帖可见性"
        )
        if visibility is not None and visibility not in {
            "public",
            "home",
            "followers",
        }:
            raise ConfigurationError("发帖可见性必须是 public/home/followers")

    def _validate_file_paths(self) -> None:
        paths = [
            (self.get(ConfigKeys.DB_PATH), "数据库目录"),
            (self.get(ConfigKeys.LOG_PATH), "日志目录"),
        ]
        for path, desc in paths:
            if path:
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    logger.error(f"创建{desc}失败 {path}: {e}")
                    raise ConfigurationError(f"无法创建{desc}: {path}") from e
