# TSMaster MCP 一期使用说明

本工具用于测试外部实体域控板的 CAN 路由。TSMaster 在明确的源通道发送有限刺激，在目标通道记录域控的实际输出；测试结束后关闭新的测量 BLF，再按明确用例分析完整日志。源、目标标识符可以相同，也可以显式映射。基础检查只确认目标通道、标识符、帧类型和观察窗口，不默认要求全部字节相同。

TSMaster 使用独立的 `automotive-tsmaster` MCP 服务和进程外 `TSMaster.TSApplication` COM 对象。它不改动现有 CANoe 工具契约，也不通过原生 SDK 另建硬件上下文。当前只支持本服务拥有的独立实例，每个服务进程同时只允许一个未关闭会话；发现既有实例或无法确认唯一所有权时拒绝继续。

## 本机验证边界（2026-10-22）

使用 Miniconda Python 3.12.14（64 位）连接本机 TSMaster 2023.3.21.848（32 位），已实际创建独立进程，并完成空白 `.T7z` 工程的新建、保存、加载及产物检查。应用关闭可能出现保存提示；后端仅处理自有实例的明确保存提示，正常关闭未完成时返回 `remaining_process_ids` 和清理错误，不强制结束用户程序。

旧版 COM 的最小 `set_can_channel_count(2)` 调用返回错误 82，描述为 `libTSMaster generic operation failed`。该失败也发生在尚未配置数据库和硬件的最小调用中；目前没有证据确认其根因。后续通道映射、DBC 持久化、回放、真实发送和测量记录尚未在该安装上验证通过。用户已确认本轮先交付代码与离线验证，并保留这项本机阻碍；本轮没有连接硬件或发送报文。

默认测试只运行模拟接口和合成日志。可用环境准备好后，可显式设置 `ANDA_TSMASTER_COM_SMOKE=1` 运行 `tests/integration/test_tsmaster_com_offline.py`，验证独立工程、双 DBC、虚拟通道映射读回、保存回载和关闭。该测试只配置虚拟参数，不连接总线；本机当前执行结果是上述错误 82，不能记为通过。

本文描述实现契约和操作步骤。模拟测试、合成日志结果与真实台架结果必须分别记录；本文不代表任何实际设备、接线、波特率或被测域控已经通过验收。

## 安装与启动

使用 Windows、PowerShell 7，以及已安装项目依赖的 Miniconda Python 3.11 或更新版本。TSMaster 自带的 Python 3.8 不满足本项目要求。进程外 COM 与 x86 原生 DLL 有不同的位数约束，不要在 x64 Python 中直接加载 x86 SDK。

在工程根目录运行以下命令，将占位解释器路径替换为本机实际 Miniconda 环境路径：

```powershell
$env:ANDA_PYTHON = 'C:/REPLACE_WITH_MINICONDA/envs/REPLACE_ENV/python.exe'
& $env:ANDA_PYTHON -m pip install -e '.[tsmaster]'
& ./plugins/automotive-network-debug-agent/scripts/run_tsmaster_mcp.ps1
```

`tsmaster` 可选依赖在 Windows 安装 `pywin32`。启动器沿用项目的解释器选择规则；`ANDA_PYTHON` 指定的解释器优先。插件的 `.mcp.json` 注册相同服务，配置中不需要写入个人机器路径。服务使用标准输入输出通信，直接运行后会等待 MCP 客户端请求。

先调用 `get_tsmaster_health`。该工具读取 Python 位数、COM 注册和既有进程，不激活 TSMaster，也不连接硬件。COM 注册存在只能证明入口可发现，不能证明工程、硬件或记录已可用。

## 八个工具

