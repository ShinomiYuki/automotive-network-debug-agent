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


def test_plugin_launcher_exposes_trace_tools(tmp_path):
    async def exercise_launcher():
        mcp_manifest = json.loads(
            (PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8")
        )
        server = mcp_manifest["mcpServers"]["automotive-trace"]
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
            assert {tool.name for tool in tools} == {
                "load_trace",
                "get_trace_summary",
                "find_messages",
                "get_message_timing",
                "search_database",
                "decode_signal",
            }

    asyncio.run(exercise_launcher())
