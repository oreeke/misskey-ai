import asyncio
from typing import Any

import aiohttp
from loguru import logger

from .constants import API_TIMEOUT, WS_TIMEOUT
from .exceptions import ClientConnectorError

__all__ = ("TCPClient", "ClientSession")


class TCPClient:
    def __init__(self) -> None:
        self.__session: aiohttp.ClientSession | None = None
        self.__connector: aiohttp.TCPConnector | None = None
        self.user_agent = "MisskeyBot/1.0"
        self._default_headers = {
            "User-Agent": self.user_agent,
        }

    @property
    def _connector(self) -> aiohttp.TCPConnector:
        if self.__connector is None or self.__connector.closed:
            self.__connector = aiohttp.TCPConnector()
        return self.__connector

    @property
    def session(self) -> aiohttp.ClientSession:
        if self.__session is None or self.__session.closed:
            self.__session = aiohttp.ClientSession(
                headers=self._default_headers,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT, connect=10),
                connector=self._connector,
                connector_owner=True,
            )
        return self.__session

    async def close_session(self, *, silent: bool = False) -> None:
        if self.__session and not self.__session.closed:
            try:
                await self.__session.close()
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.warning(f"关闭会话时出错: {e}")
        if self.__connector and not self.__connector.closed:
            try:
                await self.__connector.close()
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.warning(f"关闭连接器时出错: {e}")
        self.__session = self.__connector = None
        if not silent:
            logger.debug("TCP 会话已关闭")

    async def ws_connect(self, url: str, *, compress: int = 0) -> Any:
        try:
            return await self.session.ws_connect(
                url,
                autoclose=False,
                max_msg_size=0,
                timeout=WS_TIMEOUT,
                headers={"User-Agent": self.user_agent},
                compress=compress,
            )
        except aiohttp.ClientConnectorError as e:
            logger.error(f"TCP 客户端连接失败: {e}")
            raise ClientConnectorError()


ClientSession: TCPClient = TCPClient()
