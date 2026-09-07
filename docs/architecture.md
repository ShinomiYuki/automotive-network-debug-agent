# 架构说明

<!--
文件用途：
- 记录项目当前约定的主架构。
- 这里只记录已经讨论确认的结构，不提前设计尚未确认的复杂细节。
-->

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
