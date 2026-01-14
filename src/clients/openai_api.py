import asyncio
import json
from typing import Any
from urllib.parse import urlparse

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

from ..shared.constants import (
    API_MAX_RETRIES,
    API_TIMEOUT,
    OPENAI_MAX_CONCURRENCY,
    REQUEST_TIMEOUT,
)
from ..shared.exceptions import APIConnectionError, AuthenticationError
from ..shared.utils import retry_async

__all__ = ("OpenAIAPI",)


class OpenAIAPI:
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
        self._semaphore = asyncio.Semaphore(OPENAI_MAX_CONCURRENCY)
        try:
            self.client = openai.AsyncOpenAI(
                api_key=self.api_key, base_url=self.api_base, timeout=API_TIMEOUT
            )
            self._initialized = False
        except Exception as e:
            logger.error(f"Failed to create OpenAI API client: {e}")
            raise APIConnectionError() from e

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
            response = await self._make_api_request(
                messages, max_tokens, temperature, response_format=response_format
            )
            return self._process_api_response(response, call_type)
        except BadRequestError as e:
            logger.error(f"API request parameter error: {e}")
            raise ValueError() from e
        except OpenAIAuthenticationError as e:
            logger.error(f"API authentication failed: {e}")
            raise AuthenticationError() from e
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Invalid API response format: {e}")
            raise ValueError() from e

    def _should_use_responses(self) -> bool:
        if self.api_mode == "responses":
            return True
        if self.api_mode == "chat":
            return False
        base = (self.api_base or "").strip().lower()
        parsed = urlparse(base if "://" in base else f"https://{base}")
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if host in {"api.openai.com", "api.x.ai"}:
            return True
        if port == 11434:
            return True
        return False

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
            response = await self._make_responses_request(
                messages, max_tokens, temperature
            )
            text = self._extract_responses_text(response)
            logger.debug(
                f"OpenAI API {call_type} call succeeded; output length: {len(text)}"
            )
            return text
        except OpenAIAuthenticationError as e:
            logger.error(f"API authentication failed: {e}")
            raise AuthenticationError() from e
        except (NotFoundError, BadRequestError) as e:
            if self.api_mode != "auto":
                raise
            logger.warning(
                f"Responses API unavailable; falling back to Chat Completions: {e}"
            )
            return await self._call_api_common(
                messages, max_tokens, temperature, call_type
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Invalid API response format: {e}")
            raise ValueError() from e

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
            response = await self._make_responses_request(
                messages, max_tokens, temperature, text_format=text_format
            )
            text = self._extract_responses_text(response)
            logger.debug(
                f"OpenAI API {call_type} call succeeded; output length: {len(text)}"
            )
            return text
        except OpenAIAuthenticationError as e:
            logger.error(f"API authentication failed: {e}")
            raise AuthenticationError() from e
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Invalid API response format: {e}")
            raise ValueError() from e

    async def _make_responses_request(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        text_format: dict[str, Any] | None = None,
    ):
        async with self._semaphore:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "input": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                kwargs["max_output_tokens"] = max_tokens
            if text_format:
                kwargs["text"] = {"format": text_format}
            return await asyncio.wait_for(
                self.client.responses.create(**kwargs),
                timeout=REQUEST_TIMEOUT,
            )

    @staticmethod
    def _extract_responses_text(response) -> str:
        if isinstance((text := getattr(response, "output_text", None)), str) and text:
            return text
        parts = OpenAIAPI._collect_responses_output_text(
            getattr(response, "output", None)
        )
        if not parts:
            raise APIConnectionError()
        return "".join(parts)

    @staticmethod
    def _collect_responses_output_text(output: Any) -> list[str]:
        if not isinstance(output, list):
            raise APIConnectionError()
        return list(OpenAIAPI._iter_responses_output_text(output))

    @staticmethod
    def _iter_responses_output_text(output: list[Any]):
        for item in output:
            if getattr(item, "type", None) != "message":
                continue
            yield from OpenAIAPI._iter_responses_message_content(
                getattr(item, "content", None)
            )

    @staticmethod
    def _iter_responses_message_content(content: Any):
        if not isinstance(content, list):
            return
        for c in content:
            if (
                getattr(c, "type", None) == "output_text"
                and isinstance((t := getattr(c, "text", None)), str)
                and t
            ):
                yield t

    async def _make_api_request(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        response_format: dict[str, Any] | None = None,
    ):
        async with self._semaphore:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                api_base_host = urlparse(self.api_base).hostname
                if api_base_host and (
                    api_base_host == "openai.com"
                    or api_base_host.endswith(".openai.com")
                ):
                    kwargs["max_completion_tokens"] = max_tokens
                else:
                    kwargs["max_tokens"] = max_tokens
            if response_format:
                kwargs["response_format"] = response_format
            return await asyncio.wait_for(
                self.client.chat.completions.create(**kwargs),
                timeout=REQUEST_TIMEOUT,
            )

    @staticmethod
    def _process_api_response(response, call_type: str) -> str:
        generated_text = response.choices[0].message.content
        if not generated_text:
            raise APIConnectionError()
        logger.debug(
            f"OpenAI API {call_type} call succeeded; output length: {len(generated_text)}"
        )
        return generated_text

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

    @staticmethod
    def _coerce_json_substring(text: str) -> str | None:
        s = text.strip()
        if not s:
            return None
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            i = s.find(open_ch)
            j = s.rfind(close_ch)
            if 0 <= i < j:
                return s[i : j + 1]
        return None

    @staticmethod
    def _parse_json(text: str) -> Any:
        if not isinstance(text, str):
            raise ValueError()
        if (sub := OpenAIAPI._coerce_json_substring(text)) is None:
            raise ValueError()
        return json.loads(sub)

    @staticmethod
    def _validate_structured_output(
        obj: Any,
        *,
        expected_type: type | tuple[type, ...] | None,
        required_keys: tuple[str, ...] | None,
    ) -> Any:
        if expected_type is not None and not isinstance(obj, expected_type):
            raise ValueError()
        if required_keys:
            if not isinstance(obj, dict):
                raise ValueError()
            missing = [k for k in required_keys if k not in obj]
            if missing:
                raise ValueError()
        return obj

    @staticmethod
    def _build_structured_formats(
        schema: dict[str, Any] | None,
        *,
        name: str,
        strict: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if schema:
            rf = {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": strict},
            }
            tf = {
                "type": "json_schema",
                "name": name,
                "schema": schema,
                "strict": strict,
            }
            return rf, tf
        return {"type": "json_object"}, {"type": "json_object"}

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
        rf, tf = self._build_structured_formats(schema, name=name, strict=strict)
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
                obj = self._parse_json(last_text)
                return self._validate_structured_output(
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
