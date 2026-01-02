## 示例插件

### 功能描述

插件系统的规范写法与常见注意点<br>

### 使用方法

本目录默认不会被加载（用于示例参考）<br>
要实际启用，请复制整个 `plugins/example` 目录并重命名为你的插件名（例如 `plugins/hello`）<br>
将 `example.py` 重命名为与目录同名（例如 `hello.py`），并把类名改为 `HelloPlugin`<br>
复制 `config.yaml.example` 为 `config.yaml` 并修改配置，最后启用插件
