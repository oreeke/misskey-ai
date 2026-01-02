class ConfigKeys:
    MISSKEY_INSTANCE_URL = "misskey.instance_url"
    MISSKEY_ACCESS_TOKEN = "misskey.access_token"
    OPENAI_API_KEY = "openai.api_key"
    OPENAI_MODEL = "openai.model"
    OPENAI_API_BASE = "openai.api_base"
    OPENAI_MAX_TOKENS = "openai.max_tokens"
    OPENAI_TEMPERATURE = "openai.temperature"
    BOT_SYSTEM_PROMPT = "bot.system_prompt"
    BOT_AUTO_POST_ENABLED = "bot.auto_post.enabled"
    BOT_AUTO_POST_INTERVAL = "bot.auto_post.interval_minutes"
    BOT_AUTO_POST_MAX_PER_DAY = "bot.auto_post.max_posts_per_day"
    BOT_AUTO_POST_VISIBILITY = "bot.auto_post.visibility"
    BOT_AUTO_POST_PROMPT = "bot.auto_post.prompt"
    BOT_RESPONSE_MENTION_ENABLED = "bot.response.mention_enabled"
    BOT_RESPONSE_CHAT_ENABLED = "bot.response.chat_enabled"
    BOT_RESPONSE_CHAT_MEMORY = "bot.response.chat_memory"
    DB_PATH = "db.path"
    LOG_PATH = "log.path"
    LOG_LEVEL = "log.level"


HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_GATEWAY_TIMEOUT = 504

API_TIMEOUT = 60
API_MAX_RETRIES = 3
REQUEST_TIMEOUT = 120

WS_TIMEOUT = 30
WS_MAX_RETRIES = 10
RECEIVE_TIMEOUT = 30

STREAM_DEDUP_CACHE_MAX = 500
STREAM_DEDUP_CACHE_TTL = 3600

OPENAI_MAX_CONCURRENCY = 4
MISSKEY_MAX_CONCURRENCY = 20

STREAM_WORKERS = 8
STREAM_QUEUE_MAX = 1000
STREAM_QUEUE_PUT_TIMEOUT = 1.0

CHAT_CACHE_MAX_USERS = 5000
CHAT_CACHE_TTL = 3600
USER_LOCK_CACHE_MAX = 5000
USER_LOCK_TTL = 3600

ERROR_MESSAGES = {
    "MisskeyBotError": "抱歉，出现未知问题，请联系管理员。",
    "APIRateLimitError": "抱歉，请求过于频繁，请稍后再试。",
    "AuthenticationError": "抱歉，服务配置有误，请联系管理员。",
    "APIConnectionError": "抱歉，服务暂不可用，请稍后再试。",
    "APIBadRequestError": "抱歉，服务暂不可用，请稍后再试。",
    "ValueError": "抱歉，请求参数无效，请检查输入。",
    "RuntimeError": "抱歉，系统资源不足，请稍后再试。",
}
DEFAULT_ERROR_MESSAGE = "抱歉，处理您的消息时出现了错误。"
