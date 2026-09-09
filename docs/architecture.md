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
        Debug Analysis Skill
       动态规划与联合证据综合
          /              \
         ▼                ▼
Automotive Trace MCP    Automotive Config MCP
         │                │
    BLF + 可选 DBC    工程源码/Generated Config
                         + 可选 ARXML
```

`trace-analysis` 与 `config-analysis` 仍是可独立调用的专业入口：

```text
Trace Analysis Skill ──→ Automotive Trace MCP
Config Analysis Skill ─→ Automotive Config MCP
```

Codex 的分发单元是 `plugins/automotive-network-debug-agent/`。其中同时包含 Trace、
Config、Debug 三个 Skill、两个 MCP 配置、PowerShell 启动器和 Python 源码；安装后从 Codex 插件缓存运行，
不依赖开发仓库路径。仓库根目录的 `.agents/plugins/marketplace.json` 只负责发布
市场索引，不存放另一份 Skill。

模型由用户当前的 Harness 登录态提供。本项目既不实现模型运行时，也不读取
`OPENAI_API_KEY`。Trace 与 Config Skill 分别自主选择各自的只读 MCP Tool；Debug
Skill 按问题动态选择 Trace、Config 或两者，并只在第一阶段结果需要进一步静态或
动态证据时进入第二阶段。三个 Skill 均直接使用现有 MCP，不进行 Skill-to-Skill 调用。

Config 侧以用户明确给出的工程路径为主要范围。源码索引可以在没有 ARXML 时独立
工作；用户需要 CAN ID、PduR、Com、Signal Gateway 或 I-PDU Group 等 AUTOSAR
关系时，再把本次确定采用的 ARXML 作为补充输入。源码与 ARXML 的自动关联仅基于
显式引用或完全相同的标识符，不把相似名称当作确定性证据。

## 当前 Debug 调查路线

```text
Trace only
Config only
Trace → Config
Config → Trace
```

Debug Skill 只在阶段之间传递 CAN ID、Message/PDU/Signal 名、源/目标网段、Channel、
时间范围和已确认现象等有限事实。原始帧列表、整份 ARXML、大段源码和整批 Tool 结果
不跨域复制。Trace 与 Config 矛盾时优先检查 BLF、DBC、工程源码和 ARXML 的版本一致性，
不把相关性直接写成因果。

## Codex Subagent 增强 TODO

下图是未来 Codex 专属增强方向，不代表当前版本已经实现独立 Subagent 或 Tool scope：

```text
Codex 本地 Harness
              │
              ▼
        Debug Agent
       故障调查主智能体
        /      |       \
       /       |        \
      ▼        ▼         ▼
 Trace Agent Config Agent CANoe Agent
      │        │          │
 Trace MCP Config MCP  CANoe MCP
```

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