| 工具 | 输入与用途 |
| --- | --- |
| `get_tsmaster_health` | 无参数；只读环境检查，不启动应用。 |
| `create_tsmaster_session` | `workspace`、`project_name`、`bench`；创建并配置独立工程。必须明确 `application_authorized=true`。可选 `template` 及 `template_transmitters_reviewed`。 |
| `get_tsmaster_status` | `session_id`；读取工程状态、异步执行状态、有限实时反馈和结果摘要。 |
| `run_tsmaster_test` | `session_id`、`cases`、三个时间参数、`evidence_context`；在 `frames` 与 `replay` 中选择一种刺激，明确 `bus_authorized=true` 后连接、记录和执行。 |
| `stop_tsmaster_test` | `session_id`；提交取消标志，随后查询状态确认实际清理结果。 |
| `close_tsmaster_session` | `session_id`；清理自有资源并请求关闭自有实例，检查最终状态。 |
| `analyze_tsmaster_log` | `blf_path`、`cases`、`evidence_context`、可选 `database_bindings`；仅分析已经关闭的测量日志，不启动应用。 |
| `get_tsmaster_result` | `session_id`、可选 `offset=0`、`limit=10`；按用例分页读取结果，每页最多 10 条。 |

创建应用与连接总线是两项独立授权。`application_authorized` 只允许启动、配置、保存和加载独立测试工程；它不允许连接设备或发送。只有取得本次台架、通道及刺激清单的明确授权后，才将 `bus_authorized` 设为 `true`。开发任务本身不构成任意总线发送许可。

首次授权启动应用前，还需确认本机启动设置不会自动连接上次工程或启动其发送模块。COM 激活发生在创建空白工作工程之前，后端不能撤销应用启动时已经发生的外部发送；未确认该前提时不要启动测试实例。

## 工程、通道与数据库

`create_tsmaster_session` 的 `workspace` 必须是已有本地目录，`project_name` 必须为单一 `.T7z` 文件名。服务在工作目录下新建 `tsmaster-<session_id>` 子目录，准备工程、配置台架、保存并回载核对。可用的模板必须是明确审核过的独立测试模板；只有已确认模板没有自动启动的回放、周期发送、剩余总线仿真和 SimpleGateway，才传入 `template_transmitters_reviewed=true`。不要把正在使用的业务工程作为可随意丢弃的工作工程。

三类映射分别提供，不要把同一个编号直接传给所有接口：

| 映射 | 对外输入 | 后端处理 |
| --- | --- | --- |
| 原 BLF 通道到测试逻辑通道 | `replay.selections[].input_channel` 与 `logical_channel` 均为一基 | `AMappings` 按原 BLF 通道顺序排列，目标值为一基，`0` 表示忽略。 |
| 测试逻辑通道到硬件端口 | `bench.channels[].logical_channel` 为一基；`device_index`、`device_channel` 为零基 | COM 逻辑通道索引转换为 `logical_channel - 1`，设备及端口按明确参数配置并读回。 |
| DBC 到网络和逻辑通道 | `bench.databases[] = {path, network, logical_channel}` | 文件必须存在，网络和逻辑通道必须与台架配置相符；COM 数据库通道参数转换为零基。 |

网络名、逻辑通道和硬件物理端口必须各自唯一。台架至少包含一个 `source` 和一个 `target`。`allowed_ids` 只批准普通源刺激；目标口的 `allowed_ids` 必须为空。`response_ids` 是显式模拟诊断响应的发送例外，不能用来批准普通目标输出。

每个通道都必须明确设备类型、子型号、设备索引、物理端口、设备名、仲裁和数据波特率、控制器类型及终端电阻。速率单位为千比特每秒。`controller_type` 为 `0`（经典 CAN）、`1`（ISO CAN FD）或 `2`（非 ISO CAN FD）。后端使用正常控制器模式，目标端保持正常 CAN 确认能力；不默认使用只监听或关闭确认。终端电阻必须由台架实际接线决定。

映射读回、工程文件存在和回载核对会保存在会话状态中。波特率实际读回、数据库通道成员关系及真实端口行为仍须按台架步骤核实；看到配置调用没有异常不能替代这些核实。

## 最小执行流程与示例

插件提供以下合成示例：

- `examples/tsmaster/bench.example.json`：供创建工具的 `bench` 参数使用。
- `examples/tsmaster/run.example.json`：供执行工具的完整参数使用。

示例的设备、速率、通道、标识符和负载均用于说明格式，不是任何域控的有效配置，不能直接用于实物。设备名和会话标识符需要替换，所有台架字段需要重新核对。示例保留 `bus_authorized=false` 和未核实的证据标志，不会形成有效台架授权。

