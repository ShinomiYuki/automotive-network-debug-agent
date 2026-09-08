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
     Trace Analysis Skill（调查策略与证据边界）
                 │ 自主选择只读 Tool
                 ▼
         Automotive Trace MCP（本地 stdio）
                 │
                 ▼
       BLF + 可选 DBC/ARXML（始终留在本机）
                 │
                 ▼
现象判断 / 关键证据 / 不确定项 / 下一步建议
```

Codex 的分发单元是 `plugins/automotive-network-debug-agent/`。其中同时包含
Skill、MCP 配置、PowerShell 启动器和 Python 源码；安装后从 Codex 插件缓存运行，
不依赖开发仓库路径。仓库根目录的 `.agents/plugins/marketplace.json` 只负责发布
市场索引，不存放另一份 Skill。

模型由用户当前的 Harness 登录态提供。本项目既不实现模型运行时，也不读取
`OPENAI_API_KEY`；本地 MCP 只执行确定性的 Trace 查询。

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
