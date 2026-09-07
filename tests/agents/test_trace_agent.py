"""确定性验证项目级 Trace Skill、MCP 配置与 Harness eval 契约。"""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = ROOT / ".agents" / "skills" / "trace-analysis" / "SKILL.md"
CONFIG_PATH = ROOT / ".codex" / "config.toml"
EVAL_PATH = ROOT / "evals" / "trace_agent_cases.json"

TOOLS = {
    "load_trace",
    "get_trace_summary",
    "find_messages",
    "get_message_timing",
    "search_database",
    "decode_signal",
}
OUTPUT_SECTIONS = ["现象判断", "关键证据", "不确定项", "下一步建议"]


def test_trace_skill_is_project_discoverable_and_has_precise_frontmatter():
    content = SKILL_PATH.read_text(encoding="utf-8")

    assert content.startswith("---\nname: trace-analysis\n")
    assert "description:" in content.split("---", 2)[1]
    assert "BLF" in content.split("---", 2)[1]
    assert not (ROOT / "skills" / "trace-analysis" / "SKILL.md").exists()


def test_trace_skill_defines_minimal_routes_output_and_evidence_boundary():
    content = SKILL_PATH.read_text(encoding="utf-8")

    for tool in TOOLS:
        assert f"`{tool}`" in content
    heading_positions = [content.index(f"## {section}") for section in OUTPUT_SECTIONS]
    assert heading_positions == sorted(heading_positions)
    assert "仅当结果为零" in content
    assert "保留与第一次查询完全相同的时间范围" in content
    assert "只有得到唯一、名称精确匹配的信号候选后" in content
    assert "`matched_signal_names` 返回的规范信号名" in content
    assert "不从 Trace 单一证据推断" in content
    assert "已回答用户问题后立即停止" in content


def test_project_mcp_config_uses_native_stdio_launcher_without_model_runtime():
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    server = config["mcp_servers"]["automotive-trace"]
    project_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "pyproject.toml",
            CONFIG_PATH,
            ROOT / "scripts" / "run_trace_mcp.ps1",
        )
    )

    assert server["command"] == "pwsh"
    assert server["args"][-1] == "scripts/run_trace_mcp.ps1"
    assert server["cwd"] == "."
    assert "openai" not in project_text.casefold()


def test_eval_cases_cover_expected_tools_and_fixed_output_contract():
    specification = json.loads(EVAL_PATH.read_text(encoding="utf-8"))

    assert specification["required_output_sections"] == OUTPUT_SECTIONS
    assert {case["id"] for case in specification["cases"]} == {
        "message_exists_on_channel",
        "message_missing_on_requested_channel",
        "message_timing",
        "signal_decode_with_database",
        "signal_decode_without_database",
        "insufficient_root_cause_evidence",
    }
    for case in specification["cases"]:
        assert set(case["expected_tool_plan"]) <= TOOLS
        assert set(case["forbidden_tools"]) <= TOOLS
        assert not (
            set(case["expected_tool_plan"]) & set(case["forbidden_tools"])
        )
