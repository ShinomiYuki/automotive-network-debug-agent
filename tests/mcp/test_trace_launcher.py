"""验证 Codex 项目配置使用的 PowerShell 启动器可建立真实 stdio MCP 连接。"""

import asyncio
import json
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins" / "automotive-network-debug-agent"


EXPECTED_TOOLS = {
    "automotive-trace": {
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
    },
    "automotive-config": {
        "get_config_health",
        "create_investigation_bundle",
        "record_investigation_evidence",
        "update_investigation_state",
        "get_investigation_summary",
        "export_investigation_markdown",
        "load_config_workspace",
        "get_config_load_status",
        "search_config_symbol",
        "search_source_symbol",
        "plan_source_search",
        "search_source_evidence",
        "read_source_lines",
        "get_project_index_status",
        "inspect_source_symbol",
        "trace_message_route",
        "inspect_pdu",
        "inspect_communication",
        "trace_signal_gateway",
        "inspect_ipdu_group",
        "trace_autosar_runtime_chain",
        "find_source_context",
    },
}


def test_plugin_launchers_expose_independent_tool_sets(tmp_path):
    async def exercise_launcher():
        mcp_manifest = json.loads(
            (PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8")
        )
        for server_name, expected_tools in EXPECTED_TOOLS.items():
            server = mcp_manifest["mcpServers"][server_name]
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.update(
                {
                    "ANDA_PYTHON": sys.executable,
                    "TEMP": str(tmp_path),
                    "TMP": str(tmp_path),
                }
            )
            transport = StdioTransport(
                command=server["command"],
                args=server["args"],
                env=environment,
                cwd=str(PLUGIN_ROOT),
            )
            async with Client(transport) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools} == expected_tools

    asyncio.run(exercise_launcher())