推荐操作顺序如下：

1. 确认本次台架参数与授权，先调用健康检查。
2. 将已核对的台架对象传给创建工具；记录返回的 `session_id`，轮询至 `state=ready` 且 `operation_pending=false`。创建过程不会连接硬件。
3. 将独立路由要求写成 `cases`，明确源目标标识符、帧类型、窗口和必要数据规则。替换运行示例中的会话标识符，提供本次证据背景和发送授权。
4. 调用执行工具。服务先开始记录并等待基线，再发送或回放，刺激结束后继续记录规定响应时间，随后清理回放、记录与连接，再分析新的 BLF。
5. 通过状态工具观察 `operation_pending` 和运行状态。实时 `feedback` 只保留有限样本，`authoritative=false`，不能替代完整日志判定。
6. 需要中止时调用停止工具，继续查询直至操作结束。停止请求被接受不等于硬件已经停止；检查结果中的 `cleanup_errors`。
7. 读取结果和本次目录中的 `result.json`、`measurement.blf`。结束时调用关闭工具，并确认最终 `closed`；`closing`、`close_failed` 或仍有操作时不能宣称已释放应用。

基线执行时间范围为 0 至 10 秒，响应时间大于 0 且不超过 30 秒，总时限大于 0 且不超过 120 秒。执行响应时间必须覆盖每条用例的 `window_seconds`，执行基线不得短于用例的 `baseline_seconds`，刺激加基线和响应的计划总时间必须小于本次时限。

有限直接发送通过 `frames` 指定通道、标识符、标准或扩展类型、CAN 或 CAN FD、BRS、`data_hex`、`count` 和 `interval_ms`。每次最多 64 项，总计不超过 10000 帧；间隔为 1 至 10000 毫秒。各项依次执行，各项内部按次数发送。CAN FD 长度仅接受 0 至 8、12、16、20、24、32、48、64 字节，后端转换为 DLC 编码；经典 CAN 最多 8 字节且不允许 BRS。

## 源 BLF 在线回放

`frames` 与 `replay` 必须且只能选择一种。以下为替换直接发送部分的合成回放参数，路径仅为占位：

```json
{
  "frames": null,
  "replay": {
    "path": "C:/REPLACE_WITH_APPROVED_INPUT/source-input.blf",
    "selections": [
      {
        "input_channel": 4,
        "logical_channel": 1,
        "arbitration_ids": [256],
        "directions": ["Rx"]
      }
    ],
    "start_seconds": 0,
    "end_seconds": 1,
    "max_frames": 100
  }
}
```

时间片段相对输入日志首帧。服务用明确通道、标识符、旧日志方向和片段筛选完整输入，生成独立 `source-stimulus.blf`。只有批准的源通道可被映射；原日志目标侧历史输出和其他标识符不会进入刺激。示例原通道 4 到逻辑通道 1 对应的映射前四项为 `0,0,0,1`。

旧日志中的 `Rx` 可能恰好是需要重发的源流量，应按采集点明确选择。TSMaster 回放参数中的发送接收方向筛选针对旧日志，不是关闭当前目标硬件的接收。生成后的刺激日志可以同时允许 Tx 和 Rx，因为进入它的帧已经逐项筛选。回放不循环、不自动启动；超过帧数上限或遇到选中远程请求、错误帧和非法帧结构时拒绝整个刺激，不保留可误发的部分文件。

回放完成只说明刺激流程结束。路由结论仍需测量日志中的源刺激与目标接收证据。输入 BLF、筛选后的刺激 BLF和本次测量 BLF 有不同用途，不得互相替代。

## 路由判定与证据背景

普通帧用例的 `source`、`target` 各自明确一基 `channel`、整数 `arbitration_id`、`is_extended_id`、`is_fd`，可附加 `network`、`bitrate_switch`、`data_hex`。目标固定为 `Rx`；普通源默认为 `Tx`。`window_seconds` 必填，`minimum_matches` 默认为 1。`baseline_seconds` 以测量日志首个实际源刺激为界检查此前目标同标识符流量。

