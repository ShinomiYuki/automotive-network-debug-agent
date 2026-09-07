"""验证 Codex 项目配置使用的 PowerShell 启动器可建立真实 stdio MCP 连接。"""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[2]


def test_project_launcher_exposes_trace_tools(tmp_path):
    async def exercise_launcher():
        environment = os.environ.copy()
        environment.update(
            {
                "ANDA_PYTHON": sys.executable,
                "TEMP": str(tmp_path),
                "TMP": str(tmp_path),
            }
        )
        transport = StdioTransport(
            command="pwsh",
            args=[
                "-NoLogo",
                "-NoProfile",
                "-File",
                "scripts/run_trace_mcp.ps1",
            ],
            env=environment,
            cwd=str(ROOT),
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
