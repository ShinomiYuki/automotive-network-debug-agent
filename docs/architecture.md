# 架构说明

<!--
文件用途：
- 记录项目当前约定的主架构。
- 这里只记录已经讨论确认的结构，不提前设计尚未确认的复杂细节。
-->

## 当前可运行架构

```text
Codex / 兼容 Agent Harness（提供模型推理）
        │
        ▼
Debug Analysis Skill（按证据需要路由）
    /          |             \
   ▼           ▼              ▼
Trace Skill  Config Skill  Dynamic Verification Skill
   │           │              │
Trace MCP   Config MCP    Dynamic MCP
   │           │              │
BLF/Database  工程/ARXML   CANoe COM Automation
   │            │
BLF/Database  工程源码/Generated Config
              + 可选 ARXML
```

`trace-analysis` 与 `config-analysis` 仍是可独立调用的专业入口：

```text
Trace Analysis Skill ──→ Automotive Trace MCP
Config Analysis Skill ─→ Automotive Config MCP
Dynamic Verification Skill ─→ Automotive Dynamic MCP
```

Codex 的分发单元是 `plugins/automotive-network-debug-agent/`。其中同时包含 Trace、
Config、Dynamic、Debug 四个 Skill、三个 MCP 配置、PowerShell 启动器和 Python 源码；安装后从 Codex 插件缓存运行，
不依赖开发仓库路径。仓库根目录的 `.agents/plugins/marketplace.json` 只负责发布
市场索引，不存放另一份 Skill。

模型由用户当前的 Harness 登录态提供。本项目既不实现模型运行时，也不读取
`OPENAI_API_KEY`。三个专业 Skill 分别自主选择对应 MCP Tool；Debug Skill 按问题动态
选择 Trace、Config 和必要时的 Dynamic。Dynamic 只用于验证一个明确运行时假设，不是
固定第三阶段。四个 Skill 均直接使用现有 MCP，不进行 Skill-to-Skill 调用。

Trace 与 Config MCP 的 Load Tool 都只负责规范化输入、去重后台任务并立即返回稳定 ID。大型 BLF
或工程索引由受限后台 worker 完成，Harness 通过状态 Tool 读取阶段、计数、进度和耗时；
同一输入在 `queued/loading` 期间重复 Load 只返回原 ID，不会启动第二次扫描。BLF 帧及
DBC/LDF 定义在 Trace MCP 进程内复用；Config 的源码文件清单、标识符出现位置和行 byte
offset 使用用户缓存目录中的 SQLite 持久化，并按绝对路径、大小、mtime 增量失效。共享基础索引负责增量构建，每个 Config Workspace 查询固定的不可变 generation，使其源码证据与当次内存 ARXML 保持同一加载代。

Config 侧以用户明确给出的工程路径为主要范围。源码索引可以在没有 ARXML 时独立
工作；用户需要 CAN ID、PduR、Com、Signal Gateway 或 I-PDU Group 等 AUTOSAR
关系时，再把本次确定采用的 ARXML 作为补充输入。源码与 ARXML 的自动关联仅基于
显式引用或完全相同的标识符，不把相似名称当作确定性证据。

## Dynamic Verification Core

Dynamic MCP 是有状态、单 Session 的 CANoe 适配层：

```text
create_dynamic_session
        │
        ├─ managed / BLF ─→ 工作区外临时 .cfg
        ├─ managed / cfg ─→ 同目录影子 .cfg + 原文件摘要
        └─ attached ──────→ 唯一活动 CANoe，只持有 COM 引用
        │
        ▼
单一 Backend Worker Thread
        │
        ▼
py-canoe 26.4.1 / CANoe COM
        │
        ├─ Observation
        ├─ Offline Animation / Measurement
        ├─ Runtime Mutation
        └─ Explicit BUS_ACTIVE
```

所有 CANoe COM 对象仅在专用 worker 线程创建和访问，避免跨 Apartment 使用。Session
managed 关闭时按“停止本 Session 操作 → 退出所拥有 CANoe → 删除影子配置 → 校验原配置 → 删除临时目录”收敛；
attached 仅停止本 Session 启动的操作并释放 COM 引用，绝不调用 `quit()`。状态顶层返回
`instance_mode`、`owns_canoe_process` 和 `can_quit`。创建前通过系统进程表和 COM 活动对象
双重确认所有权；无法唯一确认时保守拒绝。
清理失败会作为结构化结果返回。`get_dynamic_health` 只做静态环境探测，不启动 CANoe
或占用许可证。

