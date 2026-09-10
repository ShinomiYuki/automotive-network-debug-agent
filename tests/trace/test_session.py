"""验证 Trace Session 复用、查询、时序、解码和错误契约。"""

from threading import Event

import pytest

from anda.common.errors import (
    TraceDatabaseError,
    TraceInputError,
    TraceNotFoundError,
    TraceNotReadyError,
)
from anda.trace.session import MAX_FRAME_RESULTS, TraceSessionManager


def test_load_trace_reuses_unchanged_source(trace_files):
    blf_path, dbc_path = trace_files
    manager = TraceSessionManager()

    first = manager.load_trace(str(blf_path), str(dbc_path))
    second = manager.load_trace(str(blf_path), str(dbc_path))

    assert first["trace_id"] == second["trace_id"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert first["frame_count"] == 225
    assert first["database_loaded"] is True


def test_background_load_returns_stable_id_and_deduplicates(trace_files, monkeypatch):
    blf_path, dbc_path = trace_files
    manager = TraceSessionManager()
    entered = Event()
    release = Event()
    original_build = TraceSessionManager._build_session

    def delayed_build(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        TraceSessionManager, "_build_session", staticmethod(delayed_build)
    )
    first = manager.start_load_trace(str(blf_path), str(dbc_path))
    assert entered.wait(1)
    second = manager.start_load_trace(str(blf_path), str(dbc_path))
    status = manager.get_load_status(first["trace_id"])

    assert first["index_ready"] is False
    assert second["trace_id"] == first["trace_id"]
    assert second["reused"] is True
    assert status["status"] == "loading"
    with pytest.raises(TraceNotReadyError, match="get_trace_load_status"):
        manager.get_summary(first["trace_id"])

    release.set()
    ready = manager.get_load_status(first["trace_id"], wait_seconds=5)
    assert ready["status"] == "ready"
    assert ready["indexed_frame_count"] == 225


def test_background_load_surfaces_failure_and_allows_retry(tmp_path):
    corrupt = tmp_path / "corrupt.blf"
    corrupt.write_bytes(b"not a BLF")
    manager = TraceSessionManager()

    first = manager.start_load_trace(str(corrupt))
    failed = manager.get_load_status(first["trace_id"], wait_seconds=5)
    retry = manager.start_load_trace(str(corrupt))

    assert failed["status"] == "failed"
    assert "BLF" in failed["error"]
    assert retry["trace_id"] != first["trace_id"]
    assert retry["reused"] is False
    assert manager.get_load_status(retry["trace_id"], wait_seconds=5)["status"] == (
        "failed"
    )


def test_summary_reports_time_channels_and_can_types(trace_files):
    blf_path, dbc_path = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path), str(dbc_path))["trace_id"]

    summary = manager.get_summary(trace_id)

    assert summary["frame_count"] == 225
    assert summary["channels"] == [1, 2]
    assert summary["arbitration_id_count"] == 4
    assert summary["classic_can_frame_count"] == 224
    assert summary["can_fd_frame_count"] == 1
    assert summary["duration_seconds"] == pytest.approx(2.19)


def test_unknown_trace_id_is_clear_error():
    with pytest.raises(TraceNotFoundError, match="未知 trace_id"):
        TraceSessionManager().get_summary("missing")


def test_load_trace_rejects_missing_input(tmp_path):
    with pytest.raises(TraceInputError, match="BLF 文件不存在"):
        TraceSessionManager().load_trace(str(tmp_path / "missing.blf"))


def test_query_rejects_invalid_limit_channel_and_time_range(trace_files):
    blf_path, _ = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path))["trace_id"]

    with pytest.raises(TraceInputError, match="limit"):
        manager.find_messages(trace_id, limit=0)
    with pytest.raises(TraceInputError, match="1-based"):
        manager.find_messages(trace_id, channel=0)
    with pytest.raises(TraceInputError, match="start_timestamp"):
        manager.find_messages(trace_id, start_timestamp=2.0, end_timestamp=1.0)


def test_find_messages_supports_id_channel_limit_and_empty_result(trace_files):
    blf_path, _ = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path))["trace_id"]

    by_id = manager.find_messages(trace_id, arbitration_id=0x100, limit=2)
    by_channel = manager.find_messages(trace_id, channel=2, limit=10)
    no_match = manager.find_messages(trace_id, arbitration_id=0x777)

    assert by_id["total_count"] == 220
    assert by_id["returned_count"] == 2
    assert all(frame["arbitration_id"] == 0x100 for frame in by_id["frames"])
    assert by_channel["total_count"] == 3
    assert all(frame["channel"] == 2 for frame in by_channel["frames"])
    assert no_match["total_count"] == 0
    assert no_match["frames"] == []


def test_find_messages_caps_large_requested_limit(trace_files):
    blf_path, _ = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path))["trace_id"]

    result = manager.find_messages(trace_id, arbitration_id=0x100, limit=50_000)

    assert result["total_count"] == 220
    assert result["returned_count"] == MAX_FRAME_RESULTS
    assert result["limit_applied"] == MAX_FRAME_RESULTS


def test_find_messages_supports_absolute_time_range(trace_files):
    blf_path, _ = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path))["trace_id"]

    result = manager.find_messages(
        trace_id,
        arbitration_id=0x100,
        start_timestamp=1_700_000_000.02,
        end_timestamp=1_700_000_000.04,
    )

    assert result["total_count"] == 3


