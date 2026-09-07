# 汽车网络 Trace MCP

这是一个面向汽车网络日志调查的 MCP 服务。当前版本可加载 Vector BLF 日志及可选的 DBC/ARXML 网络数据库，通过稳定的 `trace_id` 重复查询“测试日志中实际发生了什么”。

当前版本专注 Trace Core + Trace MCP，不负责自动给出完整故障根因，也未实现 Debug Agent、Config Agent、CANoe Agent、历史案例库或机器学习能力。

## 当前可用能力

- 加载 BLF，并在服务进程内复用已解析的 Trace Session。
- 查看日志时间范围、帧数、Channel、CAN ID、经典 CAN/CAN FD 等摘要。
- 按 CAN ID、Channel 和可选时间范围查询原始帧。
- 统计指定报文的周期、最大间隔和抖动。
- 使用 DBC 或 cantools 可直接读取的 ARXML 解码指定信号。
- 自动限制帧与信号样本输出，避免大结果进入 MCP Client 上下文。

## 安装

要求 Python 3.11 或更高版本。建议使用已有 Conda 环境，依赖只需安装一次：

```powershell
conda activate <你的环境名>
python -m pip install -e ".[dev]"
```

本项目不会在 Trace MCP 每次启动时展开一份独立依赖，也不会为日志自动生成大型磁盘数据库。Trace Session 使用进程内紧凑数组保存必要帧数据。

## 准备输入

建议把本地输入按以下方式放置：

```text
input/
├── blf/
│   └── test.blf
└── dbc/
    └── vehicle.dbc
```

`.blf`、`.asc`、`.mf4` 等测量文件已被 Git 忽略，避免误提交真实项目日志。调用 Tool 时请传入文件的绝对路径。

## 启动 Trace MCP

在仓库根目录运行：

```powershell
python -m anda.mcp.trace.server
```

默认使用 stdio transport，适合由 MCP Client 启动和连接。

一个通用的 MCP Client 配置示例：

```json
{
  "mcpServers": {
    "automotive-trace": {
      "command": "D:\\path\\to\\conda-env\\python.exe",
      "args": ["-m", "anda.mcp.trace.server"],
      "cwd": "D:\\path\\to\\automotive-network-debug-agent"
    }
  }
}
```

请把示例路径替换为本机实际 Conda 环境和仓库路径。

## MCP Tool


| Tool                 | 用途                                           |
| ---------------------- | ------------------------------------------------ |
| `load_trace`         | 加载 BLF 及可选数据库，返回`trace_id`          |
| `get_trace_summary`  | 获取时间范围、帧数、Channel、CAN/CAN FD 摘要   |
| `find_messages`      | 按 CAN ID、Channel、时间范围查询有限数量原始帧 |
| `get_message_timing` | 获取帧数、周期、最大间隔与抖动统计             |
| `decode_signal`      | 解码指定 CAN ID 中的指定信号并返回有限样本     |

### 调用示例

先加载日志：

```json
{
  "blf_path": "D:\\logs\\drive.blf",
  "database_path": "D:\\logs\\vehicle.dbc"
}
```

返回结果中的 `trace_id` 用于后续调用。同一路径、大小和修改时间均未变化时，再次调用会返回相同 `trace_id`，并把 `reused` 标记为 `true`。

按 CAN ID `0x100`（JSON 中写成十进制 `256`）查询 CAN1：

```json
{
  "trace_id": "<load_trace 返回的 trace_id>",
  "arbitration_id": 256,
  "channel": 1,
  "limit": 20
}
```

解码信号：

```json
{
  "trace_id": "<load_trace 返回的 trace_id>",
  "arbitration_id": 256,
  "signal_name": "EngineSpeed",
  "channel": 1,
  "limit": 50
}
```

`find_messages` 和 `decode_signal` 的单次返回上限均为 200 条；即使传入更大的 `limit`，服务也会按上限裁剪，并在结果中返回 `limit_applied`。

## Channel 与时间戳约定

- 对外 Channel 统一为 Vector/CANoe 风格的 1-based 编号：`1` 表示 CAN1，`2` 表示 CAN2。
- `python-can` 读取 BLF 时返回 0-based Channel，Trace Core 会明确还原为 1-based；调用者不需要自行加减 1。
- `timestamp`、`start_timestamp` 和 `end_timestamp` 均为 BLF 中的绝对 Unix 时间戳（秒）。
- 时间范围过滤的起止边界均包含在查询内。

## 当前限制

- Trace Session 只在当前 MCP Server 进程内有效；服务重启后需要重新加载 BLF。
- 当前采用紧凑内存存储，不写额外大型磁盘缓存；日志仍需有足够内存容纳必要字段和原始 payload。
- ARXML 仅支持 cantools 能直接解析的文件，尚未增加厂商扩展兼容层。
- 时序 Tool 只返回客观统计；没有可靠 expected period 时不会自动判断丢帧。
- 当前只支持 BLF 日志输入，DBC 路径已完成端到端验证。

架构与实现决策详见 [`docs/第1轮开发日志.md`](docs/第1轮开发日志.md)。
