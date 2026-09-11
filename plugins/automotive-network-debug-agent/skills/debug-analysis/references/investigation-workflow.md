# CAN→LIN timeout 与调查报告工作流

仅在跨层、反证、CAN→LIN timeout 或报告任务中读取本文件。

## 阶段顺序

1. 规范化用户提供的 BLF、DBC/LDF、工程、ARXML、HTML/测试报告与输出路径；缺少工程路径或本次采用的 ARXML 时分别询问，不猜相邻目录或版本。
2. 分别调用 `get_trace_health` 与 `get_config_health`。任何必需 MCP 不可用时在读取大型数据前停止。
3. 确认文件可访问性和版本对应关系。HTML 只作为用户证据记录；当前 MCP 不解析 HTML 时明确标记未验证，不用文本搜索冒充专用解析。
4. `load_trace` 一次并等待，取得可用的 `trace_id`。
5. 收集“逻辑网段 ↔ 数字分析 Channel ↔ 可选 ECU Channel”，用该 `trace_id` 调用 `set_channel_mapping`；随后使用 `analyze_routed_signal_timeout` 一次返回源停止事件、各 LIN Channel timeout 延迟、周期、错过的真实帧数、min/P50/P95/max 和有限异常原始帧。没有明确映射不做跨 Channel 统计；保留 BLF `timestamp_reference`，UNKNOWN 仍写 UNKNOWN。
6. `load_config_workspace` 一次并等待。先 `trace_autosar_runtime_chain`；需要补源码时 `plan_source_search` 后一次 `search_source_evidence`，只有明确行号才 `read_source_lines`。
7. 将问题拆成可验证候选。每个候选记录支持证据、`counter_evidence`、搜索范围、置信度与仍需的最小打点。不得模拟 BswM/ComM/Com/LinIf 运行时状态机。
8. 用户要求持久化时，在其明确输出目录调用 `create_investigation_bundle`。把 Channel 映射、Trace 事件、静态链、源码切片、反证和健康检查分别记录；更新 hypotheses/open questions 后先读取摘要核对数量。
9. 调用 `export_investigation_markdown` 生成 Markdown。PDF 后端 unavailable 时明确交付 Markdown/JSON，不自行引入模型 API 或假装生成成功。

## 性能与停止规则

- 同一 BLF/数据库、工程/ARXML 在未变化时复用原 ID；状态等待不重复 Load。
- Config 项目索引按绝对路径、大小和 mtime 增量失效；用 `get_project_index_status` 审计复用情况。
- 冷索引超过约 5 秒时，根据状态 Tool 的 stage、已扫描/已索引数量和进度百分比向用户发简短更新。
- 大型 generated source 默认零上下文，每条局部窗口 500–1000 字符，排除 referable-key 汇总；不得把整条超长行或整文件返回给模型。
- 得到足以回答当前问题的证据后停止，不把报告工作变成新一轮无边界扫描。
