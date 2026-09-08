"""
文件用途：
- 注册 Trace MCP Tool，并把调用直接转交给 TraceSessionManager。
- MCP 层只维护参数契约，不承载 BLF、DBC 或时序算法。

启动方式：
    python -m anda.mcp.trace.server
"""

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from anda.trace.session import TraceSessionManager

sessions = TraceSessionManager()
mcp = FastMCP("Automotive Trace MCP")

# 这 6 个 Tool 只读取用户明确提供的本地 BLF/DBC/ARXML，并把解析结果保存在
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
def load_trace(blf_path: str, database_path: str | None = None) -> dict:
    """加载 BLF 及可选 DBC/ARXML，返回可复用的 trace_id。"""
    return sessions.load_trace(blf_path=blf_path, database_path=database_path)


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def get_trace_summary(trace_id: str) -> dict:
    """返回日志时间范围、帧数、Channel、CAN ID 与 CAN FD 摘要。"""
    return sessions.get_summary(trace_id)


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def find_messages(
    trace_id: str,
    arbitration_id: int | None = None,
    channel: int | None = None,
    limit: int = 100,
    start_timestamp: float | None = None,
    end_timestamp: float | None = None,
) -> dict:
    """按 CAN ID、1-based Channel 和可选绝对时间范围查询有限数量的帧。"""
    return sessions.find_messages(
        trace_id=trace_id,
        arbitration_id=arbitration_id,
        channel=channel,
        limit=limit,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )


@mcp.tool(annotations=TRACE_READ_ONLY_ANNOTATIONS)
def get_message_timing(
    trace_id: str,
    arbitration_id: int,
    channel: int | None = None,
    start_timestamp: float | None = None,
    end_timestamp: float | None = None,
) -> dict:
    """返回指定报文的客观周期、间隔和抖动统计。"""
    return sessions.get_message_timing(
        trace_id=trace_id,
        arbitration_id=arbitration_id,
        channel=channel,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
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
) -> dict:
    """使用会话已加载的数据库解码指定信号，并限制返回样本数。"""
    return sessions.decode_signal(
        trace_id=trace_id,
        arbitration_id=arbitration_id,
        signal_name=signal_name,
        channel=channel,
        limit=limit,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )


if __name__ == "__main__":
    mcp.run()
