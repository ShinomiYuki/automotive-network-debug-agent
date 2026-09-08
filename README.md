# 一种多源数据驱动的汽车网络故障诊断智能体

这是一个可安装到 Codex 的汽车网络日志调查插件。它把专业 Trace Skill 与本地 Trace MCP 打包在一起，接收自然语言问题后自主选择 Tool，分析 Vector BLF 及可选 DBC/ARXML，并回答“测试日志中实际发生了什么”。

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

## 安装为 Codex Plugin

要求 Python 3.11 或更高版本。建议复用已有 Conda 环境，不需要为插件另建环境。先在选定环境中安装一次运行依赖：

```powershell
conda activate <你的环境名>
python -m pip install "git+https://github.com/ShinomiYuki/automotive-network-debug-agent.git@main"
```

把该环境的解释器绝对路径写入仅属于当前用户的本机配置。以下配置不在 Git 仓库中，也不会随插件发布：

```powershell
$configDir = Join-Path $env:LOCALAPPDATA "AutomotiveNetworkDebugAgent"
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
(Get-Command python).Source | Set-Content -LiteralPath (Join-Path $configDir "python-path.txt") -Encoding utf8
```

然后把 GitHub 仓库加入 Codex 插件市场并安装插件：

```powershell
codex plugin marketplace add ShinomiYuki/automotive-network-debug-agent --ref main
codex plugin add automotive-network-debug-agent@shinomi-yuki
```

安装或更新后请新建一个 Codex 会话，让新的 Skill 与 MCP Tool 进入会话工具目录。在 ChatGPT 桌面应用中也可以从插件目录选择该市场并安装。

本项目不会在 Trace MCP 每次启动时展开一份独立依赖，也不会为日志自动生成大型磁盘数据库。插件从自己的安装缓存读取 `anda` 源码，Trace Session 使用进程内紧凑数组保存必要帧数据。

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

## 在 Codex 中使用

插件包含：

- `skills/trace-analysis/SKILL.md`：专业调查指令与证据边界；
- `.mcp.json`：随插件注册的 `automotive-trace` 本地 MCP；
- `scripts/run_trace_mcp.ps1`：复用已有 Python/Conda 环境的启动器；
- `src/anda/`：随插件分发的 Trace Core 与 MCP 实现。

显式调用时，在 Codex CLI 中输入 `$trace-analysis`，或在 ChatGPT/Codex 插件界面用 `@` 选择 Trace Analysis。隐式触发时直接提供 BLF、可选 DBC 与问题描述；Skill 的描述和 `allow_implicit_invocation: true` 会允许 Harness 自动选择它。

解释器也可临时通过 `ANDA_PYTHON` 环境变量指定。不要把个人代理、解释器绝对路径或其他机器配置写入 `.mcp.json` 或插件清单。项目本身不要求代理；确有网络需要时，应由用户在自己的运行环境中配置。

在 Codex 中用自然语言提供问题和绝对路径，例如：

~~~text
BLF：D:\logs\drive.blf
DBC：D:\logs\vehicle.dbc
问题：为什么 CAN2 上看不到 0x416？
~~~

问题也可以采用工程缺陷单常见的结构：`测试路由/测试网段`、`简要描述`、`前提条件`、`操作步骤`、`预期结果`、`实际结果`。Codex 会从中提取调查对象，但仍以实际 BLF/DBC Tool 结果作为证据。

## 单独启动 Trace MCP

开发者在仓库根目录安装项目后运行：

```powershell
python -m anda.mcp.trace.server
```

默认使用 stdio transport，适合由 MCP Client 启动和连接。

## 兼容性边界

- Codex Plugin 的 `.codex-plugin/plugin.json`、插件市场与安装命令是 Codex/ChatGPT 的分发机制，其他 Harness 不能假定可一键安装同一插件。
- `skills/trace-analysis/SKILL.md` 遵循 Agent Skills 目录形式；支持该规范的 Harness 可以复用专业指令。
- `automotive-trace` 是标准 stdio MCP。任何支持本地 stdio MCP 的 Harness 都可以直接连接，但需要按该 Harness 的配置方式注册启动命令。
- 插件不依赖 OpenAI API，也不创建、读取或要求 `OPENAI_API_KEY`。云端模型由使用者自己的 Harness 登录态提供，本地 MCP 只负责读取本机文件。

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
- 安装或升级插件后必须新建会话，旧会话不会动态获得新 Skill 或 MCP Tool。
- Codex IDE 扩展当前不支持 Plugin；可在 Codex 桌面应用或 CLI 使用插件，或在 IDE 中单独安装 Skill 并手工注册 MCP。

## Harness 验收用例

`evals/trace_agent_cases.json` 是工程内长期保留的确定性验收规格，不是临时测试文件。它记录典型问题应选择的最短 Tool 路径、禁止的无意义调用和固定输出章节，供自动测试与人工真实会话验收共同使用。

## 开源许可证

本项目采用 [GNU Affero General Public License v3.0](LICENSE)（SPDX：`AGPL-3.0-only`）开源。完整条款见仓库根目录的 `LICENSE` 文件。
