## 命令插件

### 功能描述

在聊天中使用 `^` 开头的命令管理机器人<br>

### 使用方法

复制 `config.yaml.example` 为 `config.yaml` 并修改配置<br>
在与机器人的聊天页面中使用命令：
- `^help`：帮助信息
- `^status`：机器人状态
- `^sysinfo`：系统信息
- `^memory`：内存使用情况
- `^plugins`：插件列表
- `^enable <插件名>`：启用插件
- `^disable <插件名>`：禁用插件
- `^model`：查看当前模型
- `^model <模型名>`：切换模型
- `^model reset`：恢复默认模型
- `^timeline`：查看时间线订阅状态
- `^timeline add <home|local|hybrid|global>...`：添加订阅
- `^timeline del <home|local|hybrid|global>...`：移除订阅
- `^timeline set <home|local|hybrid|global>...`：覆盖订阅集合
- `^timeline clear`：清空订阅（仍保留 main）
- `^timeline reset`：按配置文件恢复订阅集合
- `^dbstats`：数据库统计
- `^dbclear <插件名> [键名]`：清理插件数据
