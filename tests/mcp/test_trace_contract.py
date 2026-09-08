"""通过进程内 FastMCP Client 验证 Tool 可调用、结构、错误和输出上限。"""

import asyncio

from fastmcp import Client

from anda.mcp.trace.server import mcp
from anda.trace.session import MAX_FRAME_RESULTS


def test_trace_mcp_contract_and_session_reuse(trace_files):
    blf_path, dbc_path = trace_files

    async def exercise_tools():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == {
                "load_trace",
                "get_trace_summary",
                "find_messages",
                "get_message_timing",
                "search_database",
                "decode_signal",
            }
            for tool in tools:
                # 所有 Trace Tool 均只读取本地证据；这些断言防止以后新增或重构
                # 装饰器时意外丢失注解，导致非交互 Harness 再次要求审批。
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.idempotent_hint is True
                assert tool.annotations.open_world_hint is False

            first = await client.call_tool(
                "load_trace",
                {"blf_path": str(blf_path), "database_path": str(dbc_path)},
            )
            second = await client.call_tool(
                "load_trace",
                {"blf_path": str(blf_path), "database_path": str(dbc_path)},
            )
            assert first.data["trace_id"] == second.data["trace_id"]
            assert second.data["reused"] is True
            trace_id = first.data["trace_id"]

            summary = await client.call_tool(
                "get_trace_summary", {"trace_id": trace_id}
            )
            messages = await client.call_tool(
                "find_messages",
                {"trace_id": trace_id, "arbitration_id": 0x100, "limit": 50_000},
            )
            timing = await client.call_tool(
                "get_message_timing",
                {"trace_id": trace_id, "arbitration_id": 0x100, "channel": 1},
            )
            database_match = await client.call_tool(
                "search_database",
                {"trace_id": trace_id, "query": "VehicleSpeed"},
            )
            signal = await client.call_tool(
                "decode_signal",
                {
                    "trace_id": trace_id,
                    "arbitration_id": 0x100,
                    "signal_name": "VehicleSpeed",
                    "limit": 2,
                },
            )

            assert summary.data["frame_count"] == 225
            assert messages.data["returned_count"] == MAX_FRAME_RESULTS
            assert len(messages.data["frames"]) == MAX_FRAME_RESULTS
            assert timing.data["period_median_ms"] is not None
            assert database_match.data["matches"][0]["arbitration_id"] == 0x100
            assert database_match.data["matches"][0]["signal_name_exact"] is True
            assert signal.data["returned_count"] == 2

    asyncio.run(exercise_tools())


def test_trace_mcp_returns_concise_error_for_unknown_trace():
    async def exercise_error():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "get_trace_summary",
                {"trace_id": "missing"},
                raise_on_error=False,
            )
            assert result.is_error is True
            assert "未知 trace_id: missing" in str(result.content)

    asyncio.run(exercise_error())