基础结论是窗口内的目标存在性，不是逐帧转发率。若基线已有目标同标识符报文，当前实现保守返回证据不足；应先隔离其他发送源或重新明确可识别的关联用例，不能仅凭之后再次出现宣称因果成立。

`evidence_context` 是来自本次实际测量条件的事实声明，各字段不得因 COM 无异常或回放进度达到 100% 就设为 `true`：

| 字段 | 所需事实来源 |
| --- | --- |
| `executed` | 离线分析时由本次执行记录提供；在线执行由服务覆盖为是否进入实际刺激调用，仍需日志源帧佐证。 |
| `capture_complete` | 本次记录覆盖基线、全部刺激和完整响应尾部，写入已停止且采集无已知丢失。在线执行还要求流程完成且清理无错误。 |
| `direction_verified` | 同一设备与记录配置下，对已知本地发送和外部接收核对事件与新 BLF 的方向。 |
| `isolated` | 接线分别对应域控两侧，测试工具未发送目标普通输出，没有未排除的同标识符发送源。 |
| `clock_verified` | 源目标位于同一可核对的记录时间轴；分开的记录器必须有明确时钟对齐依据。 |
| `data_frames_only_verified` | 本次分析对象已核实只有受支持的 CAN 数据帧。现有 `RawFrame` 未保留远程请求标记，不能自动排除远程请求假匹配。 |
| `recorded_channels` | 经实际标记帧和独立解析验证完整记录的输出 BLF 一基通道列表。 |
| `response_origin_verified` | 源端指定 `direction="Rx"` 时，确认该帧由本次真实或明确控制的响应端产生。 |

离线工具必须指向已关闭的新测量日志。在线服务只在停止写入后自动分析，完整扫描与对外证据上限分开：结果计数使用全部帧，源证据、目标证据和匹配对分别最多返回 10 条。`get_tsmaster_result` 对用例分页；完整报告保存在运行目录。

结果状态为 `observed`（观察到预期）、`not_observed`（窗口内未观察到）、`insufficient_evidence`（证据不足）、`not_executed`（未执行）。未观察到描述日志现象，不直接确定域控内部故障原因；缺少源证据、采集前提或可信方向时不报告路由失败或通过。

## 信号路由

信号检查以目标实际接收帧按目标 DBC 解码，不使用剩余总线仿真的缓存值。源目标端点分别增加 `network`，`kind` 设为 `signal`，并提供以下明确规则；路径和信号名均为占位：

```json
{
  "source_dbc": "C:/REPLACE_WITH_DATABASES/source.dbc",
  "target_dbc": "C:/REPLACE_WITH_DATABASES/target.dbc",
  "source_signal": "REPLACE_SOURCE_SIGNAL",
  "target_signal": "REPLACE_TARGET_SIGNAL",
  "factor": 2,
  "offset": 1,
  "tolerance": 0.1
}
```

该对象放入用例的 `signal`。计算规则为 `目标物理值 = 源物理值 × factor + offset`，绝对误差不得超过明确的 `tolerance`。这些参数没有自动推断或通用容差。当前按允许窗口内最新源值寻找合格目标值；不承诺源每一帧都产生独立目标更新。解码失败或多路复用信号当前不可用会保留原因并返回证据不足。

创建会话时，将源目标 DBC 分别加入 `bench.databases`：

```json
[
  {"path": "C:/REPLACE_WITH_DATABASES/source.dbc", "network": "EXAMPLE_SOURCE", "logical_channel": 1},
  {"path": "C:/REPLACE_WITH_DATABASES/target.dbc", "network": "EXAMPLE_TARGET", "logical_channel": 2}
]
```

在线执行自动转交数据库绑定。离线分析的 `database_bindings` 使用 `channel` 字段替代 `logical_channel`，仍为一基。文件、网络和通道必须唯一匹配，同名信号不能替代源目标映射依据。

## 诊断帧正向与反向检查

诊断帧检查使用 `kind="diagnostic_frame"`。至少分开定义请求正向和响应反向两条用例，不猜响应标识符。例如以下为合成说明，所有地址和数据必须由具体诊断要求替换：

