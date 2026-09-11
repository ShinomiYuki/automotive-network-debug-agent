"""
文件用途：
- 提供无需加载用户工程或 BLF 的插件后端健康检查。
- 区分“当前 MCP 已实际响应”与“另一个 MCP 仅在插件清单中配置”。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

TRACE_TOOL_NAMES = [
    "get_trace_health",
    "load_trace",
    "get_trace_load_status",
    "get_trace_summary",
    "set_channel_mapping",
    "find_messages",
    "get_message_timing",
    "analyze_routed_signal_timeout",
    "search_database",
    "decode_signal",
]
CONFIG_TOOL_NAMES = [
    "get_config_health",
    "load_config_workspace",
    "get_config_load_status",
    "get_project_index_status",
    "plan_source_search",
    "search_source_evidence",
    "read_source_lines",
    "trace_autosar_runtime_chain",
    "create_investigation_bundle",
    "record_investigation_evidence",
    "update_investigation_state",
    "get_investigation_summary",
    "export_investigation_markdown",
]


def get_plugin_health(current_mcp: str) -> dict:
    """返回确定性依赖和清单状态；不伪装验证另一个独立 MCP 进程。"""
    plugin_root = Path(__file__).resolve().parents[2]
    configured_servers = _configured_servers(plugin_root / ".mcp.json")
    current = current_mcp.strip().casefold()
    trace_status = (
        "available"
        if current == "trace"
        else "configured_not_runtime_verified"
        if "automotive-trace" in configured_servers
        else "unavailable"
    )
    config_status = (
        "available"
        if current == "config"
        else "configured_not_runtime_verified"
        if "automotive-config" in configured_servers
        else "unavailable"
    )
    return {
        "current_mcp": current,
        "trace_mcp": {
            "status": trace_status,
            "configured": "automotive-trace" in configured_servers,
            "core_tools": TRACE_TOOL_NAMES,
        },
        "config_mcp": {
            "status": config_status,
            "configured": "automotive-config" in configured_servers,
            "core_tools": CONFIG_TOOL_NAMES,
        },
        "blf_parser_backend": _module_status("vblf"),
        "dbc_decoder": _module_status("cantools"),
        "ldf_decoder": _module_status("ldfparser"),
        "arxml_parser": {
            "status": "available",
            "backend": "xml.etree.ElementTree",
        },
        "project_index": _module_status("sqlite3"),
        "report_exporter": {
            "status": "available",
            "formats": ["json", "markdown"],
            "pdf": "unavailable",
        },
        "registration_note": (
            "只有当前 Tool 所属 MCP 的 available 是运行时实测；"
            "configured_not_runtime_verified 仍需 Harness 工具目录确认。若 Skill 可见但"
            "健康 Tool 不可调用，请先检查插件安装缓存版本、MCP 注册与启动错误，并在"
            "升级后新建会话。"
        ),
        "openai_api_required": False,
    }


def _module_status(module: str) -> dict:
    available = importlib.util.find_spec(module) is not None
    return {
        "status": "available" if available else "unavailable",
        "backend": module,
    }


def _configured_servers(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    servers = payload.get("mcpServers", {})
    return set(servers) if isinstance(servers, dict) else set()
