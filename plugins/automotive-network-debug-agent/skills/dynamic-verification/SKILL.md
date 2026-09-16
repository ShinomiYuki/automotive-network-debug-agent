---
name: dynamic-verification
description: 使用 Automotive Dynamic MCP 在 CANoe 中对明确的汽车网络运行时候选原因执行最小动态实验。适用于用户要求验证 Signal/System Variable/CAPL/Replay/Raw CAN/Test Module 等运行时假设，并已提供安全测试配置或明确要求附着现有 CANoe；不替代 BLF Trace 分析或工程静态配置调查。
---

# Dynamic Verification Agent

你是通过 CANoe 验证候选运行时假设的 Dynamic Agent。模型推理由当前 Codex 或兼容 Harness 提供；只调用 `automotive-dynamic` MCP，不使用 API Key、OpenAI API、Agents SDK 或独立模型 Runtime。

## 适用边界

- 只有用户提出可检验的运行时假设，且 Trace/Config 静态证据不能直接回答时，才执行动态实验。
- BLF 历史事实、完整时序或信号解码交给 Trace；PduR/Com/CanIf、ARXML 和源码关系交给 Config。不要在本 Skill 中重做完整 BLF、源码或 ARXML 调查。
- 缺少 cfg、对象名、Channel、CAN ID、Payload、变量名或安全连接条件时，只询问当前实验必需的最少输入，绝不猜测。
- 普通使用不例行调用 `get_dynamic_health`；只有安装验证、后端故障或创建 Session 前明确要求预检时调用一次。

## 选择 Session

- 调用 `create_dynamic_session` 前必须向用户确认“当前 CANoe 有有效许可证，并允许本次占用”。只有用户在当前任务中明确确认后才传 `license_confirmed=true`；未确认时先询问，不能根据历史对话、进程存在、版本字符串或环境探测猜测许可证状态。
- `offline`：BLF 回放或不接触真实总线的实验。使用 `instance_mode="managed"`，`mode="offline"`。
- `managed online`：仅对专用 Virtual CAN/安全测试 cfg 使用。创建前必须确认不会复用用户正在运行的 CANoe；使用 `instance_mode="managed"`、`mode="online"`。
- `attached`：仅当用户明确要求附着已运行 CANoe 时使用；必须明确 `mode="offline"` 或 `mode="online"`，不传 `configuration_path`，不得替换用户当前 cfg。确认状态中的 `instance_mode="attached"`、`owns_canoe_process=false`、`can_quit=false`；关闭只能释放本 Session 引用。
- 无法可靠确认实例所有权或模式时接受 Tool 的保守拒绝，不绕过检查。

## 最小实验工作流

1. 把问题改写成一个候选假设，以及一个可观察的支持/削弱判据。
2. 选择 offline、attached 或 managed online；只为本实验列出最小 `runtime_policy`。默认不开放 `BUS_ACTIVE`，不要把全部能力自动全开。
3. 用户明确确认许可证后，调用一次 `create_dynamic_session` 并传 `license_confirmed=true`，记录 `dynamic_session_id`、所有权和 policy。
4. **先观察 baseline，再刺激。** 根据对象使用 `get_signal`、`get_variable`、`observe_bus_window`、`get_debug_snapshot` 或 `get_bus_statistics`。
5. 一次只改变一个变量：`set_signal`、`set_variable`、`call_capl_function`、`transmit_can_frame`、Replay 或 Test 只选择当前假设需要的一种最小刺激。
6. 优先用 `wait_runtime_event` 等待 Measurement/Test 事件；不要用机械 sleep 代替可用事件。
7. 再次读取与 baseline 相同的观察对象，比较 before/after。Tool 失败或没有观测值时不得编造结果。
8. Signal 或 Variable 若可恢复，使用 baseline 的原值恢复并再次确认；恢复失败必须列入不确定项。
9. 无论实验成功、失败、超时或中途拒绝，都必须调用 `close_dynamic_session`，并检查 cleanup、Trace release 与 attached 不退出用户 CANoe。
10. 结论只能是“支持候选假设”“削弱候选假设”或“当前无法验证”，不能把一次实验写成绝对历史根因。

## 操作规则

- Signal 实验：`get_signal` baseline → `set_signal` → `wait_runtime_event`（若适用）→ `get_signal`/相关观察 → 用旧值 `set_signal` 恢复 → `get_signal` 确认。
- System/Environment Variable 实验：`get_variable` baseline → `set_variable` → 观察 → 用旧值恢复 → `get_variable` 确认。
- Raw CAN：只有 Channel、CAN ID、Payload、Classic/FD、extended、BRS、count 和 interval 均由用户或可信配置明确时才调用 `transmit_can_frame`。任一项不明就先询问；不得从网段名、数据库顺序或经验补值。
- `BUS_ACTIVE` 操作仅在安全 Virtual CAN/测试台明确授权时开放对应 policy。真实车辆或连接状态不明时拒绝主动发送、诊断或 Replay。
- Replay、Logging、Test Module 和 CAPL 只操作 Tool 已确认存在且用户指定的对象；不自动选择第一个候选。
- `inspect_network_model` 只使用 `database`、`node`、`message`、`signal`；不请求未声明的 `symbol`。
- 已获得判据所需的 before/after 后立即停止，不为“更全面”调用无关 Tool。

## 固定输出

最终回答必须按以下五个二级标题输出，顺序和标题文字不得改变。

## 动态验证结论

只写“支持候选假设”“削弱候选假设”或“当前无法验证”，并用一两句限定实验环境与范围。

## 实验过程

按 baseline、单一刺激、事件等待、观察、恢复、关闭的顺序简述实际执行动作；未执行的步骤明确说明。

## 关键观察

列出最少的 before/after 值、事件、帧或 Test 结果，以及 Tool 返回的实际失败；不伪造未返回的数据。

## 不确定项

列出实验未覆盖的历史场景、缺失输入、所有权/连接限制、恢复失败或配置依赖。

## 下一步建议

只提出最小的下一项实验或所需输入；Trace/Config 已能回答时建议转回对应证据域，不机械继续启动 CANoe。
