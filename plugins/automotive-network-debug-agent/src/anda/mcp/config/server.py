"""
文件用途：
- 注册 Config MCP Tool，并把调用转交给 ConfigWorkspaceManager 或调查证据包模块。
- MCP 层只定义参数契约；XML、关系和源码索引逻辑全部位于 Config Core。

启动方式：
    python -m anda.mcp.config.server
"""

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from anda.config.workspace import ConfigWorkspaceManager
from anda.health import get_plugin_health
from anda.investigation import bundle

workspaces = ConfigWorkspaceManager()
mcp = FastMCP("Automotive Config MCP")

# 调查 Tool 只读取用户明确指定的工程目录；SQLite 只保存外部缓存索引。
# 证据包 Tool 只在用户指定的输出目录写入 JSON/Markdown，使用单独的写入注解。
CONFIG_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
CONFIG_WRITE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def get_config_health() -> dict:
    """验证 Config MCP 当前响应、索引后端及插件清单状态。"""
    return get_plugin_health("config")


@mcp.tool(annotations=CONFIG_WRITE_ANNOTATIONS)
def create_investigation_bundle(
    output_directory: str,
    case_id: str,
    case_data: dict,
    channel_mapping: list[dict] | None = None,
) -> dict:
    """在用户明确目录创建案件、证据、假设和开放问题结构。"""
    return bundle.create_investigation_bundle(
        output_directory, case_id, case_data, channel_mapping
    )


@mcp.tool(annotations=CONFIG_WRITE_ANNOTATIONS)
def record_investigation_evidence(
    bundle_path: str,
    category: str,
    evidence: dict | list[dict],
    replace: bool = False,
) -> dict:
    """向结构化调查包追加或替换一类已取得证据。"""
    return bundle.record_investigation_evidence(
        bundle_path, category, evidence, replace
    )


@mcp.tool(annotations=CONFIG_WRITE_ANNOTATIONS)
def update_investigation_state(
    bundle_path: str,
    hypotheses: list[dict] | None = None,
    open_questions: list[dict] | None = None,
) -> dict:
    """更新带反证状态的候选假设与开放问题快照。"""
    return bundle.update_investigation_state(
        bundle_path, hypotheses, open_questions
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def get_investigation_summary(bundle_path: str) -> dict:
    """读取调查包的数量与有限样本，避免倾倒完整证据文件。"""
    return bundle.get_investigation_summary(bundle_path)


@mcp.tool(annotations=CONFIG_WRITE_ANNOTATIONS)
def export_investigation_markdown(
    bundle_path: str, title: str | None = None
) -> dict:
    """把已记录内容确定性导出为 Markdown，不自动补充根因。"""
    return bundle.export_investigation_markdown(bundle_path, title)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def load_config_workspace(
    root_path: str,
    arxml_paths: list[str] | None = None,
    force_reload: bool = False,
    cache_directory: str | None = None,
) -> dict:
    """启动工程及明确 ARXML 的后台索引，立即返回 workspace_id。"""
    return workspaces.start_load_config_workspace(
        root_path, arxml_paths, force_reload, cache_directory
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def get_config_load_status(workspace_id: str, wait_seconds: float = 0) -> dict:
    """查询后台索引状态；可等待最多 55 秒，期间不得重复加载。"""
    return workspaces.get_load_status(workspace_id, wait_seconds)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def search_config_symbol(workspace_id: str, query: str, limit: int = 20) -> dict:
    """按 Message、CAN ID、PDU、配置对象或源码标识符查询有限候选。"""
    return workspaces.search_config_symbol(workspace_id, query, limit)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def search_source_symbol(
    workspace_id: str,
    query: str,
    limit: int = 20,
    modules: list[str] | None = None,
) -> dict:
    """按完整名、前缀或子串搜索工程源码符号，并返回角色与位置摘要。"""
    return workspaces.search_source_symbol(workspace_id, query, limit, modules)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def plan_source_search(
    workspace_id: str, question_type: str, identifiers: list[str]
) -> dict:
    """按问题类型从一次性文件清单规划最小模块与候选文件范围。"""
    return workspaces.plan_source_search(workspace_id, question_type, identifiers)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def search_source_evidence(
    workspace_id: str,
    identifiers: list[str],
    modules: list[str] | None = None,
    include_generated: bool = True,
    include_source: bool = True,
    limit_per_identifier: int = 20,
    max_chars_per_match: int = 1000,
) -> dict:
    """一次查询多个完整标识符，返回有界证据及可审计未命中范围。"""
    return workspaces.search_source_evidence(
        workspace_id,
        identifiers,
        modules,
        include_generated,
        include_source,
        limit_per_identifier,
        max_chars_per_match,
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def read_source_lines(
    workspace_id: str,
    path: str,
    ranges: list[list[int]],
    max_chars_per_line: int = 1000,
) -> dict:
    """按 SQLite 行偏移一次读取多个明确行范围，不整文件加载。"""
    return workspaces.read_source_lines(
        workspace_id, path, ranges, max_chars_per_line
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def get_project_index_status(workspace_id: str) -> dict:
    """返回项目索引缓存、增量复用和失效依据。"""
    return workspaces.get_project_index_status(workspace_id)


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def inspect_source_symbol(
    workspace_id: str,
    symbol: str,
    limit: int = 20,
    context_lines: int = 0,
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
def trace_autosar_runtime_chain(
    workspace_id: str,
    signal_name: str,
    additional_identifiers: list[str] | None = None,
) -> dict:
    """输出信号跨 CanIf/PduR/Com/LinIf/OS/BswM/CDD 的静态证据链。"""
    return workspaces.trace_autosar_runtime_chain(
        workspace_id, signal_name, additional_identifiers
    )


@mcp.tool(annotations=CONFIG_READ_ONLY_ANNOTATIONS)
def find_source_context(
    workspace_id: str,
    symbol: str,
    limit: int = 20,
    context_lines: int = 0,
) -> dict:
    """定位生成代码或源码中的完整标识符，并返回受限上下文。"""
    return workspaces.find_source_context(
        workspace_id, symbol, limit=limit, context_lines=context_lines
    )


if __name__ == "__main__":
    mcp.run()
