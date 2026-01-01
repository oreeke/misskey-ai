import asyncio
from typing import Any, Optional

import openai
from loguru import logger
from openai import (
    APIConnectionError as OpenAIConnectionError,
    APIError as OpenAIError,
    APITimeoutError,
    AuthenticationError as OpenAIAuthenticationError,
    BadRequestError,
    RateLimitError,
    Timeout,
)

from .constants import API_MAX_RETRIES, API_TIMEOUT, REQUEST_TIMEOUT
from .exceptions import APIConnectionError, AuthenticationError
from .utils import retry_async

__all__ = ("OpenAIAPI",)


class OpenAIAPI:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        api_base: str = "https://api.openai.com/v1",
    ):
        self.api_key = api_key
        self.model = model
        self.api_base = api_base
        try:
            self.client = openai.AsyncOpenAI(
                api_key=self.api_key, base_url=self.api_base, timeout=API_TIMEOUT
            )
            self._initialized = False
        except (ValueError, TypeError, OSError) as e:
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
        max_tokens: Optional[int],
        temperature: Optional[float],
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

    async def _make_api_request(
        self,
        messages: list[dict[str, Any]],
        max_tokens: Optional[int],
        temperature: Optional[float],
    ):
        return await asyncio.wait_for(
            self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
            timeout=REQUEST_TIMEOUT,
        )

    def _process_api_response(self, response, call_type: str) -> str:
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

    def _build_messages(
        self, prompt: str, system_prompt: Optional[str] = None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": prompt.strip()})
        return messages

    async def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        messages = self._build_messages(prompt, system_prompt)
        return await self._call_api_common(
            messages, max_tokens, temperature, "单轮文本"
        )

    async def generate_chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        return await self._call_api_common(
            messages, max_tokens, temperature, "多轮对话"
        )
