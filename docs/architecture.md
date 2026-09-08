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
Trace Analysis Skill               Harness 直接选择 Tool
       │ 自主选择只读 Tool                     │
       ▼                                      ▼
Automotive Trace MCP              Automotive Config MCP
       │                                      │
BLF + 可选 DBC/ARXML             工程源码/生成配置 + 可选 ARXML
       │                                      │
       └────────────── 可审查的本地证据  ──────┘
```

Codex 的分发单元是 `plugins/automotive-network-debug-agent/`。其中同时包含 Trace
Skill、两个 MCP 配置、PowerShell 启动器和 Python 源码；安装后从 Codex 插件缓存运行，
不依赖开发仓库路径。仓库根目录的 `.agents/plugins/marketplace.json` 只负责发布
市场索引，不存放另一份 Skill。

模型由用户当前的 Harness 登录态提供。本项目既不实现模型运行时，也不读取
`OPENAI_API_KEY`；本地 MCP 只执行确定性的 Trace 或 Config 查询。当前尚未实现
Config Skill/Agent，Config MCP 不自动调用 Trace MCP。

Config 侧以用户明确给出的工程路径为主要范围。源码索引可以在没有 ARXML 时独立
工作；用户需要 CAN ID、PduR、Com、Signal Gateway 或 I-PDU Group 等 AUTOSAR
关系时，再把本次确定采用的 ARXML 作为补充输入。源码与 ARXML 的自动关联仅基于
显式引用或完全相同的标识符，不把相似名称当作确定性证据。

## 未来多源架构

下图是尚未实现的后续方向，不代表当前版本已经具备完整根因诊断能力：

```text
Harness
Codex / Claude Code / 其他兼容环境
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

二期 TODO：

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
