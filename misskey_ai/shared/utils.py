import os
import platform
import re
from typing import Any

import psutil
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

__all__ = (
    "retry_async",
    "get_system_info",
    "get_memory_usage",
    "normalize_tokens",
    "extract_user_id",
    "extract_username",
    "extract_user_handle",
    "redact_misskey_access_token",
    "health_check",
)

_MISSKEY_I_PARAM_RE = re.compile(r"([?&]i=)[^&#\s]+")
_MISSKEY_I_JSON_RE = re.compile(r'("i"\s*:\s*")[^"]+(")')


def redact_misskey_access_token(text: str) -> str:
    if not text:
        return text
    text = _MISSKEY_I_PARAM_RE.sub(r"\1***", text)
    return _MISSKEY_I_JSON_RE.sub(r"\1***\2", text)


def retry_async(max_retries=3, retryable_exceptions=None):
    kwargs = {
        "stop": stop_after_attempt(max_retries),
        "wait": wait_random_exponential(multiplier=1, max=30),
        "before_sleep": lambda retry_state: logger.info(
            f"Retry attempt #{retry_state.attempt_number}..."
        ),
    }
    if retryable_exceptions:
        kwargs["retry"] = retry_if_exception_type(retryable_exceptions)
    return retry(**kwargs)


def get_system_info() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_count": psutil.cpu_count(),
        "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
        "process_id": os.getpid(),
    }


def get_memory_usage() -> dict[str, Any]:
    process = psutil.Process()
    memory_info = process.memory_info()
    mb_factor = 1024 * 1024
    return {
        "rss_mb": round(memory_info.rss / mb_factor, 2),
        "vms_mb": round(memory_info.vms / mb_factor, 2),
        "percent": process.memory_percent(),
    }


def normalize_tokens(value: Any, *, lower: bool = False) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        tokens = [t.strip() for t in value.replace(",", " ").split() if t.strip()]
    elif isinstance(value, list):
        tokens = [str(v).strip() for v in value if v is not None and str(v).strip()]
    else:
        s = str(value).strip()
        tokens = [s] if s else []
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        k = t.lower() if lower else t
        if k in seen:
            continue
        seen.add(k)
        out.append(k if lower else t)
    return out


def extract_user_id(message: dict[str, Any]) -> str | None:
    user_info = message.get("fromUser") or message.get("user")
    if isinstance(user_info, dict):
        return user_info.get("id")
    return message.get("userId") or message.get("fromUserId")


def extract_username(message: dict[str, Any]) -> str:
    user_info = message.get("fromUser") or message.get("user", {})
    if isinstance(user_info, dict):
        return user_info.get("username", "unknown")
    return "unknown"


def extract_user_handle(message: dict[str, Any]) -> str | None:
    user_info = message.get("fromUser") or message.get("user")
    if not isinstance(user_info, dict):
        return None
    username = user_info.get("username")
    if not isinstance(username, str) or not (u := username.strip()):
        return None
    host = user_info.get("host")
    if isinstance(host, str) and (h := host.strip()):
        return f"{u}@{h}"
    return u


def health_check() -> bool:
    try:
        return psutil.Process().is_running()
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return False
