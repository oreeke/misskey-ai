import asyncio
from typing import Any

import openai
from loguru import logger
from openai import (
    APIConnectionError as OpenAIConnectionError,
    APIError as OpenAIError,
    APITimeoutError,
    AuthenticationError as OpenAIAuthenticationError,
    BadRequestError,
    NotFoundError,
    RateLimitError,
    Timeout,
)

from ...shared.constants import (
    API_MAX_RETRIES,
    API_TIMEOUT,
    OPENAI_MAX_CONCURRENCY,
)
from ...shared.exceptions import APIConnectionError, AuthenticationError
from ...shared.utils import retry_async
from .extract import (
    build_structured_formats,
    extract_responses_text,
    parse_json,
    process_chat_completions_response,
    validate_structured_output,
)
from .requests import (
    make_chat_completions_request,
    make_responses_request,
    should_use_responses,
)

__all__ = ("OpenAIAPI",)


class OpenAIAPI:
    @staticmethod
    def _safe_error_message(e: Exception, *, limit: int = 300) -> str:
        name = type(e).__name__
        msg = str(e).strip()
        if not msg:
            return name
        msg = " ".join(msg.split())
        if len(msg) > limit:
            msg = f"{msg[: max(0, limit - 3)]}..."
        return f"{name}: {msg}"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5-mini",
        api_base: str = "https://api.openai.com/v1",
        api_mode: str | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.strip().strip("`")
        self.api_mode = (api_mode or "auto").strip().lower()
        self._responses_disabled = False
        self._semaphore = asyncio.Semaphore(OPENAI_MAX_CONCURRENCY)
        try:
            self.client = openai.AsyncOpenAI(
                api_key=self.api_key, base_url=self.api_base, timeout=API_TIMEOUT
            )
            self._initialized = False
        except Exception as e:
            logger.error(f"Failed to create OpenAI API client: {e}")
            raise APIConnectionError(self._safe_error_message(e)) from e

    def initialize(self) -> None:
        if not self._initialized:
            logger.info(f"OpenAI API client initialized: {self.api_base}")
            self._initialized = True

    @retry_async(
        max_retries=API_MAX_RETRIES,
        retryable_exceptions=(
            RateLimitError,
            APITimeoutError,
            Timeout,
            OpenAIError,
            OpenAIConnectionError,
            OSError,
        ),
    )
    async def _call_api_common(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        call_type: str,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        try:
            response = await make_chat_completions_request(
                client=self.client,
                semaphore=self._semaphore,
                model=self.model,
                api_base=self.api_base,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
            )
            return process_chat_completions_response(response, call_type)
        except BadRequestError as e:
            logger.error(f"API request parameter error: {e}")
            raise ValueError(self._safe_error_message(e)) from e
        except OpenAIAuthenticationError as e:
            logger.error(f"API authentication failed: {e}")
            raise AuthenticationError(self._safe_error_message(e)) from e
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Invalid API response format: {e}")
            raise ValueError(self._safe_error_message(e)) from e

    def _should_use_responses(self) -> bool:
        if self._responses_disabled:
            return False
        return should_use_responses(api_mode=self.api_mode, api_base=self.api_base)

    async def _call_api(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        call_type: str,
    ) -> str:
        if not self._should_use_responses():
            return await self._call_api_common(
                messages, max_tokens, temperature, call_type
            )
        try:
            response = await make_responses_request(
                client=self.client,
                semaphore=self._semaphore,
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            text = extract_responses_text(response)
            logger.debug(
                f"OpenAI API {call_type} call succeeded; output length: {len(text)}"
            )
            return text
        except OpenAIAuthenticationError as e:
            logger.error(f"API authentication failed: {e}")
            raise AuthenticationError(self._safe_error_message(e)) from e
        except (NotFoundError, BadRequestError) as e:
            self._responses_disabled = True
            logger.warning(
                f"Responses API unavailable; falling back to Chat Completions: {e}"
            )
            return await self._call_api_common(
                messages, max_tokens, temperature, call_type
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Invalid API response format: {e}")
            raise ValueError(self._safe_error_message(e)) from e

    async def _call_api_structured(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        call_type: str,
        *,
        response_format: dict[str, Any] | None,
        text_format: dict[str, Any] | None,
    ) -> str:
        if not self._should_use_responses():
            return await self._call_api_common(
                messages,
                max_tokens,
                temperature,
                call_type,
                response_format=response_format,
            )
        try:
            response = await make_responses_request(
                client=self.client,
                semaphore=self._semaphore,
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                text_format=text_format,
            )
            text = extract_responses_text(response)
            logger.debug(
                f"OpenAI API {call_type} call succeeded; output length: {len(text)}"
            )
            return text
        except OpenAIAuthenticationError as e:
            logger.error(f"API authentication failed: {e}")
            raise AuthenticationError(self._safe_error_message(e)) from e
        except (NotFoundError, BadRequestError) as e:
            self._responses_disabled = True
            logger.warning(
                f"Responses API unavailable; falling back to Chat Completions: {e}"
            )
            return await self._call_api_common(
                messages,
                max_tokens,
                temperature,
                call_type,
                response_format=response_format,
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Invalid API response format: {e}")
            raise ValueError(self._safe_error_message(e)) from e

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def close(self):
        if getattr(self, "client", None):
            await self.client.close()
            logger.debug("OpenAI API client closed")

    @staticmethod
    def _build_messages(
        prompt: str, system_prompt: str | None = None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": prompt.strip()})
        return messages

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        messages = self._build_messages(prompt, system_prompt)
        return await self._call_api(
            messages, max_tokens, temperature, "single-turn text"
        )

    async def generate_structured(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        schema: dict[str, Any] | None = None,
        name: str = "result",
        strict: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
        expected_type: type | tuple[type, ...] | None = (dict, list),
        required_keys: tuple[str, ...] | None = None,
        max_attempts: int = 2,
    ) -> Any:
        if max_attempts < 1:
            raise ValueError()
        rf, tf = build_structured_formats(schema, name=name, strict=strict)
        messages = self._build_messages(prompt, system_prompt)
        last_text = ""
        for attempt in range(max_attempts):
            if attempt:
                repair = (
                    "Output a single valid JSON only. No Markdown. No explanations.\n"
                    f"Previous output: {last_text}\n"
                    "Fix it into valid JSON:"
                )
                messages = [{"role": "user", "content": repair}]
            last_text = await self._call_api_structured(
                messages,
                max_tokens,
                temperature,
                "single-turn structured",
                response_format=rf,
                text_format=tf,
            )
            try:
                obj = parse_json(last_text)
                return validate_structured_output(
                    obj, expected_type=expected_type, required_keys=required_keys
                )
            except ValueError:
                continue
        raise ValueError()

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        schema: dict[str, Any] | None = None,
        name: str = "result",
        strict: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
        required_keys: tuple[str, ...] | None = None,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        obj = await self.generate_structured(
            prompt,
            system_prompt,
            schema=schema,
            name=name,
            strict=strict,
            max_tokens=max_tokens,
            temperature=temperature,
            expected_type=dict,
            required_keys=required_keys,
            max_attempts=max_attempts,
        )
        if not isinstance(obj, dict):
            raise ValueError()
        return obj

    async def generate_chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        return await self._call_api(
            messages, max_tokens, temperature, "multi-turn chat"
        )
