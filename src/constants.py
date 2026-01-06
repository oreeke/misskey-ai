class ConfigKeys:
    MISSKEY_INSTANCE_URL = "misskey.instance_url"
    MISSKEY_ACCESS_TOKEN = "misskey.access_token"
    OPENAI_API_KEY = "openai.api_key"
    OPENAI_MODEL = "openai.model"
    OPENAI_API_BASE = "openai.api_base"
    OPENAI_API_MODE = "openai.api_mode"
    OPENAI_MAX_TOKENS = "openai.max_tokens"
    OPENAI_TEMPERATURE = "openai.temperature"
    BOT_SYSTEM_PROMPT = "bot.system_prompt"
    BOT_AUTO_POST_ENABLED = "bot.auto_post.enabled"
    BOT_AUTO_POST_INTERVAL = "bot.auto_post.interval_minutes"
    BOT_AUTO_POST_MAX_PER_DAY = "bot.auto_post.max_posts_per_day"
    BOT_AUTO_POST_VISIBILITY = "bot.auto_post.visibility"
    BOT_AUTO_POST_LOCAL_ONLY = "bot.auto_post.local_only"
    BOT_AUTO_POST_PROMPT = "bot.auto_post.prompt"
    BOT_RESPONSE_MENTION_ENABLED = "bot.response.mention_enabled"
    BOT_RESPONSE_CHAT_ENABLED = "bot.response.chat_enabled"
    BOT_RESPONSE_CHAT_MEMORY = "bot.response.chat_memory"
    BOT_TIMELINE_ENABLED = "bot.timeline.enabled"
    BOT_TIMELINE_HOME = "bot.timeline.home"
    BOT_TIMELINE_LOCAL = "bot.timeline.local"
    BOT_TIMELINE_HYBRID = "bot.timeline.hybrid"
    BOT_TIMELINE_GLOBAL = "bot.timeline.global"
    BOT_TIMELINE_ANTENNA_IDS = "bot.timeline.antenna_ids"
    DB_PATH = "db.path"
    LOG_PATH = "log.path"
    LOG_LEVEL = "log.level"
    LOG_DUMP_EVENTS = "log.dump_events"


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

STREAM_DEDUP_CACHE_MAX = 2000
STREAM_DEDUP_CACHE_TTL = 600

OPENAI_MAX_CONCURRENCY = 4
MISSKEY_MAX_CONCURRENCY = 20

STREAM_WORKERS = 8
STREAM_QUEUE_MAX = 1000
STREAM_QUEUE_PUT_TIMEOUT = 1.0

CHAT_CACHE_MAX_USERS = 5000
CHAT_CACHE_TTL = 3600
USER_LOCK_CACHE_MAX = 5000
USER_LOCK_TTL = 3600
