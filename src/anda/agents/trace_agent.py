"""
文件用途：
- 定义 Trace Agent。
- Trace Agent 只负责 BLF / DBC / CAN/CAN FD 日志调查。
- 它通过 Trace MCP 获取结构化分析结果，并把原始大量帧压缩成证据与结论。

当前状态：
- TODO 占位。
- 等 Trace MCP 的 Tool Contract 稳定后再接入 Agent SDK。

设计约束：
- 不直接读取整个 BLF 到模型上下文。
- 不直接承担 Config / CANoe / 历史案例分析。
- 输出应尽量结构化：finding / evidence / confidence / next_question。
"""

# TODO：接入 OpenAI Agents SDK 或兼容 Harness。
