"""验证结构化调查证据包的创建、追加、状态更新和 Markdown 导出。"""

import json

import pytest

from anda.common.errors import InvestigationError
from anda.investigation.bundle import (
    create_investigation_bundle,
    export_investigation_markdown,
    get_investigation_summary,
    record_investigation_evidence,
    update_investigation_state,
)


def test_investigation_bundle_persists_evidence_and_exports_markdown(tmp_path):
    created = create_investigation_bundle(
        str(tmp_path),
        "can-to-lin-timeout",
        {"title": "CAN to LIN timeout", "inputs": {"blf": "sample.blf"}},
        [
            {
                "analysis_channel": 1,
                "logical_network": "FL_CANFD_IC",
                "mapping_source": "user",
            }
        ],
    )
    bundle = created["bundle_path"]
    record_investigation_evidence(
        bundle,
        "trace_events",
        {"event": 1, "source_to_timeout_ms": 529.3},
    )
    record_investigation_evidence(
        bundle,
        "counter_evidence",
        [{"candidate": "CDD", "result": "NOT_FOUND_IN_SCOPE"}],
    )
    update_investigation_state(
        bundle,
        hypotheses=[{"name": "task phase", "support": "high", "counter": []}],
        open_questions=[{"question": "BLF capture point", "status": "open"}],
    )

    summary = get_investigation_summary(bundle)
    exported = export_investigation_markdown(bundle)

    assert summary["evidence"]["trace_events"]["count"] == 1
    assert summary["evidence"]["channel_mapping"]["count"] == 1
    assert summary["hypothesis_count"] == 1
    assert summary["open_question_count"] == 1
    assert exported["pdf_export_available"] is False
    report = (tmp_path / "can-to-lin-timeout" / "report.md").read_text(
        encoding="utf-8"
    )
    assert "CAN to LIN timeout" in report
    assert "NOT_FOUND_IN_SCOPE" in report
    case = json.loads(
        (tmp_path / "can-to-lin-timeout" / "case.json").read_text(
            encoding="utf-8"
        )
    )
    assert case["updated_at"] >= case["created_at"]


def test_investigation_bundle_refuses_to_overwrite_existing_case(tmp_path):
    created = create_investigation_bundle(
        str(tmp_path),
        "case-1",
        {"case_id": "forged", "created_at": "forged", "updated_at": "forged"},
    )
    case = json.loads(
        (tmp_path / "case-1" / "case.json").read_text(encoding="utf-8")
    )

    assert created["created"] is True
    assert case["case_id"] == "case-1"
    assert case["created_at"] != "forged"

    with pytest.raises(InvestigationError, match="拒绝覆盖"):
        create_investigation_bundle(str(tmp_path), "case-1", {})
