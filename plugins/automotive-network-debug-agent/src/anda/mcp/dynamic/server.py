"""注册 Dynamic Verification MCP 的 26 个稳定 Tool。"""

from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from anda.dynamic.session import DynamicSessionManager

sessions = DynamicSessionManager()
mcp = FastMCP("Automotive Dynamic MCP")

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
LOCAL_MUTATION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
RUNTIME_MUTATION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
BUS_ACTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


@mcp.tool(annotations=READ_ONLY)
def get_dynamic_health() -> dict:
    """静态检查 Windows、CANoe COM 和 py-canoe；不会启动 CANoe。"""
    return sessions.get_health()


@mcp.tool(annotations=LOCAL_MUTATION)
def create_dynamic_session(
    blf_path: str | None = None,
    database_paths: list[str] | None = None,
    database_mappings: list[dict[str, Any]] | None = None,
    configuration_path: str | None = None,
    mode: str = "auto",
    instance_mode: str = "managed",
    license_confirmed: bool = False,
    runtime_policy: list[str] | None = None,
    visible: bool = False,
    raw_frame_capl_function: str = "ANDA_TransmitCanFrame",
) -> dict:
    """用户确认许可证后创建 managed/attached Session；未确认时不启动 CANoe。"""
    return sessions.create_dynamic_session(
        blf_path=blf_path,
        database_paths=database_paths,
        database_mappings=database_mappings,
        configuration_path=configuration_path,
        mode=mode,
        instance_mode=instance_mode,
        license_confirmed=license_confirmed,
        runtime_policy=runtime_policy,
        visible=visible,
        raw_frame_capl_function=raw_frame_capl_function,
    )


@mcp.tool(annotations=READ_ONLY)
def get_dynamic_status(dynamic_session_id: str) -> dict:
    """紧凑返回 CANoe、配置、回放、测试和活动操作状态。"""
    return sessions.get_dynamic_status(dynamic_session_id)


@mcp.tool(annotations=RUNTIME_MUTATION)
def close_dynamic_session(dynamic_session_id: str) -> dict:
    """清理本 Session；只有 managed 所有者可退出 CANoe。"""
    return sessions.close_dynamic_session(dynamic_session_id)


@mcp.tool(annotations=LOCAL_MUTATION)
def compile_capl(dynamic_session_id: str, write_line_limit: int = 40) -> dict:
    """编译当前 Configuration 的 CAPL，并返回有限错误与 Write Window 证据。"""
    return sessions.compile_capl(dynamic_session_id, write_line_limit)


@mcp.tool(annotations=RUNTIME_MUTATION)
def control_measurement(dynamic_session_id: str, action: str) -> dict:
    """对当前 Session 执行 start、stop 或 reset Measurement。"""
    return sessions.control_measurement(dynamic_session_id, action)


@mcp.tool(annotations=RUNTIME_MUTATION)
def control_offline_animation(
    dynamic_session_id: str,
    action: str,
    animation_delay_ms: int = 100,
) -> dict:
    """对 Offline Animation 执行 start、pause、resume、step、reset 或 stop。"""
    return sessions.control_offline_animation(
        dynamic_session_id, action, animation_delay_ms
    )


@mcp.tool(annotations=READ_ONLY)
def wait_runtime_event(
    dynamic_session_id: str,
    event: str,
    timeout_seconds: float = 10,
) -> dict:
    """通过 COM Event Sink 等待 Measurement/Test 事件，最长 55 秒。"""
    return sessions.wait_runtime_event(dynamic_session_id, event, timeout_seconds)


@mcp.tool(annotations=READ_ONLY)
def observe_bus_window(
    dynamic_session_id: str,
    start_timestamp: float | None = None,
    end_timestamp: float | None = None,
    around_cursor_seconds: float = 0.1,
    channel: int | None = None,
    bus_type: str | None = None,
    arbitration_id: int | None = None,
    limit: int = 100,
) -> dict:
    """复用 Trace Core，观察当前 Playback Cursor 或明确时间范围内的有限帧。"""
    return sessions.observe_bus_window(
        dynamic_session_id=dynamic_session_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        around_cursor_seconds=around_cursor_seconds,
        channel=channel,
        bus_type=bus_type,
        arbitration_id=arbitration_id,
        limit=limit,
    )


@mcp.tool(annotations=READ_ONLY)
def get_signal(
    dynamic_session_id: str,
    bus: str,
    channel: int,
    message: str,
    signal: str,
) -> dict:
    """读取当前 CAN/LIN Signal 的物理值、原始值和时间戳。"""
    return sessions.get_signal(dynamic_session_id, bus, channel, message, signal)


@mcp.tool(annotations=READ_ONLY)
def get_variable(dynamic_session_id: str, kind: str, name: str) -> dict:
    """按 kind 读取 System Variable 或 Environment Variable。"""
    return sessions.get_variable(dynamic_session_id, kind, name)


@mcp.tool(annotations=READ_ONLY)
def read_write_window(
    dynamic_session_id: str,
    line_limit: int = 50,
    char_limit: int = 12_000,
) -> dict:
    """读取有行数和字符数上限的 CANoe Write Window 尾部。"""
    return sessions.read_write_window(dynamic_session_id, line_limit, char_limit)


