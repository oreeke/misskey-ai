## 主题插件

### 功能描述

为自动发帖提供内容源<br>

### 使用方法

复制 `config.yaml.example` 为 `config.yaml` 并修改配置<br>
`source` 可选：
- `txt`
  - 像装填弹夹一样，每行一词，将主题关键词写入 `topics.txt`
  - 插件有序装载关键词，拼接成提示前缀，AI 以此为题生成内容
  - 如果关键词为纯链接 `http://...` / `https://...`，将直接发链接不经过 AI
- `rss`
  - `rss_list` 中添加 RSS 链接，机器人筛选最新动态作为帖子发布
  - RSS 拉取和发帖间隔由主配置 `auto_post.interval_minutes` 控制
  - `rss_ai` 让 AI 生成总结或感想，前提是 RSS 包含摘要或接入的模型能预览 URL
