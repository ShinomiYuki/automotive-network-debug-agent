# 一种多源数据驱动的汽车网络故障诊断智能体

这是一个在 Codex 或兼容 Agent Harness 中运行的汽车网络日志调查 Agent。它接收自然语言问题，自主选择本地 Trace MCP Tool，分析 Vector BLF 及可选 DBC/ARXML，并回答“测试日志中实际发生了什么”。

模型推理由 Harness 提供；项目不启动独立模型运行时，也不需要 API Key。BLF/DBC 和 Trace MCP 均保留在本机。

## 当前可用能力

- 加载 BLF，并在服务进程内复用已解析的 Trace Session。
- 查看日志时间范围、帧数、Channel、CAN ID、经典 CAN/CAN FD 等摘要。
- 按 CAN ID、Channel 和可选时间范围查询原始帧。
- 统计指定报文的周期、最大间隔和抖动。
- 使用 DBC 或 cantools 可直接读取的 ARXML 解码指定信号。
- 按 Message/Signal 名搜索有限数据库候选，为自然语言信号调查定位 CAN ID。
- 自动限制帧与信号样本输出，避免大结果进入 MCP Client 上下文。

## Trace Agent 能做什么

- 某 CAN ID 是否出现在指定 Channel 或时间范围内。
- 指定 Channel 未出现报文时，该报文是否出现在其他 Channel。
- 报文的中位/平均周期、最大间隔和抖动等客观统计。
- 有 DBC/ARXML 时，某个 Signal 属于哪个报文、值如何变化。
- 当前 Trace 证据足以支持什么现象，还有哪些无法确认。

Trace Agent 不会仅凭 BLF 武断判断 PduR、CanIf、源 ECU 软件、硬件、线束或 CANoe 配置根因。需要工程配置或动态验证时，它只会说明证据边界并给出下一步建议。

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

## 在 Codex / Harness 中运行

仓库已提供：

- `.agents/skills/trace-analysis/SKILL.md`：Codex 可自动发现的项目级专业 Agent 指令；
- `.codex/config.toml`：项目级 `automotive-trace` MCP 注册；
- `scripts/run_trace_mcp.ps1`：复用已有 Python/Conda 环境的本地启动器。

首次使用时，在已安装项目依赖的 Conda 环境中打开本项目即可。如果桌面 Harness 没有继承已激活环境，可在本机创建 `.codex/python-path.txt`，只写一行你自己的 `python.exe` 绝对路径。该文件已被 Git 忽略，不会泄露或绑定个人路径。也可临时设置 `ANDA_PYTHON` 环境变量。

不要把个人代理、解释器绝对路径或其他机器配置写入 `.codex/config.toml`。项目本身不要求代理；确有网络需要时，应由用户在自己的运行环境中配置。

在 Codex 中用自然语言提供问题和绝对路径，例如：

~~~text
BLF：D:\logs\drive.blf
DBC：D:\logs\vehicle.dbc
问题：为什么 CAN2 上看不到 0x416？
~~~

Codex 会按问题自动选择 `trace-analysis` Skill，并通过本地 Trace MCP 调查。

## 单独启动 Trace MCP

在仓库根目录运行：

```powershell
python -m anda.mcp.trace.server
```

默认使用 stdio transport，适合由 MCP Client 启动和连接。

## MCP Tool


| Tool                 | 用途                                             |
| ---------------------- | -------------------------------------------------- |
| `load_trace`         | 加载 BLF 及可选数据库，返回`trace_id`            |
| `get_trace_summary`  | 获取时间范围、帧数、Channel、CAN/CAN FD 摘要     |
| `find_messages`      | 按 CAN ID、Channel、时间范围查询有限数量原始帧   |
| `get_message_timing` | 获取帧数、周期、最大间隔与抖动统计               |
| `search_database`    | 按 Message/Signal 名搜索至多 20 个数据库导航候选 |
| `decode_signal`      | 解码指定 CAN ID 中的指定信号并返回有限样本       |

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

## Agent 输出

Trace Agent 的最终回答固定包含四部分：

- **现象判断**：当前日志直接支持的事实结论。
- **关键证据**：支撑结论的最少计数、Channel、时序统计或信号样本。
- **不确定项**：仅凭当前 Trace 无法确认的原因、期望值或缺失输入。
- **下一步建议**：为缩小不确定性而提出的具体下一步。

示例：

~~~text
## 现象判断
0x416 在给定日志的 CAN2 中未观察到，但在 CAN1 中存在。

## 关键证据
- CAN2：0 帧
- CAN1：5234 帧

## 不确定项
仅凭 BLF 无法确认原因属于路由配置、发送条件还是其他工程实现。

## 下一步建议
确认该报文是否要求从 CAN1 路由至 CAN2；若要求，再核对对应网关配置。
~~~

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
- Harness 必须信任并重新加载项目配置后，才能发现新注册的项目 MCP。

## 开源许可证

本项目采用 [GNU Affero General Public License v3.0](LICENSE)（SPDX：`AGPL-3.0-only`）开源。完整条款见仓库根目录的 `LICENSE` 文件。
