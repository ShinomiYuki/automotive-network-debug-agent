"""
文件用途：
- 在用户明确指定的目录中持久化一次汽车网络调查的结构化证据包。
- 将已记录事实、反证、假设和开放问题确定性导出为 Markdown。

本模块不调用模型、不读取未指定工程，也不从证据自动生成新的根因结论。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from anda.common.errors import InvestigationError

EVIDENCE_CATEGORIES = {
    "trace_events",
    "channel_mapping",
    "config_chain",
    "source_snippets",
    "counter_evidence",
    "health_check",
}
_CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_MAX_TOOL_SUMMARY_ITEMS = 20
_MAX_REPORT_SECTION_CHARS = 100_000


def create_investigation_bundle(
    output_directory: str,
    case_id: str,
    case_data: dict,
    channel_mapping: list[dict] | None = None,
) -> dict:
    """创建固定目录结构；拒绝覆盖已有案件。"""
    normalized_id = case_id.strip()
    if not _CASE_ID_RE.fullmatch(normalized_id):
        raise InvestigationError(
            "case_id 必须为 1 到 80 位字母、数字、点、下划线或连字符"
        )
    output_root = Path(output_directory).expanduser().resolve()
    bundle = output_root / normalized_id
    if bundle.exists():
        raise InvestigationError(f"调查证据包已存在，拒绝覆盖：{bundle}")
    evidence = bundle / "evidence"
    evidence.mkdir(parents=True)
    now = _now()
    case_payload = {
        **case_data,
        "case_id": normalized_id,
        "created_at": now,
        "updated_at": now,
    }
    _write_json(bundle / "case.json", case_payload)
    for category in EVIDENCE_CATEGORIES:
        initial = (channel_mapping or []) if category == "channel_mapping" else []
        _write_json(evidence / f"{category}.json", initial)
    _write_json(bundle / "hypotheses.json", [])
    _write_json(bundle / "open_questions.json", [])
    return {
        "bundle_path": str(bundle),
        "case_file": str(bundle / "case.json"),
        "evidence_directory": str(evidence),
        "created": True,
        "files": _bundle_files(bundle),
    }


def record_investigation_evidence(
    bundle_path: str,
    category: str,
    evidence: dict | list[dict],
    replace: bool = False,
) -> dict:
    """追加或替换一个受控证据分类；每次写入都更新案件时间。"""
    bundle = _existing_bundle(bundle_path)
    normalized = category.strip().casefold()
    if normalized not in EVIDENCE_CATEGORIES:
        raise InvestigationError(
            "category 仅支持：" + ", ".join(sorted(EVIDENCE_CATEGORIES))
        )
    incoming = evidence if isinstance(evidence, list) else [evidence]
    if not all(isinstance(item, dict) for item in incoming):
        raise InvestigationError("evidence 必须是对象或对象列表")
    target = bundle / "evidence" / f"{normalized}.json"
    current = [] if replace else _read_json(target)
    if not isinstance(current, list):
        raise InvestigationError(f"证据文件格式损坏：{target}")
    updated = [*current, *incoming]
    _write_json(target, updated)
    _touch_case(bundle)
    return {
        "bundle_path": str(bundle),
        "category": normalized,
        "replaced": replace,
        "added_count": len(incoming),
        "total_count": len(updated),
        "file": str(target),
    }


def update_investigation_state(
    bundle_path: str,
    hypotheses: list[dict] | None = None,
    open_questions: list[dict] | None = None,
) -> dict:
    """替换假设和开放问题快照，避免旧状态与新证据混在一起。"""
    bundle = _existing_bundle(bundle_path)
    if hypotheses is None and open_questions is None:
        raise InvestigationError("hypotheses 和 open_questions 至少提供一项")
    updated = []
    if hypotheses is not None:
        _validate_object_list(hypotheses, "hypotheses")
        _write_json(bundle / "hypotheses.json", hypotheses)
        updated.append("hypotheses")
    if open_questions is not None:
        _validate_object_list(open_questions, "open_questions")
        _write_json(bundle / "open_questions.json", open_questions)
        updated.append("open_questions")
    _touch_case(bundle)
    return {
        "bundle_path": str(bundle),
        "updated_sections": updated,
    }


def get_investigation_summary(bundle_path: str) -> dict:
    """返回数量和有限样本，不把整个证据包倾倒到 Tool 响应。"""
    bundle = _existing_bundle(bundle_path)
    case_data = _read_json(bundle / "case.json")
    evidence_summary = {}
    for category in sorted(EVIDENCE_CATEGORIES):
        payload = _read_json(bundle / "evidence" / f"{category}.json")
        items = payload if isinstance(payload, list) else [payload]
        evidence_summary[category] = {
            "count": len(items),
            "sample": items[:_MAX_TOOL_SUMMARY_ITEMS],
            "sample_truncated": len(items) > _MAX_TOOL_SUMMARY_ITEMS,
        }
    hypotheses = _read_json(bundle / "hypotheses.json")
    questions = _read_json(bundle / "open_questions.json")
    return {
        "bundle_path": str(bundle),
        "case": case_data,
        "evidence": evidence_summary,
        "hypothesis_count": len(hypotheses),
        "hypotheses": hypotheses[:_MAX_TOOL_SUMMARY_ITEMS],
        "open_question_count": len(questions),
        "open_questions": questions[:_MAX_TOOL_SUMMARY_ITEMS],
    }


def export_investigation_markdown(
    bundle_path: str, title: str | None = None
) -> dict:
    """把现有结构化内容导出为 Markdown；不新增未记录的推断。"""
    bundle = _existing_bundle(bundle_path)
    case_data = _read_json(bundle / "case.json")
    report_title = title.strip() if title and title.strip() else str(
        case_data.get("title") or case_data.get("case_id") or "汽车网络调查报告"
    )
    sections = [
        f"# {report_title}",
        "",
        "> 本报告由结构化 investigation bundle 确定性导出；不会自动补充根因推断。",
        "",
        "## 案件信息",
        "",
        _json_block(case_data),
    ]
    for category in sorted(EVIDENCE_CATEGORIES):
        payload = _read_json(bundle / "evidence" / f"{category}.json")
        sections.extend(
            [
                "",
                f"## 证据：{category}",
                "",
                _json_block(payload),
            ]
        )
    sections.extend(
        [
            "",
            "## 候选假设与反证状态",
            "",
            _json_block(_read_json(bundle / "hypotheses.json")),
            "",
            "## 不确定项与开放问题",
            "",
            _json_block(_read_json(bundle / "open_questions.json")),
            "",
        ]
    )
    target = bundle / "report.md"
    _write_text(target, "\n".join(sections))
    return {
        "bundle_path": str(bundle),
        "report_path": str(target),
        "format": "markdown",
        "pdf_export_available": False,
    }


def _existing_bundle(value: str) -> Path:
    bundle = Path(value).expanduser().resolve()
    if not bundle.is_dir() or not (bundle / "case.json").is_file():
        raise InvestigationError(f"不是有效的 investigation bundle：{bundle}")
    return bundle


def _touch_case(bundle: Path) -> None:
    case_file = bundle / "case.json"
    payload = _read_json(case_file)
    if not isinstance(payload, dict):
        raise InvestigationError(f"case.json 格式损坏：{case_file}")
    payload["updated_at"] = _now()
    _write_json(case_file, payload)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvestigationError(f"无法读取调查文件：{path}：{error}") from error


def _write_json(path: Path, payload: object) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise InvestigationError(f"无法写入调查文件：{path}：{error}") from error


def _bundle_files(bundle: Path) -> list[str]:
    return [
        str(path)
        for path in sorted(bundle.rglob("*"), key=lambda item: str(item).casefold())
        if path.is_file()
    ]


def _validate_object_list(values: list[dict], label: str) -> None:
    if not all(isinstance(item, dict) for item in values):
        raise InvestigationError(f"{label} 必须是对象列表")


def _json_block(payload: object) -> str:
    value = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(value) > _MAX_REPORT_SECTION_CHARS:
        value = value[:_MAX_REPORT_SECTION_CHARS] + "\n...（本节已按导出上限截断）"
    return f"```json\n{value}\n```"


def _now() -> str:
    return datetime.now(UTC).isoformat()
