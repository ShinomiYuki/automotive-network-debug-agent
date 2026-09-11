"""确定性验证 Debug Skill 的路由、证据综合和 Plugin 契约。"""

import json
import tomllib
from pathlib import Path

from anda import __version__

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins" / "automotive-network-debug-agent"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "debug-analysis"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
OPENAI_YAML_PATH = SKILL_ROOT / "agents" / "openai.yaml"
PLUGIN_MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MCP_MANIFEST_PATH = PLUGIN_ROOT / ".mcp.json"
EVAL_PATH = ROOT / "evals" / "debug_agent_cases.json"

TRACE_TOOLS = {
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
}
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
ALL_TOOLS = TRACE_TOOLS | CONFIG_TOOLS
OUTPUT_SECTIONS = ["综合判断", "关键证据", "候选原因", "不确定项", "下一步建议"]
CASE_IDS = {
    "trace_only_message_presence",
    "config_only_route",
    "trace_then_config_missing_destination",
    "config_question_stops_despite_blf",
    "config_present_trace_abnormal",
    "issue_statement_conflicts_with_trace",
    "trace_config_version_conflict",
    "signal_trace_then_gateway",
    "insufficient_cross_domain_evidence",
    "missing_inputs_asks_minimum",
    "config_then_trace_when_route_exists",
    "can_to_lin_timeout_with_report",
}


def test_debug_skill_is_packaged_with_discriminating_metadata():
    content = SKILL_PATH.read_text(encoding="utf-8")
    interface = OPENAI_YAML_PATH.read_text(encoding="utf-8")

    assert content.startswith("---\nname: debug-analysis\n")
    frontmatter = content.split("---", 2)[1]
    assert "跨来源问题" in frontmatter
    assert "单纯日志事实调查应使用 trace-analysis" in frontmatter
    assert "单纯静态配置查询应使用 config-analysis" in frontmatter
    assert 'display_name: "Debug Analysis"' in interface
    assert "$debug-analysis" in interface
    assert "allow_implicit_invocation: true" in interface
    assert {path.name for path in (PLUGIN_ROOT / "skills").iterdir()} == {
        "trace-analysis",
        "config-analysis",
        "debug-analysis",
    }


def test_debug_skill_defines_dynamic_routes_evidence_rules_and_fixed_output():
    content = SKILL_PATH.read_text(encoding="utf-8")

    for route in ("Trace only", "Config only", "Trace → Config", "Config → Trace"):
        assert f"`{route}`" in content
    heading_positions = [content.index(f"## {section}") for section in OUTPUT_SECTIONS]
    assert heading_positions == sorted(heading_positions)
    assert "第二阶段必须由第一阶段结果触发" in content
    assert "同一 BLF/数据库最多调用一次 `load_trace`" in content
    assert "同一工程和所选 ARXML 最多调用一次 `load_config_workspace`" in content
    assert "跨域只传递有限事实" in content
    assert "普通样本查询使用 `limit=20`" in content
    assert "首次查询返回零条匹配时" in content
    assert "不能把 SU、IC 等逻辑网段名当作 Channel" in content
    assert "不得因此认定源码 Bug" in content
    assert "版本列为不确定项" in content
    assert "当前证据不足以形成可靠候选根因" in content
    assert "不要把事实结论重述成故障原因" in content
    assert "不调用其他 Skill" in content
    assert "不修改配置、源码或日志" in content
    assert "`analyze_routed_signal_timeout`" in content
    assert "`trace_autosar_runtime_chain`" in content
    assert "`create_investigation_bundle`" in content
    assert (SKILL_ROOT / "references" / "investigation-workflow.md").is_file()


