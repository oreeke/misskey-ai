import asyncio
from typing import Any

import aiohttp
from loguru import logger

from ...shared.constants import API_TIMEOUT
from ...shared.exceptions import ClientConnectorError

__all__ = ("TCPClient",)


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
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
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
                logger.warning(f"Error closing session: {e}")
        if self.__connector and not self.__connector.closed:
            try:
                await self.__connector.close()
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.warning(f"Error closing connector: {e}")
        self.__session = self.__connector = None
        if not silent:
            logger.debug("TCP session closed")

    async def ws_connect(self, url: str) -> Any:
        try:
            return await self.session.ws_connect(url)
        except aiohttp.ClientConnectorError as e:
            logger.error(f"TCP client connection failed: {e}")
            raise ClientConnectorError()
