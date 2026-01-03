import os
import platform
from typing import Any

import psutil
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

__all__ = (
    "retry_async",
    "get_system_info",
    "get_memory_usage",
    "extract_user_id",
    "extract_username",
    "health_check",
)


def retry_async(max_retries=3, retryable_exceptions=None):
    kwargs = {
        "stop": stop_after_attempt(max_retries),
        "wait": wait_fixed(3),
        "before_sleep": lambda retry_state: logger.info(
            f"第 {retry_state.attempt_number} 次重试..."
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


def health_check() -> bool:
    try:
        return psutil.Process().is_running()
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return False
