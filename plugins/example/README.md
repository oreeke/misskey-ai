## 示例插件

### 功能描述

插件系统的规范写法与常见注意点<br>

### 使用方法

本目录默认不会被加载（用于示例参考）<br>
要实际启用，请复制整个 `plugins/example` 目录并重命名为你的插件名（例如 `plugins/hello`）<br>
将 `example.py` 重命名为与目录同名（例如 `hello.py`），并把类名改为 `HelloPlugin`<br>
复制 `config.yaml.example` 为 `config.yaml` 并修改配置，最后启用插件<br>

### 需要注意

- **目录/文件命名**：插件目录名必须与主文件名一致（`plugins/<name>/<name>.py`）<br>
- **类命名**：推荐 `<Name>Plugin`，与目录名首字母大写一致；否则需要确保目录内只存在一个插件类<br>
- **Hook 入参差异**：提及事件通常是 wrapper（包含 `note`），请按需取 `data["note"]` 读取 `text/files/fileIds`<br>
- **纯媒体消息**：聊天事件可能 `text: null`（只发图片/文件），插件应能处理或直接返回 `None`<br>
- **handled 语义（重要）**：`on_message/on_mention` 中返回 `handled: true` 会停止继续调用低优先级插件<br>
- **避免误接管**：不相关就返回 `None`，只在确实要“独占处理并回复”时返回 `handled: true`<br>
- **异常处理**：插件内部要把输入当作不可信（字段缺失/为 `None`），并避免抛异常影响消息链路<br>
- **可用上下文对象**：`misskey/drive/openai/persistence_manager/global_config/utils_provider/plugin_manager` 会注入到插件实例（存在性视运行环境而定）<br>