`auto` 模式始终解析为 offline。BLF 进入 CANoe Offline Source，普通 Measurement
Start/Reset 在 offline Session 中被拒绝，播放只能经 Offline Animation 控制。用户明确
选择 online 时必须同时提供 `.cfg`，并仍受 runtime policy 控制。

Runtime capability 默认只包含 `observation`、`offline_animation` 与 `measurement`。
Signal/Variable 写入、CAPL 调用、Replay、原始帧注入、诊断、测试和 Logging 必须在创建
Session 时显式授权。工具元数据再把操作分为 `READ_ONLY`、`LOCAL_MUTATION`、
`RUNTIME_MUTATION` 和 `BUS_ACTIVE`；其中总线主动操作保持 destructive/open-world 标记。

Dynamic 的 BLF 窗口观察直接复用 Trace Core 的索引与有界输出，不通过 Trace MCP 产生
MCP-to-MCP 调用。CANoe 实时读取、播放游标和运行事件由 Dynamic backend 提供，两类事实
在 Session 返回值中明确标注来源。Session 关闭会调用私有 `TraceSessionManager.release_trace`；
即使索引仍在后台 loading，完成后也不会重新注册为常驻 Session。

cfg-only 网络模型从当前 CANoe Configuration 已加载数据库路径构建统一 Database 索引，
稳定支持 Database、Node、Message、Signal。Node 来自当前 CANoe Bus COM 模型，Message 与
Signal 使用项目已有 DBC/ARXML/LDF 解析器；未可靠实现的 Symbol 已从 Tool Contract 删除。

原始 CAN/CAN FD 注入使用配置内的 `ANDA_TransmitCanFrame` CAPL Bridge，并在调用前限制
ID、Classic/FD 载荷长度、帧数、间隔和总时长。Bridge 不存在时返回不支持；Core 不隐式
改写用户工程。Replay 与 Offline Source 是不同对象：前者操作明确 Replay Block，后者只
用于离线测量播放，不能混用控制接口。

## 当前 Debug 调查路线

```text
Trace only
Config only
Trace → Config
Config → Trace
Trace/Config → Dynamic（仅明确运行时假设）
```

Debug Skill 只在阶段之间传递 CAN ID、Message/PDU/Signal 名、源/目标网段、Channel、
时间范围和已确认现象等有限事实。原始帧列表、整份 ARXML、大段源码和整批 Tool 结果
不跨域复制。Trace 与 Config 矛盾时优先检查 BLF、DBC、工程源码和 ARXML 的版本一致性，
不把相关性直接写成因果。

跨 Channel 调查必须先把用户明确给出的“逻辑网段 ↔ 分析 Channel ↔ ECU Channel”登记到
Trace Session；配置顺序和命名均不能用于猜测映射。CAN→LIN timeout 使用专用关联查询，
静态侧使用 `trace_autosar_runtime_chain` 组织 CanIf/PduR/Com/LinIf/OS-RTE/BswM/ComM/
CDD 的生产者、消费者、任务、处理模式、控制引用与限定范围反证。用户要求持久化时，
Config MCP 可在明确输出目录创建 JSON investigation bundle 并确定性导出 Markdown。

## Harness-native Agent 结构

当前四个 Skill 由同一 Harness 原生运行，不实现独立 Subagent 或模型 Runtime：

```text
Codex 本地 Harness
              │
              ▼
        Debug Agent
       故障调查主智能体
        /      |       \
       /       |        \
      ▼        ▼         ▼
 Trace Agent Config Agent Dynamic Agent
      │        │          │
 Trace MCP Config MCP  Dynamic MCP
```

Dynamic Skill 以“假设 → 最小 policy → baseline → 单一刺激 → 事件等待 → 观察 → 恢复 →
close”为固定流程。Debug 只有在 Trace/Config 仍留下明确可检验假设时才进入该流程。

未来 TODO：

```text
Debug Agent
    │
    └── Case MCP
           └── 历史问题库
               └── 飞书同步 / 本地索引
```

三期展望：

- 机器学习异常检测
- 异常时间窗发现
- 报文 / 信号模式识别
- 历史故障模式聚类