- 请求用例：源通道 1、请求标识符 `0x700`、`Tx`，目标通道 2、转发请求标识符 `0x701`、`Rx`。
- 响应用例：源通道 2、响应标识符 `0x708`、`Rx`，目标通道 1、转发响应标识符 `0x709`、`Rx`。真实 ECU 响应作为此用例的源证据，需 `response_origin_verified=true`。

两条用例各自明确帧类型和窗口。真实 ECU 在本次请求后产生的响应不能由旧 BLF 历史响应替代。如果明确模拟单帧响应，发送项必须指定 `purpose="diagnostic_response"`，且该通道 `response_ids` 明确批准响应标识符；对应响应用例的源证据为本地 `Tx`。该例外会出现在发送计划中，不能混同为域控输出。

有限帧列表按既定顺序和间隔发送，不会根据 ECU 流控动态发送 ISO-TP 分段。当前未执行完整 ISO-TP/UDS 事务，不验证多帧流控、服务完成、正负响应语义、会话切换或安全解锁。结果保留 `diagnostic_transaction_checked=false`，整次报告标记完整事务未执行。两条诊断帧路由都观察到，也只能说明声明的两个方向存在匹配帧。

## 台架参数与手动验证

连接前集中准备以下信息，不从示例或旧日志猜测：

- 实际设备型号、旧版支持的类型和子型号、设备身份与索引、物理端口及域控接线网络。
- 各网络的经典 CAN 或 CAN FD、标准或扩展标识符、BRS、速率、终端电阻、正常确认条件，以及域控供电、唤醒和工作模式。
- 源目标路由要求、可发送标识符和合法负载、次数、间隔、观察窗口及数据规则；如有背景同标识符流量，明确隔离方法。
- DBC 文件及版本、网络和通道绑定、已知信号值及转换容差。
- 回放输入、原日志通道、方向、片段和批准标识符；明确目标历史输出的排除规则。
- 诊断请求与响应的两个方向、地址、真实或模拟对端、响应来源；另行确认任何模拟响应发送例外。
- 工作目录、工程名、模板审核记录、本次测量日志位置和应用及总线两项授权。

首次台架按下面顺序验证并记录实际结果：

1. 在没有用户既有 TSMaster 实例时准备独立工程。先不连接，核对新建、保存和回载产物，检查映射和 DBC 文件集合；人工核对速率和数据库通道归属。
2. 检查工程没有自动回放、周期发送、剩余总线仿真或 SimpleGateway。确认目标保持正常 CAN 确认能力且未发送普通测试输出，核对接线与终端。
3. 用已知且已批准的单个标记帧核对源发送、目标外部接收、事件方向和新 BLF 方向；核对所有受测通道、数据、标准或扩展类型、FD/BRS。确认分析对象不含远程请求。
4. 先记录基线，检查目标同标识符是否本来周期出现。若存在，先解决来源歧义；不要把一次出现作为转发因果证据。
5. 使用有限次数刺激检查目标窗口。确认记录先于首个刺激，最后刺激后仍覆盖完整响应窗口；独立解析关闭后的 BLF，核对两侧首尾标记和通道完整性。
6. 执行一个明确无匹配的反例，验证不会把本地目标发送、错误类型或窗外帧当成通过。缺失采集时应返回证据不足。
7. 验证源 BLF 筛选结果，只含批准源流量；确认原目标历史输出未发出，映射的零与一基转换正确，回放结束与停止均可观察。
8. 对已知源信号变化与目标值分别按两端 DBC 核对；诊断只按已批准的正反帧用例检查，保留真实响应来源证据。
9. 分别验证超时、手动停止、连接或记录失败时的清理。检查停止刺激、记录关闭、断开和应用关闭的最终状态；清理失败时保留错误，不宣称资源已释放。

`result.json` 保存执行结果、批准发送计划、回放筛选摘要、证据背景、错误、清理错误和完整分析结果。报告、输入刺激与新测量日志应一起保留，供后续按原始帧复查。

执行和创建操作由专用 COM 线程串行处理。取消请求不能中断厂商正在阻塞的 COM 调用；这种情况下操作仍显示待完成，关闭也可能显示 `closing` 或 `close_failed`。应继续检查最终状态，必要时人工处理已确认的自有实例，不能把请求已提交当作资源已释放。
