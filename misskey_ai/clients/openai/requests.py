import asyncio
from typing import Any
from urllib.parse import urlparse

from ...shared.constants import REQUEST_TIMEOUT


def should_use_responses(*, api_mode: str, api_base: str) -> bool:
    if api_mode == "chat":
        return False
    _ = api_base
    return True


async def make_responses_request(
    *,
    client: Any,
    semaphore: asyncio.Semaphore,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None,
    temperature: float | None,
    text_format: dict[str, Any] | None = None,
):
    async with semaphore:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        if text_format:
            kwargs["text"] = {"format": text_format}
        return await asyncio.wait_for(
            client.responses.create(**kwargs),
            timeout=REQUEST_TIMEOUT,
        )


async def make_chat_completions_request(
    *,
    client: Any,
    semaphore: asyncio.Semaphore,
    model: str,
    api_base: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None,
    temperature: float | None,
    response_format: dict[str, Any] | None = None,
):
    async with semaphore:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            api_base_host = urlparse(api_base).hostname
            if api_base_host and (
                api_base_host == "openai.com" or api_base_host.endswith(".openai.com")
            ):
                kwargs["max_completion_tokens"] = max_tokens
            else:
                kwargs["max_tokens"] = max_tokens
        if response_format:
            kwargs["response_format"] = response_format
        return await asyncio.wait_for(
            client.chat.completions.create(**kwargs),
            timeout=REQUEST_TIMEOUT,
        )