def test_message_timing_covers_regular_irregular_single_and_empty(trace_files):
    blf_path, _ = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path))["trace_id"]

    regular = manager.get_message_timing(trace_id, 0x100, channel=1)
    irregular = manager.get_message_timing(trace_id, 0x200, channel=2)
    single = manager.get_message_timing(trace_id, 0x400, channel=1)
    empty = manager.get_message_timing(trace_id, 0x777)

    assert regular["frame_count"] == 220
    assert regular["period_median_ms"] == pytest.approx(10.0, rel=1e-4)
    assert irregular["frame_count"] == 3
    assert irregular["period_min_ms"] == pytest.approx(15.0, rel=1e-4)
    assert irregular["period_max_ms"] == pytest.approx(25.0, rel=1e-4)
    assert single["frame_count"] == 1
    assert single["period_median_ms"] is None
    assert empty["frame_count"] == 0


def test_decode_signal_returns_stats_and_limited_samples(trace_files):
    blf_path, dbc_path = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path), str(dbc_path))["trace_id"]

    result = manager.decode_signal(trace_id, 0x100, "EngineSpeed", channel=1, limit=3)

    assert result["sample_count"] == 220
    assert result["returned_count"] == 3
    assert result["min"] == pytest.approx(1000.0)
    assert result["max"] == pytest.approx(1400.0)
    assert [sample["value"] for sample in result["samples"]] == [
        1000.0,
        1100.0,
        1200.0,
    ]


def test_decode_signal_rejects_missing_database(trace_files):
    blf_path, _ = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path))["trace_id"]

    with pytest.raises(TraceDatabaseError, match="未加载"):
        manager.decode_signal(trace_id, 0x100, "EngineSpeed")


def test_decode_signal_rejects_unknown_signal_and_can_id(trace_files):
    blf_path, dbc_path = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path), str(dbc_path))["trace_id"]

    with pytest.raises(TraceDatabaseError) as missing_signal:
        manager.decode_signal(trace_id, 0x100, "MissingSignal")
    assert "MissingSignal" in str(missing_signal.value)
    with pytest.raises(TraceDatabaseError) as missing_id:
        manager.decode_signal(trace_id, 0x999, "EngineSpeed")
    assert "0x999" in str(missing_id.value)


def test_search_database_resolves_signal_and_caps_results(trace_files):
    blf_path, dbc_path = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path), str(dbc_path))["trace_id"]

    exact = manager.search_database(trace_id, "VehicleSpeed", limit=100)
    case_variant = manager.search_database(trace_id, "vehiclespeed")
    partial = manager.search_database(trace_id, "speed")
    missing = manager.search_database(trace_id, "MissingSignal")

    assert exact["total_count"] == 1
    assert exact["limit_applied"] == 20
    assert exact["matches"] == [
        {
            "bus_type": "can",
            "arbitration_id": 0x100,
            "arbitration_id_hex": "0x100",
            "frame_id": 0x100,
            "frame_id_hex": "0x100",
            "message_name": "EngineData",
            "message_name_exact": False,
            "matched_signal_names": ["VehicleSpeed"],
            "signal_name_exact": True,
            "database_file": "test.dbc",
        }
    ]
    assert case_variant["matches"][0]["matched_signal_names"] == ["VehicleSpeed"]
    assert case_variant["matches"][0]["signal_name_exact"] is True
    assert partial["total_count"] == 1
    assert partial["matches"][0]["matched_signal_names"] == [
        "EngineSpeed",
        "VehicleSpeed",
    ]
    assert missing["matches"] == []


def test_lin_ldf_query_timing_and_decode(lin_trace_files):
    blf_path, ldf_path = lin_trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path), database_paths=[str(ldf_path)])[
        "trace_id"
    ]

    summary = manager.get_summary(trace_id)
    messages = manager.find_messages(
        trace_id, arbitration_id=0x2A, channel=8, bus_type="LIN"
    )
    timing = manager.get_message_timing(
        trace_id, arbitration_id=0x2A, channel=8, bus_type="lin"
    )
    database = manager.search_database(trace_id, "TimeoutStatus")
    signal = manager.decode_signal(
        trace_id,
        arbitration_id=0x2A,
        signal_name="TimeoutStatus",
        channel=8,
        bus_type="lin",
    )

    assert summary["lin_frame_count"] == 1
    assert summary["bus_types"] == ["lin"]
    assert messages["total_count"] == 1
    assert messages["filters"]["bus_type"] == "lin"
    assert messages["frames"][0]["frame_id"] == 0x2A
    assert messages["frames"][0]["bus_type"] == "lin"
    assert timing["frame_count"] == 1
    assert database["matches"][0]["database_file"] == "test.ldf"
    assert signal["sample_count"] == 1
    assert signal["samples"][0]["value"] == 0x7F


def test_search_database_rejects_missing_database_and_invalid_query(trace_files):
    blf_path, _ = trace_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path))["trace_id"]

    with pytest.raises(TraceDatabaseError, match="未加载"):
        manager.search_database(trace_id, "VehicleSpeed")
    with pytest.raises(TraceInputError, match="query"):
        manager.search_database(trace_id, " ")
    with pytest.raises(TraceInputError, match="limit"):
        manager.search_database(trace_id, "VehicleSpeed", limit=0)
