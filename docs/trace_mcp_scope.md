# Trace MCP 一期范围

<!--
文件用途：
- 约束 Trace MCP 第一阶段只解决最基础、最可验证的问题。
- 防止一期过度设计。
-->

## 第一批 Tool

- load_trace
- get_trace_summary
- find_messages
- get_message_timing
- decode_signal

## 后续候选

- compare_messages
- get_signal_series
- find_dropouts
- find_transitions
- counter_check
- crc_check
- bus_load
- export_plot

## 当前不做

- 完整自动根因诊断
- 历史问题库
- 机器学习
- CANoe 强依赖
