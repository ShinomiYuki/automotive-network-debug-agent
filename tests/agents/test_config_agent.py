"""确定性验证 Config Skill、调查 Case、Plugin 路径与 MCP 契约的一致性。"""

import json
from pathlib import Path

import pytest

from anda.common.errors import ConfigConflictError
from anda.config.workspace import ConfigWorkspaceManager

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins" / "automotive-network-debug-agent"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "config-analysis"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
OPENAI_YAML_PATH = SKILL_ROOT / "agents" / "openai.yaml"
PLUGIN_MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
EVAL_PATH = ROOT / "evals" / "config_agent_cases.json"
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
OUTPUT_SECTIONS = ["配置判断", "关键证据", "不确定项", "下一步建议"]


def test_config_skill_is_packaged_and_has_discriminating_invocation_metadata():
    content = SKILL_PATH.read_text(encoding="utf-8")
    interface = OPENAI_YAML_PATH.read_text(encoding="utf-8")
    plugin = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert content.startswith("---\nname: config-analysis\n")
    frontmatter = content.split("---", 2)[1]
    assert "工程路径" in frontmatter
    assert "不用于 BLF" in frontmatter
    assert 'display_name: "Config Analysis"' in interface
    assert "$config-analysis" in interface
    assert "allow_implicit_invocation: true" in interface
    assert plugin["skills"] == "./skills/"
    assert {path.name for path in (PLUGIN_ROOT / "skills").iterdir()} == {
        "trace-analysis",
        "config-analysis",
        "debug-analysis",
    }
    assert not (ROOT / ".agents" / "skills" / "config-analysis").exists()
    assert not (ROOT / "skills" / "config-analysis").exists()
    assert not (PLUGIN_ROOT / "src" / "anda" / "agents" / "config_agent.py").exists()


def test_config_skill_covers_tools_minimal_routing_and_evidence_boundaries():
    content = SKILL_PATH.read_text(encoding="utf-8")

    for tool in CONFIG_TOOLS:
        assert f"`{tool}`" in content
    heading_positions = [content.index(f"## {section}") for section in OUTPUT_SECTIONS]
    assert heading_positions == sorted(heading_positions)
    assert "每次调查先调用一次 `load_config_workspace`" in content
    assert "不重复加载" in content
    assert "已有结果包含所需事实时立即停止" in content
    assert "不要调用全部 Tool" in content
    assert "完整标识符一致" in content
    assert "不得自行选择" in content
    assert "不得模拟规则执行顺序" in content
    assert "不调用 `automotive-trace`" in content
    assert "不输出 Python traceback" in content
    assert "不重复加载同一输入" in content
    assert "`plan_source_search`" in content
    assert "`search_source_evidence`" in content
    assert "`read_source_lines`" in content
    assert "`trace_autosar_runtime_chain`" in content
    assert "NOT_FOUND_IN_SCOPE" in content


def test_config_eval_cases_use_only_existing_tools_and_minimal_loads():
    specification = json.loads(EVAL_PATH.read_text(encoding="utf-8"))

    assert specification["required_output_sections"] == OUTPUT_SECTIONS
    assert {case["id"] for case in specification["cases"]} == {
        "route_exists",
        "destination_missing",
        "route_absent",
        "signal_gateway",
        "communication_type",
        "period_and_timeout",
        "ipdu_group_controls",
        "source_symbol",
        "insufficient_runtime_evidence",
        "ambiguous_message",
        "large_generated_source_batch",
        "autosar_runtime_chain",
    }
    for case in specification["cases"]:
        plan = case["expected_tool_plan"]
        forbidden = case["forbidden_tools"]
        assert set(plan) <= CONFIG_TOOLS
        assert set(forbidden) <= CONFIG_TOOLS
        assert not (set(plan) & set(forbidden))
        assert plan[0] == "load_config_workspace"
        assert plan.count("load_config_workspace") == 1
        assert plan[1] == "get_config_load_status"
        assert plan.count("get_config_load_status") == 1
        assert len(plan) <= 4


def test_fixture_supports_config_agent_route_gateway_and_attribute_cases():
    manager = ConfigWorkspaceManager()
    loaded = manager.load_config_workspace(str(FIXTURE_ROOT))
    workspace_id = loaded["workspace_id"]

    route = manager.trace_message_route(
        workspace_id,
        arbitration_id=0x416,
        source_network="SU",
        destination_network="IC",
    )
    missing_destination = manager.trace_message_route(
        workspace_id,
        arbitration_id=0x416,
        source_network="SU",
        destination_network="BD",
    )
    absent = manager.trace_message_route(workspace_id, arbitration_id=0x700)
    signal = manager.trace_signal_gateway(workspace_id, "VehicleSpeed")
    communication = manager.inspect_communication(
        workspace_id, arbitration_id=0x534, network="Network_BD"
    )

    assert route["route_found"] is True
    assert route["requested_destination_found"] is True
    assert missing_destination["route_found"] is True
    assert missing_destination["requested_destination_found"] is False
    assert absent["route_found"] is False
    assert signal["gateway_found"] is True
    assert signal["gateways"][0]["mapping"] == "VehicleSpeed_GW"
    assert communication["message"]["is_fd"] is True
    assert communication["message"]["length_bytes"] == 16


def test_fixture_supports_period_group_source_and_ambiguity_cases():
    manager = ConfigWorkspaceManager()
    loaded = manager.load_config_workspace(str(FIXTURE_ROOT))
    workspace_id = loaded["workspace_id"]

    communication = manager.inspect_communication(
        workspace_id, arbitration_id=0x534, network="Network_BD"
    )
    parameter_values = {
        parameter["name"]: parameter["value"]
        for item in communication["communication_objects"]
        for ipdu in item["com_ipdus"]
        for signal in ipdu["signals"]
        for parameter in signal["properties"]
    }
    timing_values = {
        parameter["name"]: parameter["value"]
        for item in communication["communication_objects"]
        for ipdu in item["com_ipdus"]
        for config in ipdu["timing_and_mode_configs"]
        for parameter in config["parameters"]
    }
    tx_communication = manager.inspect_communication(
        workspace_id, pdu_name="Pdu_534_Tx"
    )
    group_reference = tx_communication["communication_objects"][0]["com_ipdus"][0][
        "group_references"
    ][0]
    group = manager.inspect_ipdu_group(workspace_id, group_reference)
    source = manager.inspect_source_symbol(workspace_id, "PduRRoutingPath_416_SU")

    assert parameter_values["ComTimeout"] == "0.5"
    assert parameter_values["ComRxDataTimeoutAction"] == "REPLACE"
    assert timing_values["ComTxModeTimePeriod"] == "0.02"
    assert group["group"]["name"] == "TxGroup"
    assert group["direct_control_reference_count"] == 2
    assert group["runtime_state_inferred"] is False
    assert source["inspection_basis"] == "exact_c_identifier"
    assert source["returned_count"] == 3
    with pytest.raises(ConfigConflictError, match="多个候选"):
        manager.trace_message_route(workspace_id, arbitration_id=0x534)
