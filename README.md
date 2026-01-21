<div align="center">

<h1>Misskey AI</h1>

一只 Python 实现的 Misskey 机器人<br><br>
正运行在：[oreeke.com/@ai](https://oreeke.com/@ai)

<a href="https://www.python.org/downloads">
    <img alt="python 3.11+" src="https://img.shields.io/badge/python-3.11+-4775b1.svg?style=for-the-badge&labelColor=303030&logo=python&logoColor=4775b1"></a>
<a href="https://github.com/misskey-dev/misskey">
    <img alt="misskey 2025+" src="https://img.shields.io/badge/misskey-2025+-acea31.svg?style=for-the-badge&labelColor=303030&logo=misskey&logoColor=acea31"></a>
<a href="./LICENSE">
    <img alt="license" src="https://img.shields.io/badge/license-AGPL--3.0-603669.svg?style=for-the-badge&labelColor=303030&logo=gnu&logoColor=ffffff"></a>

</div>

## 简介

### 主要功能

- 📜 根据已设置的周期和可见性自动发帖，由 AI 生成内容
- 💬 实时响应用户提及（`@`）、私信、群聊
- 👁️ 接入多模态模型时，支持视觉理解（需启用 [Vision](./plugins/vision) 插件）
- 📡 订阅天线，对感兴趣的帖子自动反应、回复、转发、引用（需启用 [Radar](./plugins/radar) 插件）
- 🥏 自定义关键词和期望回复，绕过 AI（需启用 [KeyAct](./plugins/keyact) 插件）
- 🧠 自定义 AI 系统提示（性格）、发帖提示、回帖提示、引用提示

## 开始

### 克隆仓库

```bash
git clone https://github.com/oreeke/misskey-ai
cd misskey-ai
```

### 部署方式

#### `a` 手动安装

- 复制 `config.yaml.example` 为 `config.yaml` 并修改配置
<details>
<summary><kbd>📃 config.yaml</kbd></summary>

```yaml
misskey:
  instance_url: "https://misskey.example.com"       # Misskey 实例 URL（本地：http://localhost:port）
  access_token: "your_access_token_here"            # Misskey 访问令牌

openai:
  api_key: "your_api_key_here"                      # OpenAI API 密钥
  model: "deepseek-chat"                            # 使用的模型名称
  api_base: "https://api.deepseek.com/v1"           # OpenAI API 端点
  api_mode: "auto"                                  # auto/chat/responses
  max_tokens: 1000                                  # 最大生成 token 数
  temperature: 0.8                                  # 温度参数

bot:
  system_prompt: |                                  # 系统提示词（支持文件导入："prompts/*.txt"，"file://path/to/*.txt"）
    你是一个可爱的AI助手，运行在Misskey平台上。
    请用简短、友好的方式发帖和回答问题。

  timeline:
    enabled: false                                  # 是否订阅时间线
    home: false                                     # homeTimeline
    local: false                                    # localTimeline
    hybrid: false                                   # hybridTimeline
    global: false                                   # globalTimeline
    antenna_ids: []                                 # antenna ID 或名称（逗号/空格分隔）

  auto_post:
    enabled: true                                   # 是否启用自动发帖
    interval_minutes: 180                           # 发帖间隔（分钟）
    max_posts_per_day: 8                            # 每日最大发帖数量（凌晨 0 点重置计数器）
    visibility: "public"                            # 发帖可见性（public/home/followers）
    local_only: false                               # 是否禁用联合（仅本地可见）
    prompt: |                                       # 发帖提示词
      生成一篇有趣、有见解的社交媒体帖子。

  response:
    mention: true                                   # 是否响应提及（@）
    chat: true                                      # 是否响应聊天
    chat_memory: 10                                 # 聊天上下文记忆长度（条）
    rate_limit: -1                                  # 回复速率限制：同一用户回复最小间隔；-1 不限制；30s/5m/1h
    rate_limit_reply: "我需要休息一下..."            # 速率限制回复文案
    max_turns: -1                                   # 回复次数限制：同一用户最多对话轮数（机器人回复次数）；-1 不限制
    max_turns_reply: "我要回家了..."                 # 次数限制回复文案
    max_turns_release: -1                           # 次数限制解除时间：超限后多久解除；-1 不解除；30s/5m/1h
    whitelist:                                      # 白名单：username@host/userId，这些用户不受以上限制
      - "admin@example.com"
      - "user-id-123"
    blacklist:                                      # 黑名单：username@host/userId，这些用户禁止使用回复
      - "admin@example.com"
      - "user-id-123"

db:
  path: "data/misskey_ai.db"                        # SQLite 路径
  clear: 30                                         # SQLite 数据保留天数（不含插件）；-1 不清理

log:
  level: "INFO"                                     # 日志级别 (DEBUG/INFO/WARNING/ERROR)
  dump_events: false                                # 是否输出事件原始数据（DEBUG）
```
</details>

```bash
pip install -r requirements.txt
python run.py
```

> 后台运行（可选）
```bash
nohup python run.py & tail -f logs/misskey_ai.log
```

> 作为服务（可选）

<details>
<summary><kbd>📃 misskey-ai.service</kbd></summary>

```ini
[Unit]
Description=Misskey AI Service
After=network.target

[Service]
Type=exec
WorkingDirectory=/path/to/misskey-ai
ExecStart=/path/to/envs/misskey-ai/bin/python run.py
KillMode=control-group
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
```
</details>

```bash
systemctl daemon-reload
systemctl start misskey-ai.service
```

#### `b` Docker Compose

- 修改 `docker-compose.yaml` 中的环境变量
<details>
<summary><kbd>📃 docker-compose.yaml</kbd></summary>

```yaml
MISSKEY_INSTANCE_URL=https://misskey.example.com           # Misskey 实例 URL（本地：http://localhost:port）
MISSKEY_ACCESS_TOKEN=your_access_token_here                # Misskey 访问令牌
OPENAI_API_KEY=your_api_key_here                           # OpenAI API 密钥
OPENAI_MODEL=deepseek-chat                                 # 使用的模型名称
OPENAI_API_BASE=https://api.deepseek.com/v1                # OpenAI API 端点
OPENAI_API_MODE=auto                                       # auto/chat/responses
OPENAI_MAX_TOKENS=1000                                     # OpenAI 最大生成 token 数
OPENAI_TEMPERATURE=0.8                                     # OpenAI 温度参数
BOT_SYSTEM_PROMPT=你是一个可爱的AI助手...                    # 系统提示词（支持文件导入："prompts/*.txt"，"file://path/to/*.txt"）
BOT_AUTO_POST_ENABLED=true                                 # 是否启用自动发帖
BOT_AUTO_POST_INTERVAL=180                                 # 发帖间隔（分钟）
BOT_AUTO_POST_MAX_PER_DAY=8                                # 每日最大发帖数量（凌晨 0 点重置计数器）
BOT_AUTO_POST_VISIBILITY=public                            # 发帖可见性（public/home/followers）
BOT_AUTO_POST_LOCAL_ONLY=false                             # 是否禁用联合（仅本地可见）
BOT_AUTO_POST_PROMPT=生成一篇有趣、有见解的社交媒体帖子。      # 发帖提示词
BOT_RESPONSE_MENTION=true                                  # 是否响应提及（@）
BOT_RESPONSE_CHAT=true                                     # 是否响应聊天
BOT_RESPONSE_CHAT_MEMORY=10                                # 聊天上下文记忆长度（条）
BOT_RESPONSE_RATE_LIMIT=-1                                 # 回复速率限制：同一用户回复最小间隔；-1 不限制；30s/5m/1h
BOT_RESPONSE_RATE_LIMIT_REPLY=我需要休息一下...             # 速率限制回复文案
BOT_RESPONSE_MAX_TURNS=-1                                  # 回复次数限制：同一用户最多对话轮数（机器人回复次数）；-1 不限制
BOT_RESPONSE_MAX_TURNS_REPLY=我要回家了...                  # 次数限制回复文案
BOT_RESPONSE_MAX_TURNS_RELEASE=-1                          # 次数限制解除时间：超限后多久解除；-1 不解除；30s/5m/1h
BOT_RESPONSE_WHITELIST=                                    # 白名单：username@host/userId，这些用户不受以上限制
BOT_RESPONSE_BLACKLIST=                                    # 黑名单：username@host/userId，这些用户禁止使用回复
BOT_TIMELINE_ENABLED=false                                 # 是否订阅时间线
BOT_TIMELINE_HOME=false                                    # homeTimeline
BOT_TIMELINE_LOCAL=false                                   # localTimeline
BOT_TIMELINE_HYBRID=false                                  # hybridTimeline
BOT_TIMELINE_GLOBAL=false                                  # globalTimeline
BOT_TIMELINE_ANTENNA_IDS=                                  # antenna ID 或名称（逗号/空格分隔）
DB_PATH=data/misskey_ai.db                                 # SQLite 路径
DB_CLEAR=30                                                # SQLite 数据保留天数（不含插件）；-1 不清理
LOG_LEVEL=INFO                                             # 日志级别 (DEBUG/INFO/WARNING/ERROR)
LOG_DUMP_EVENTS=false                                      # 是否输出事件原始数据（DEBUG）
```
</details>

```bash
docker compose build
docker compose up -d
```

> [!TIP]
>
> - 自动发帖会尽量绕过 [Prompt caching](https://platform.openai.com/docs/guides/prompt-caching)，想让帖子更多样化请配置并启用 [Topics](./plugins/topics) 插件<br>
> - 切换模型仅需修改 `api_key` `model` `api_base`，相同 `api_base` 的模型可通过 [Cmd](./plugins/cmd) 实时切换
> - 机器人使用 [Radar](./plugins/radar) + `antenna` 时间线接收帖子，非必要无需订阅其他时间线（日志噪音大）

> [!NOTE]
>
> - 数据库自动清理（`db.clear`）或手动删除，会重置对用户的回复限制，以及 [Cmd](./plugins/cmd) 设置的黑白名单

## 生态

### 模型兼容

| 供应商 | OpenAI SDK | 多模态 |
| :---: | :---: | --- |
| [OpenAI](https://platform.openai.com/docs/overview) | ✅ | 📝 👁️ 🎨 |
| [DeepSeek](https://api-docs.deepseek.com/) | ✅ | 📝 |
| [xAI](https://docs.x.ai/docs/guides/migration) | ✅ | 📝 👁️ 🎨 |
| [Gemini](https://ai.google.dev/gemini-api/docs/openai) | ✅ | 📝 👁️ 🎨 |
| [Claude](https://docs.anthropic.com/en/api/openai-sdk) | ✅ | 📝 👁️ |
| [Ollama](https://ollama.com/blog/openai-compatibility) | ✅ | 📝 👁️ |

### 插件系统

| 插件 | 功能描述 |
| :---: | --- |
| [Cmd](./plugins/cmd) | 在聊天中使用 `^` 开头的命令管理机器人 |
| [KeyAct](./plugins/keyact) | 匹配自定义关键词直接回复，绕过 AI |
| [Radar](./plugins/radar) | 与天线推送的帖子互动（反应、回复、转发、引用） |
| [Topics](./plugins/topics) | 为自动发帖插入有序循环的主题关键词 |
| [Vision](./plugins/vision) | 识别提及（`@`）或聊天中的图片并回复 |
| [Weather](./plugins/weather) | 查询指定城市的天气信息 |
