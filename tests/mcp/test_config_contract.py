"""通过进程内 FastMCP Client 验证 Config Tool 契约和真实调用链。"""

import asyncio
from pathlib import Path

from fastmcp import Client

from anda.mcp.config.server import mcp

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "config"
CONFIG_TOOLS = {
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
}
WRITE_TOOLS = {
    "create_investigation_bundle",
    "record_investigation_evidence",
    "update_investigation_state",
    "export_investigation_markdown",
}


def test_config_mcp_contract_annotations_and_calls():
    async def exercise_tools():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == CONFIG_TOOLS
            for tool in tools:
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is (tool.name not in WRITE_TOOLS)
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.idempotent_hint is (tool.name not in WRITE_TOOLS)
                assert tool.annotations.open_world_hint is False

            health = await client.call_tool("get_config_health", {})
            assert health.data["config_mcp"]["status"] == "available"
            assert health.data["trace_mcp"]["status"] == (
                "configured_not_runtime_verified"
            )
            assert health.data["report_exporter"]["formats"] == ["json", "markdown"]

            loaded = await client.call_tool(
                "load_config_workspace", {"root_path": str(FIXTURE_ROOT)}
            )
            workspace_id = loaded.data["workspace_id"]
            status = await client.call_tool(
                "get_config_load_status",
                {"workspace_id": workspace_id, "wait_seconds": 5},
            )
            assert status.data["status"] == "ready"
            route = await client.call_tool(
                "trace_message_route",
                {"workspace_id": workspace_id, "arbitration_id": 0x416},
            )
            source = await client.call_tool(
                "find_source_context",
                {
                    "workspace_id": workspace_id,
                    "symbol": "PduRRoutingPath_416_SU",
                },
            )
            source_inspection = await client.call_tool(
                "inspect_source_symbol",
                {
                    "workspace_id": workspace_id,
                    "symbol": "VehicleSpeed_Rx",
                },
            )
            communication = await client.call_tool(
                "inspect_communication",
                {
                    "workspace_id": workspace_id,
                    "arbitration_id": 0x534,
                    "network": "Network_BD",
                },
            )
            signal = await client.call_tool(
                "trace_signal_gateway",
                {"workspace_id": workspace_id, "signal_name": "VehicleSpeed"},
            )
            group = await client.call_tool(
                "inspect_ipdu_group",
                {"workspace_id": workspace_id, "group_name": "TxGroup"},
            )
            plan = await client.call_tool(
                "plan_source_search",
                {
                    "workspace_id": workspace_id,
                    "question_type": "can_to_lin_timeout",
                    "identifiers": ["VehicleSpeed", "PduRRoutingPath_416_SU"],
                },
            )
            evidence = await client.call_tool(
                "search_source_evidence",
                {
                    "workspace_id": workspace_id,
                    "identifiers": ["VehicleSpeed_Rx", "does_not_exist"],
                    "modules": ["Com"],
                },
            )
            index_status = await client.call_tool(
                "get_project_index_status", {"workspace_id": workspace_id}
            )

            assert status.data["index_ready"] is True
            assert route.data["route_found"] is True
            assert len(route.data["routing_paths"][0]["destinations"]) == 2
            assert source.data["returned_count"] == 3
            assert source_inspection.data["config_match_count"] >= 1
            assert communication.data["message"]["is_fd"] is True
            assert signal.data["gateway_found"] is True
            assert group.data["direct_control_reference_count"] == 2
            assert sum(
                stage["candidate_file_count"] for stage in plan.data["stages"]
            ) >= 1
            assert "does_not_exist" in evidence.data["not_found"]
            assert index_status.data["backend"] == "sqlite"

    asyncio.run(exercise_tools())


def test_config_mcp_investigation_bundle_round_trip(tmp_path):
    async def exercise_bundle():
        async with Client(mcp) as client:
            created = await client.call_tool(
                "create_investigation_bundle",
                {
                    "output_directory": str(tmp_path),
                    "case_id": "timeout-case",
                    "case_data": {"title": "CAN to LIN timeout"},
                    "channel_mapping": [
                        {
                            "analysis_channel": 8,
                            "logical_network": "LIN8",
                            "bus_type": "lin",
                            "mapping_source": "user",
                        }
                    ],
                },
            )
            bundle_path = created.data["bundle_path"]
            recorded = await client.call_tool(
                "record_investigation_evidence",
                {
                    "bundle_path": bundle_path,
                    "category": "counter_evidence",
                    "evidence": {"candidate": "CDD", "result": "NOT_FOUND_IN_SCOPE"},
                },
            )
            summary = await client.call_tool(
                "get_investigation_summary", {"bundle_path": bundle_path}
            )
            exported = await client.call_tool(
                "export_investigation_markdown", {"bundle_path": bundle_path}
            )

            assert recorded.data["total_count"] == 1
            assert summary.data["evidence"]["channel_mapping"]["count"] == 1
            assert summary.data["evidence"]["counter_evidence"]["count"] == 1
            assert Path(exported.data["report_path"]).is_file()

    asyncio.run(exercise_bundle())


def test_config_mcp_returns_concise_error_for_unknown_workspace():
    async def exercise_error():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "search_config_symbol",
                {"workspace_id": "missing", "query": "0x416"},
                raise_on_error=False,
            )
            assert result.is_error is True
            assert "未知 workspace_id：missing" in str(result.content)

    asyncio.run(exercise_error())
