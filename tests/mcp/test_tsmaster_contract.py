"""验证独立工具、总线授权默认关闭及只读健康检查。"""

import asyncio

from fastmcp import Client

from anda.mcp.tsmaster.server import mcp

TOOLS = {
    "get_tsmaster_health",
    "create_tsmaster_session",
    "get_tsmaster_status",
    "run_tsmaster_test",
    "stop_tsmaster_test",
    "close_tsmaster_session",
    "analyze_tsmaster_log",
    "get_tsmaster_result",
}


def test_tsmaster_tools_annotations_and_authorization_defaults():
    async def check():
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools) == TOOLS
            assert tools["get_tsmaster_health"].annotations.read_only_hint
            assert tools["analyze_tsmaster_log"].annotations.read_only_hint
            assert tools["run_tsmaster_test"].annotations.destructive_hint
            assert tools["run_tsmaster_test"].annotations.open_world_hint
            assert (
                tools["run_tsmaster_test"].input_schema["properties"]["bus_authorized"][
                    "default"
                ]
                is False
            )
            assert (
                tools["create_tsmaster_session"].input_schema["properties"][
                    "application_authorized"
                ]["default"]
                is False
            )
            result = await client.call_tool("get_tsmaster_health", {})
            assert not result.is_error

    asyncio.run(check())