def test_debug_eval_covers_routes_stage_gates_handoffs_and_minimal_loads():
    specification = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    allowed_handoff = set(specification["allowed_handoff_facts"])

    assert specification["required_output_sections"] == OUTPUT_SECTIONS
    assert {case["id"] for case in specification["cases"]} == CASE_IDS
    assert {case["route"] for case in specification["cases"]} == {
        "trace_only",
        "config_only",
        "trace_then_config",
        "config_then_trace",
        "ask_input",
    }

    for case in specification["cases"]:
        plan = case["expected_tool_plan"]
        forbidden = case["forbidden_tools"]
        flattened_stages = [
            tool for stage in case["expected_stages"] for tool in stage["tools"]
        ]
        assert flattened_stages == plan
        assert set(plan) <= ALL_TOOLS
        assert set(forbidden) <= ALL_TOOLS
        assert not (set(plan) & set(forbidden))
        assert plan.count("load_trace") <= 1
        assert plan.count("load_config_workspace") <= 1
        for index, stage in enumerate(case["expected_stages"]):
            if index:
                assert stage["after_observation"]
                assert set(stage["handoff_facts"]) <= allowed_handoff

        if case["route"] == "trace_only":
            assert set(plan) <= TRACE_TOOLS
            assert {
                "load_config_workspace",
                "get_config_load_status",
                "trace_message_route",
            } <= set(forbidden)
        elif case["route"] == "config_only":
            assert set(plan) <= CONFIG_TOOLS
            assert {
                "load_trace",
                "get_trace_load_status",
                "find_messages",
            } <= set(forbidden)
        elif case["route"] == "ask_input":
            assert plan == []
            assert {
                "load_trace",
                "load_config_workspace",
                "find_messages",
                "trace_message_route",
            } <= set(forbidden)


def test_debug_eval_encodes_conflict_and_non_inference_boundaries():
    specification = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in specification["cases"]}

    assert (
        "不得认定源码 Bug"
        in cases["config_present_trace_abnormal"]["expected_behavior"][0]
    )
    assert (
        "问题描述与当前日志证据不一致"
        in cases["issue_statement_conflicts_with_trace"]["expected_behavior"][0]
    )
    assert (
        "版本一致性列为高优先级不确定项"
        in cases["trace_config_version_conflict"]["expected_behavior"][1]
    )
    assert (
        "当前无法形成可靠候选原因"
        in cases["insufficient_cross_domain_evidence"]["expected_behavior"][0]
    )
    assert "不机械要求 DBC" in cases["missing_inputs_asks_minimum"]["expected_behavior"]
    assert (
        "数字 Channel 映射"
        in cases["missing_inputs_asks_minimum"]["expected_behavior"][0]
    )
    for case_id in (
        "trace_then_config_missing_destination",
        "config_present_trace_abnormal",
        "issue_statement_conflicts_with_trace",
        "trace_config_version_conflict",
        "signal_trace_then_gateway",
        "config_then_trace_when_route_exists",
    ):
        assert "channel_mapping" in cases[case_id]["provided_inputs"]


def test_plugin_keeps_one_package_two_mcps_and_no_independent_agent_runtime():
    plugin = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mcp_manifest = json.loads(MCP_MANIFEST_PATH.read_text(encoding="utf-8"))
    repository_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "pyproject.toml",
            PLUGIN_MANIFEST_PATH,
            MCP_MANIFEST_PATH,
            SKILL_PATH,
        )
    ).casefold()

    assert plugin["name"] == "automotive-network-debug-agent"
    assert __version__ == project["project"]["version"]
    assert plugin["version"].startswith(f"{__version__}+codex.")
    assert set(mcp_manifest["mcpServers"]) == {
        "automotive-trace",
        "automotive-config",
    }
    assert "debug analysis" in plugin["interface"]["longDescription"].casefold()
    assert any(
        "$debug-analysis" in prompt for prompt in plugin["interface"]["defaultPrompt"]
    )
    assert len(plugin["interface"]["defaultPrompt"]) <= 3
    assert "openai_api_key" not in repository_text
    assert "openai-agents" not in repository_text
    assert not (PLUGIN_ROOT / "src" / "anda" / "agents" / "debug_agent.py").exists()
    assert not (ROOT / ".codex" / "agents").exists()
    assert not (PLUGIN_ROOT / ".codex" / "agents").exists()
