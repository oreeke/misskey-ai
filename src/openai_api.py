import asyncio
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

from .constants import (
    API_MAX_RETRIES,
    API_TIMEOUT,
    OPENAI_MAX_CONCURRENCY,
    REQUEST_TIMEOUT,
)
from .exceptions import APIConnectionError, AuthenticationError
from .utils import retry_async

__all__ = ("OpenAIAPI",)


class OpenAIAPI:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
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
            logger.error(f"创建 OpenAI API 客户端失败: {e}")
            raise APIConnectionError() from e

    async def initialize(self) -> None:
        if not self._initialized:
            logger.info(f"OpenAI API 客户端初始化完成: {self.api_base}")
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
    ) -> str:
        try:
            response = await self._make_api_request(messages, max_tokens, temperature)
            return self._process_api_response(response, call_type)
        except BadRequestError as e:
            logger.error(f"API 请求参数错误: {e}")
            raise ValueError() from e
        except OpenAIAuthenticationError as e:
            logger.error(f"API 认证失败: {e}")
            raise AuthenticationError() from e
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"API 响应数据格式错误: {e}")
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
            logger.debug(f"OpenAI API {call_type}调用成功，生成内容长度: {len(text)}")
            return text
        except OpenAIAuthenticationError as e:
            logger.error(f"API 认证失败: {e}")
            raise AuthenticationError() from e
        except (NotFoundError, BadRequestError) as e:
            if self.api_mode != "auto":
                raise
            logger.warning(f"Responses API 不可用，回退到 Chat Completions: {e}")
            return await self._call_api_common(
                messages, max_tokens, temperature, call_type
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"API 响应数据格式错误: {e}")
            raise ValueError() from e

    async def _make_responses_request(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
    ):
        async with self._semaphore:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "input": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                kwargs["max_output_tokens"] = max_tokens
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
        parts: list[str] = []
        for item in output:
            if getattr(item, "type", None) != "message":
                continue
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for c in content:
                if getattr(c, "type", None) == "output_text" and isinstance(
                    (t := getattr(c, "text", None)), str
                ):
                    if t:
                        parts.append(t)
        return parts

    async def _make_api_request(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
    ):
        async with self._semaphore:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                if "openai.com" in self.api_base:
                    kwargs["max_completion_tokens"] = max_tokens
                else:
                    kwargs["max_tokens"] = max_tokens
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
            f"OpenAI API {call_type}调用成功，生成内容长度: {len(generated_text)}"
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
            logger.debug("OpenAI API 客户端已关闭")

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
        return await self._call_api(messages, max_tokens, temperature, "单轮文本")

    async def generate_chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        return await self._call_api(messages, max_tokens, temperature, "多轮对话")
