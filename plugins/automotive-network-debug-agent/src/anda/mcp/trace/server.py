"""
文件用途：
- 注册 Trace MCP Tool，并把调用直接转交给 TraceSessionManager。
- MCP 层只维护参数契约，不承载 BLF、DBC 或时序算法。

启动方式：
    python -m anda.mcp.trace.server
"""

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from anda.health import get_plugin_health
from anda.trace.session import TraceSessionManager

sessions = TraceSessionManager()
mcp = FastMCP("Automotive Trace MCP")

# 这组 Tool 只读取用户明确提供的本地 BLF/DBC/ARXML/LDF，并把解析结果保存在
# 当前 MCP 进程的临时内存中；它们不会修改输入文件、调用外部系统或产生不可逆副作用。
# 显式声明标准 MCP 注解后，Harness 可以在“仅写操作需审批”的策略下自动执行这些
# 只读调查动作，同时仍然对未来可能新增的写 Tool 保持审批保护。
TRACE_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def get_trace_health() -> dict:
    """验证 Trace MCP 当前响应、解析后端及插件清单状态。"""
    return get_plugin_health("trace")


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def load_trace(
    blf_path: str,
    database_path: str | None = None,
    database_paths: list[str] | None = None,
) -> dict:
    """启动 BLF 与可选 DBC/ARXML/LDF 后台索引，立即返回 trace_id。"""
    return sessions.start_load_trace(
        blf_path=blf_path,
        database_path=database_path,
        database_paths=database_paths,
    )


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def get_trace_load_status(trace_id: str, wait_seconds: float = 0) -> dict:
    """查询后台索引状态；可等待最多 55 秒，期间不得重复调用 load_trace。"""
    return sessions.get_load_status(trace_id, wait_seconds)


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def get_trace_summary(trace_id: str) -> dict:
    """返回日志时间范围、帧数、Channel、CAN ID 与 CAN FD 摘要。"""
    return sessions.get_summary(trace_id)


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def set_channel_mapping(trace_id: str, mappings: list[dict]) -> dict:
    """登记用户明确提供的分析仪 Channel、逻辑网段与 ECU Channel 映射。"""
    return sessions.set_channel_mapping(trace_id, mappings)


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def find_messages(
    trace_id: str,
    arbitration_id: int | None = None,
    channel: int | None = None,
    limit: int = 100,
    start_timestamp: float | None = None,
    end_timestamp: float | None = None,
    bus_type: str | None = None,
) -> dict:
    """按 CAN/LIN、帧 ID、1-based Channel 和时间范围查询有限帧。"""
    return sessions.find_messages(
        trace_id=trace_id,
        arbitration_id=arbitration_id,
        channel=channel,
        limit=limit,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        bus_type=bus_type,
    )


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def get_message_timing(
    trace_id: str,
    arbitration_id: int,
    channel: int | None = None,
    start_timestamp: float | None = None,
    end_timestamp: float | None = None,
    bus_type: str | None = None,
) -> dict:
    """返回指定 CAN/LIN 帧的客观周期、间隔和抖动统计。"""
    return sessions.get_message_timing(
        trace_id=trace_id,
        arbitration_id=arbitration_id,
        channel=channel,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        bus_type=bus_type,
    )


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def analyze_routed_signal_timeout(
    trace_id: str,
    source_channel: int,
    source_message: str,
    target_channels: list[int],
    target_frame: str,
    signal_name: str,
    timeout_value: float | str | bool,
    configured_timeout_ms: float,
    source_bus_type: str = "can",
    target_bus_type: str = "lin",
    source_database_file: str | None = None,
    target_database_files: dict[str, str] | None = None,
) -> dict:
    """关联源报文停止与各目标 Channel 的 timeout 值首次出现时刻。"""
    return sessions.analyze_routed_signal_timeout(
        trace_id=trace_id,
        source_channel=source_channel,
        source_message=source_message,
        target_channels=target_channels,
        target_frame=target_frame,
        signal_name=signal_name,
        timeout_value=timeout_value,
        configured_timeout_ms=configured_timeout_ms,
        source_bus_type=source_bus_type,
        target_bus_type=target_bus_type,
        source_database_file=source_database_file,
        target_database_files=target_database_files,
    )


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def search_database(trace_id: str, query: str, limit: int = 20) -> dict:
    """按报文名或信号名搜索有限 DBC/ARXML 候选，供后续精确查询。"""
    return sessions.search_database(trace_id=trace_id, query=query, limit=limit)


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def decode_signal(
    trace_id: str,
    arbitration_id: int,
    signal_name: str,
    channel: int | None = None,
    limit: int = 100,
    start_timestamp: float | None = None,
    end_timestamp: float | None = None,
    bus_type: str | None = None,
    database_file: str | None = None,
) -> dict:
    """使用已加载 DBC/ARXML/LDF 解码指定 CAN/LIN 信号并限制样本数。"""
    return sessions.decode_signal(
        trace_id=trace_id,
        arbitration_id=arbitration_id,
        signal_name=signal_name,
        channel=channel,
        limit=limit,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        bus_type=bus_type,
        database_file=database_file,
    )


if __name__ == "__main__":
    mcp.run()
