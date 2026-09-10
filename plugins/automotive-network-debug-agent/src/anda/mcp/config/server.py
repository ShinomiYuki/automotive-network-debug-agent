"""
文件用途：
- 注册只读 Config MCP Tool，并把调用转交给 ConfigWorkspaceManager。
- MCP 层只定义参数契约；XML、关系和源码索引逻辑全部位于 Config Core。

启动方式：
    python -m anda.mcp.config.server
"""

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from anda.config.workspace import ConfigWorkspaceManager

workspaces = ConfigWorkspaceManager()
mcp = FastMCP("Automotive Config MCP")

# 本组 Tool 只读取用户明确指定的工程目录，并在 MCP 进程内建立临时内存索引。
# 它们不修改 ARXML/源码、不生成配置、不调用外部系统，因此统一声明为只读、
# 非破坏、幂等和封闭世界；未来若增加写入 Tool，不得复用这组注解。
CONFIG_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def load_config_workspace(
    root_path: str,
    arxml_paths: list[str] | None = None,
    force_reload: bool = False,
) -> dict:
    """启动工程及明确 ARXML 的后台索引，立即返回 workspace_id。"""
    return workspaces.start_load_config_workspace(root_path, arxml_paths, force_reload)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def get_config_load_status(workspace_id: str, wait_seconds: float = 0) -> dict:
    """查询后台索引状态；可等待最多 55 秒，期间不得重复加载。"""
    return workspaces.get_load_status(workspace_id, wait_seconds)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def search_config_symbol(workspace_id: str, query: str, limit: int = 20) -> dict:
    """按 Message、CAN ID、PDU、配置对象或源码标识符查询有限候选。"""
    return workspaces.search_config_symbol(workspace_id, query, limit)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def search_source_symbol(workspace_id: str, query: str, limit: int = 20) -> dict:
    """按完整名、前缀或子串搜索工程源码符号，并返回角色与位置摘要。"""
    return workspaces.search_source_symbol(workspace_id, query, limit)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def inspect_source_symbol(
    workspace_id: str,
    symbol: str,
    limit: int = 20,
    context_lines: int = 2,
) -> dict:
    """以完整源码标识符为入口，查看定义/引用、上下文和同名配置证据。"""
    return workspaces.inspect_source_symbol(
        workspace_id, symbol, limit=limit, context_lines=context_lines
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def trace_message_route(
    workspace_id: str,
    arbitration_id: int | None = None,
    message_name: str | None = None,
    source_network: str | None = None,
    destination_network: str | None = None,
) -> dict:
    """追踪 Message/CAN ID 到 Source PDU、PduR Path 与 Destination PDU。"""
    return workspaces.trace_message_route(
        workspace_id=workspace_id,
        arbitration_id=arbitration_id,
        message_name=message_name,
        source_network=source_network,
        destination_network=destination_network,
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def inspect_pdu(workspace_id: str, pdu_name: str) -> dict:
    """查看一个 PDU 的定义、网段、Message 和 Routing Path 关联。"""
    return workspaces.inspect_pdu(workspace_id, pdu_name)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def inspect_communication(
    workspace_id: str,
    arbitration_id: int | None = None,
    message_name: str | None = None,
    pdu_name: str | None = None,
    network: str | None = None,
) -> dict:
    """查看 Message/PDU 的 CanIf、Com、周期、模式、超时和源码证据。"""
    return workspaces.inspect_communication(
        workspace_id,
        arbitration_id=arbitration_id,
        message_name=message_name,
        pdu_name=pdu_name,
        network=network,
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def trace_signal_gateway(workspace_id: str, signal_name: str, limit: int = 20) -> dict:
    """追踪 Com Signal、所在 I-PDU 与 ComGwMapping Source/Destination。"""
    return workspaces.trace_signal_gateway(workspace_id, signal_name, limit)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def inspect_ipdu_group(workspace_id: str, group_name: str) -> dict:
    """查看 I-PDU Group 成员及 BswM/ComM 的直接启停配置引用。"""
    return workspaces.inspect_ipdu_group(workspace_id, group_name)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def find_source_context(
    workspace_id: str,
    symbol: str,
    limit: int = 20,
    context_lines: int = 2,
) -> dict:
    """定位生成代码或源码中的完整标识符，并返回受限上下文。"""
    return workspaces.find_source_context(
        workspace_id, symbol, limit=limit, context_lines=context_lines
    )


if __name__ == "__main__":
    mcp.run()
