"""确定性验证 Dynamic Skill 的流程、安全边界和 Eval 契约。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = (
    ROOT
    / "plugins"
    / "automotive-network-debug-agent"
    / "skills"
    / "dynamic-verification"
)
SKILL_PATH = SKILL_ROOT / "SKILL.md"
OPENAI_YAML_PATH = SKILL_ROOT / "agents" / "openai.yaml"
EVAL_PATH = ROOT / "evals" / "dynamic_agent_cases.json"

OUTPUT_SECTIONS = [
    "动态验证结论",
    "实验过程",
    "关键观察",
    "不确定项",
    "下一步建议",
]


def test_dynamic_skill_is_packaged_and_harness_native():
    content = SKILL_PATH.read_text(encoding="utf-8")
    interface = OPENAI_YAML_PATH.read_text(encoding="utf-8")

    assert content.startswith("---\nname: dynamic-verification\n")
    assert "automotive-dynamic" in content
    assert "API Key" in content
    assert "不使用 API Key、OpenAI API、Agents SDK" in content
    assert "$dynamic-verification" in interface
    assert "allow_implicit_invocation: true" in interface


def test_dynamic_skill_enforces_baseline_minimal_stimulus_restore_and_cleanup():
    content = SKILL_PATH.read_text(encoding="utf-8")

    positions = [content.index(f"## {section}") for section in OUTPUT_SECTIONS]
    assert positions == sorted(positions)
    assert "先观察 baseline，再刺激" in content
    assert "一次只改变一个变量" in content
    assert "不要把全部能力自动全开" in content
    assert "不得编造结果" in content
    assert "都必须调用 `close_dynamic_session`" in content
    assert "不得从网段名、数据库顺序或经验补值" in content
    assert 'instance_mode="attached"' in content
    assert "can_quit=false" in content
    assert "当前 CANoe 有有效许可证，并允许本次占用" in content
    assert "license_confirmed=true" in content
    assert "支持候选假设" in content
    assert "削弱候选假设" in content
    assert "当前无法验证" in content


def test_dynamic_eval_covers_required_safety_scenarios():
    specification = json.loads(EVAL_PATH.read_text(encoding="utf-8"))

    assert specification["required_output_sections"] == OUTPUT_SECTIONS
    assert {case["id"] for case in specification["cases"]} == {
        "license_confirmation_missing",
        "blf_only_offline",
        "attached_preserves_user_canoe",
        "signal_restore",
        "system_variable_restore",
        "raw_can_missing_parameters",
        "minimal_runtime_policy",
        "tool_failure_no_fabrication",
        "evidence_already_sufficient",
    }
    for case in specification["cases"]:
        plan = case["expected_tool_plan"]
        if plan:
            assert plan[-1] == "close_dynamic_session"
        assert not (set(plan) & set(case.get("forbidden_tools", [])))

    signal_plan = next(
        case["expected_tool_plan"]
        for case in specification["cases"]
        if case["id"] == "signal_restore"
    )
    assert signal_plan.index("get_signal") < signal_plan.index("set_signal")
    assert signal_plan.count("set_signal") == 2