@mcp.tool(annotations=READ_ONLY)
def get_bus_statistics(dynamic_session_id: str, channel: int) -> dict:
    """读取当前 CAN Channel 底层实际提供的 Bus Statistics。"""
    return sessions.get_bus_statistics(dynamic_session_id, channel)


@mcp.tool(annotations=READ_ONLY)
def inspect_network_model(
    dynamic_session_id: str,
    kind: str,
    query: str = "",
    bus: str = "CAN",
    limit: int = 50,
) -> dict:
    """浏览当前 CANoe Configuration 的 Database、Node、Message 或 Signal。"""
    return sessions.inspect_network_model(dynamic_session_id, kind, query, bus, limit)


@mcp.tool(annotations=READ_ONLY)
def get_debug_snapshot(
    dynamic_session_id: str,
    signals: list[dict[str, Any]] | None = None,
    variables: list[dict[str, Any]] | None = None,
    write_line_limit: int = 50,
) -> dict:
    """在一致时间附近采集状态、Write Window 与明确指定的少量变量。"""
    return sessions.get_debug_snapshot(
        dynamic_session_id, signals, variables, write_line_limit
    )


@mcp.tool(annotations=RUNTIME_MUTATION)
def set_signal(
    dynamic_session_id: str,
    bus: str,
    channel: int,
    message: str,
    signal: str,
    value: float,
    raw_value: bool = False,
) -> dict:
    """在 Runtime Policy 允许时设置明确的 CAN/LIN Signal。"""
    return sessions.set_signal(
        dynamic_session_id, bus, channel, message, signal, value, raw_value
    )


@mcp.tool(annotations=RUNTIME_MUTATION)
def set_variable(
    dynamic_session_id: str,
    kind: str,
    name: str,
    value: Any,
) -> dict:
    """在 Runtime Policy 允许时设置 System/Environment Variable。"""
    return sessions.set_variable(dynamic_session_id, kind, name, value)


@mcp.tool(annotations=RUNTIME_MUTATION)
def call_capl_function(
    dynamic_session_id: str,
    name: str,
    arguments: list[Any] | None = None,
) -> dict:
    """调用 Configuration 中已存在且参数数量匹配的 CAPL Function。"""
    return sessions.call_capl_function(dynamic_session_id, name, arguments)


@mcp.tool(annotations=BUS_ACTIVE)
def transmit_can_frame(
    dynamic_session_id: str,
    channel: int,
    arbitration_id: int,
    data: str,
    is_extended: bool = False,
    is_fd: bool = False,
    brs: bool = False,
    count: int = 1,
    interval_ms: int = 0,
) -> dict:
    """通过明确的 CAPL Bridge 有界发送 Classic CAN/CAN FD Raw Frame。"""
    return sessions.transmit_can_frame(
        dynamic_session_id,
        channel,
        arbitration_id,
        data,
        is_extended,
        is_fd,
        brs,
        count,
        interval_ms,
    )


@mcp.tool(annotations=BUS_ACTIVE)
def send_diagnostic_request(
    dynamic_session_id: str,
    ecu_qualifier: str,
    request: str,
    timeout_seconds: float = 10,
) -> dict:
    """向明确 ECU 发送一条有限诊断请求；不生成额外诊断序列。"""
    return sessions.send_diagnostic_request(
        dynamic_session_id, ecu_qualifier, request, timeout_seconds
    )


@mcp.tool(annotations=LOCAL_MUTATION)
def configure_replay(
    dynamic_session_id: str,
    block_name: str,
    source_path: str,
) -> dict:
    """为明确 Replay Block 配置本地输入，不自动启动 Replay。"""
    return sessions.configure_replay(dynamic_session_id, block_name, source_path)


@mcp.tool(annotations=BUS_ACTIVE)
def control_replay(dynamic_session_id: str, block_name: str, action: str) -> dict:
    """对 Replay Block 执行 start、stop 或 reset；在线时可能影响真实总线。"""
    return sessions.control_replay(dynamic_session_id, block_name, action)


@mcp.tool(annotations=RUNTIME_MUTATION)
def manage_logging(
    dynamic_session_id: str,
    action: str,
    output_path: str | None = None,
    block_name: str | None = None,
) -> dict:
    """统一 list、configure、start 或 stop Logging Block。"""
    return sessions.manage_logging(dynamic_session_id, action, output_path, block_name)


@mcp.tool(annotations=READ_ONLY)
def list_tests(dynamic_session_id: str, limit: int = 50) -> dict:
    """有限列出 Test Environment 与其中的 Test Module。"""
    return sessions.list_tests(dynamic_session_id, limit)


@mcp.tool(annotations=RUNTIME_MUTATION)
def start_test(
    dynamic_session_id: str,
    scope: str,
    name: str | None = None,
) -> dict:
    """按 module、environment 或 all 范围启动测试。"""
    return sessions.start_test(dynamic_session_id, scope, name)


@mcp.tool(annotations=READ_ONLY)
def get_test_result(
    dynamic_session_id: str,
    name: str | None = None,
    limit: int = 50,
) -> dict:
    """有限读取测试 running/stopped、verdict 和 stop reason。"""
    return sessions.get_test_result(dynamic_session_id, name, limit)


if __name__ == "__main__":
    mcp.run()
