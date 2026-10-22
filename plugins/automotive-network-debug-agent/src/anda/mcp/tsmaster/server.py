"""TSMaster 工程、受限总线执行及测量结果工具。"""

from contextlib import asynccontextmanager

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from anda.tsmaster.analysis import analyze_routes
from anda.tsmaster.session import TSMasterSessions

sessions = TSMasterSessions()


@asynccontextmanager
async def lifespan(_server):
    try:
        yield
    finally:
        sessions.shutdown()


mcp = FastMCP("Automotive TSMaster MCP", lifespan=lifespan)
READ = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
LOCAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
BUS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


@mcp.tool(annotations=READ)
def get_tsmaster_health() -> dict:
    """只读检查 Python 位数、COM 注册及既有进程，不启动 TSMaster。"""
    return sessions.get_health()


@mcp.tool(annotations=LOCAL)
def create_tsmaster_session(
    workspace: str,
    project_name: str,
    bench: dict,
    application_authorized: bool = False,
    template: str | None = None,
    template_transmitters_reviewed: bool = False,
) -> dict:
    """创建独立工程并配置显式台架；授权仅允许启动应用，不允许连接或发送。"""
    return sessions.create(
        workspace,
        project_name,
        bench,
        application_authorized,
        template,
        template_transmitters_reviewed,
    )


@mcp.tool(annotations=READ)
def get_tsmaster_status(session_id: str) -> dict:
    """读取异步操作状态、有限接收反馈和结果摘要，不跨线程调用 COM。"""
    return sessions.status(session_id)


@mcp.tool(annotations=BUS)
def run_tsmaster_test(
    session_id: str,
    cases: list[dict],
    baseline_seconds: float,
    response_seconds: float,
    timeout_seconds: float,
    evidence_context: dict,
    frames: list[dict] | None = None,
    replay: dict | None = None,
    bus_authorized: bool = False,
) -> dict:
    """明确授权后异步连接、先录制再有限发送或回放，停止写入后自动检查完整 BLF。"""
    return sessions.run(
        session_id,
        cases,
        frames,
        replay,
        baseline_seconds,
        response_seconds,
        timeout_seconds,
        evidence_context,
        bus_authorized,
    )


@mcp.tool(annotations=LOCAL)
def stop_tsmaster_test(session_id: str) -> dict:
    """请求停止本次刺激；查询状态确认停止、记录关闭及断开，超时不伪报已停止。"""
    return sessions.stop(session_id)


@mcp.tool(annotations=LOCAL)
def close_tsmaster_session(session_id: str) -> dict:
    """清理并释放本会话的 COM 和事件；仅向唯一自有应用请求正常关闭。"""
    return sessions.close(session_id)


@mcp.tool(annotations=READ)
def analyze_tsmaster_log(
    blf_path: str,
    cases: list[dict],
    evidence_context: dict,
    database_bindings: list[dict] | None = None,
) -> dict:
    """离线扫描完整、已关闭的测量 BLF；方向和采集前提不明时只返回证据不足。"""
    return analyze_routes(blf_path, cases, evidence_context, database_bindings)


@mcp.tool(annotations=READ)
def get_tsmaster_result(session_id: str, offset: int = 0, limit: int = 10) -> dict:
    """读取已完成结果，按用例分页限制返回量；完整报告保存在本次运行目录。"""
    return sessions.result(session_id, offset, limit)


if __name__ == "__main__":
    mcp.run()
