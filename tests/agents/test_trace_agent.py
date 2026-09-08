"""确定性验证插件 Trace Skill、MCP 配置与 Harness eval 契约。"""

import json
import tomllib
from pathlib import Path

from anda import __version__

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins" / "automotive-network-debug-agent"
SKILL_PATH = PLUGIN_ROOT / "skills" / "trace-analysis" / "SKILL.md"
PLUGIN_MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MCP_MANIFEST_PATH = PLUGIN_ROOT / ".mcp.json"
OPENAI_YAML_PATH = SKILL_PATH.parent / "agents" / "openai.yaml"
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"
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


def test_trace_skill_is_plugin_discoverable_and_has_precise_frontmatter():
    content = SKILL_PATH.read_text(encoding="utf-8")
    interface = OPENAI_YAML_PATH.read_text(encoding="utf-8")

    assert content.startswith("---\nname: trace-analysis\n")
    assert "description:" in content.split("---", 2)[1]
    assert "BLF" in content.split("---", 2)[1]
    assert "测试路由/测试网段" in content
    assert "简要描述、前提条件、操作步骤、预期结果、实际结果" in content
    assert "allow_implicit_invocation: true" in interface
    assert "$trace-analysis" in interface
    assert not (ROOT / ".agents" / "skills" / "trace-analysis" / "SKILL.md").exists()


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


def test_plugin_packages_skill_and_native_stdio_mcp_without_model_runtime():
    plugin = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mcp_manifest = json.loads(MCP_MANIFEST_PATH.read_text(encoding="utf-8"))
    marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
    server = mcp_manifest["mcpServers"]["automotive-trace"]
    project_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "pyproject.toml",
            PLUGIN_MANIFEST_PATH,
            MCP_MANIFEST_PATH,
            PLUGIN_ROOT / "scripts" / "run_trace_mcp.ps1",
        )
    )

    assert plugin["name"] == "automotive-network-debug-agent"
    # Python 包、工程发行版和带 cachebuster 的插件版本必须共享同一基础版本，
    # 否则用户排查安装状态时会看到互相矛盾的版本号。
    assert __version__ == project["project"]["version"]
    assert plugin["version"].startswith(f"{__version__}+codex.")
    assert plugin["skills"] == "./skills/"
    assert plugin["mcpServers"] == "./.mcp.json"
    assert marketplace["name"] == "shinomi-yuki"
    assert marketplace["plugins"][0]["source"]["path"] == (
        "./plugins/automotive-network-debug-agent"
    )
    assert server["command"] == "pwsh"
    assert server["args"][-1] == "./scripts/run_trace_mcp.ps1"
    assert server["cwd"] == "."
    assert server["default_tools_approval_mode"] == "writes"
    assert "openai_api_key" not in project_text.casefold()
    assert not (ROOT / ".codex" / "config.toml").exists()


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
