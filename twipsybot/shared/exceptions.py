__all__ = (
    "APIBadRequestError",
    "APIConnectionError",
    "APIRateLimitError",
    "AuthenticationError",
    "ClientConnectorError",
    "ConfigurationError",
    "MisskeyBotError",
    "WebSocketConnectionError",
    "WebSocketReconnectError",
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
