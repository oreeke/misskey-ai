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
    pass


class ConfigurationError(MisskeyBotError):
    pass


class AuthenticationError(MisskeyBotError):
    pass


class APIConnectionError(MisskeyBotError):
    pass


class APIRateLimitError(MisskeyBotError):
    pass


class APIBadRequestError(MisskeyBotError):
    pass


class WebSocketConnectionError(MisskeyBotError):
    pass


class WebSocketReconnectError(WebSocketConnectionError):
    pass


class ClientConnectorError(MisskeyBotError):
    pass
