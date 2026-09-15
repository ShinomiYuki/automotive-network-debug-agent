"""验证 Dynamic MCP 的 26 个 Tool 名称和副作用注解。"""

import asyncio

from fastmcp import Client

from anda.mcp.dynamic.server import mcp

DYNAMIC_TOOLS = {
    "get_dynamic_health",
    "create_dynamic_session",
    "get_dynamic_status",
    "close_dynamic_session",
    "compile_capl",
    "control_measurement",
    "control_offline_animation",
    "wait_runtime_event",
    "observe_bus_window",
    "get_signal",
    "get_variable",
    "read_write_window",
    "get_bus_statistics",
    "inspect_network_model",
    "get_debug_snapshot",
    "set_signal",
    "set_variable",
    "call_capl_function",
    "transmit_can_frame",
    "send_diagnostic_request",
    "configure_replay",
    "control_replay",
    "manage_logging",
    "list_tests",
    "start_test",
    "get_test_result",
}
READ_ONLY_TOOLS = {
    "get_dynamic_health",
    "get_dynamic_status",
    "wait_runtime_event",
    "observe_bus_window",
    "get_signal",
    "get_variable",
    "read_write_window",
    "get_bus_statistics",
    "inspect_network_model",
    "get_debug_snapshot",
    "list_tests",
    "get_test_result",
}
BUS_ACTIVE_TOOLS = {
    "transmit_can_frame",
    "send_diagnostic_request",
    "control_replay",
}


def test_dynamic_mcp_contract_and_static_health():
    async def exercise_tools():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == DYNAMIC_TOOLS
            assert len(tools) == 26
            for tool in tools:
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is (
                    tool.name in READ_ONLY_TOOLS
                )
                assert tool.annotations.destructive_hint is (
                    tool.name in BUS_ACTIVE_TOOLS
                )
                assert tool.annotations.idempotent_hint is (
                    tool.name in READ_ONLY_TOOLS
                )
                assert tool.annotations.open_world_hint is (
                    tool.name in BUS_ACTIVE_TOOLS
                )

            health = await client.call_tool("get_dynamic_health", {})
            assert health.data["status"] == "available"
            assert health.data["stateful"] is True
            assert health.data["openai_api_required"] is False

    asyncio.run(exercise_tools())
