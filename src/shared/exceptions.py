__all__ = (
    "MisskeyBotError",
    "ConfigurationError",
    "AuthenticationError",
    "APIConnectionError",
    "APIRateLimitError",
    "APIBadRequestError",
    "WebSocketConnectionError",
    "WebSocketReconnectError",
    "ClientConnectorError",
)


class MisskeyBotError(Exception):
    """Base error"""


class ConfigurationError(MisskeyBotError):
    """Configuration error"""


class AuthenticationError(MisskeyBotError):
    """Authentication error"""


class APIConnectionError(MisskeyBotError):
    """API connection error"""


class APIRateLimitError(MisskeyBotError):
    """API rate limit error"""


class APIBadRequestError(MisskeyBotError):
    """API bad request error"""


class WebSocketConnectionError(MisskeyBotError):
    """WebSocket connection error"""


class WebSocketReconnectError(WebSocketConnectionError):
    """WebSocket reconnect error"""


class ClientConnectorError(MisskeyBotError):
    """TCP client connector error"""
