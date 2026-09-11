"""验证 Trace Session 复用、查询、时序、解码和错误契约。"""

from pathlib import Path
from threading import Event

import pytest

from anda.common.errors import (
    TraceDatabaseError,
    TraceInputError,
    TraceNotFoundError,
    TraceNotReadyError,
)
from anda.trace.dbc import DatabaseMessage
from anda.trace.models import RawFrame
from anda.trace.routed_timeout import analyze_routed_signal_timeout
from anda.trace.session import MAX_FRAME_RESULTS, TraceSessionManager
from anda.trace.store import CompactFrameStore


class _ByteDecoder:
    def decode(self, data: bytes) -> dict:
        return {"TimeoutStatus": data[0]}


class _PerChannelDatabase:
    def __init__(self, messages: dict[str, DatabaseMessage]) -> None:
        self.messages = messages

    def resolve_message(
        self,
        selector: str,
        *,
        bus_type: str | None = None,
        database_file: str | None = None,
    ) -> DatabaseMessage:
        assert selector == "LinStatus"
        assert bus_type == "lin"
        assert database_file is not None
        return self.messages[database_file]

    def resolve_signal(
        self,
        arbitration_id: int,
        signal_name: str,
        bus_type: str | None = None,
        database_file: str | None = None,
    ) -> DatabaseMessage:
        assert signal_name == "TimeoutStatus"
        assert bus_type == "lin"
        assert database_file is not None
        message = self.messages[database_file]
        assert message.frame_id == arbitration_id
        return message


def _append_frame(
    store: CompactFrameStore,
    timestamp: float,
    channel: int,
    frame_id: int,
    value: int,
    bus_type: str,
) -> None:
    store.append(
        RawFrame(
            timestamp=timestamp,
            channel=channel,
            arbitration_id=frame_id,
            dlc=8,
            data=bytes([value]) + bytes(7),
            is_extended_id=False,
            is_fd=False,
            is_rx=True,
            bus_type=bus_type,
        )
    )


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
    assert summary["timestamp_reference"]["capture_point"] == "UNKNOWN"
    assert summary["timestamp_reference"]["ecu_internal_send_time"] == "UNKNOWN"


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


def test_routed_timeout_requires_explicit_channel_mapping(routed_timeout_files):
    blf_path, ldf_path = routed_timeout_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path), str(ldf_path))["trace_id"]

    with pytest.raises(TraceInputError, match="set_channel_mapping"):
        manager.analyze_routed_signal_timeout(
            trace_id,
            source_channel=1,
            source_message="0x207",
            target_channels=[8, 9],
            target_frame="0x2A",
            signal_name="TimeoutStatus",
            timeout_value=0,
            configured_timeout_ms=500,
        )


def test_routed_timeout_pairs_events_and_reports_per_channel_statistics(
    routed_timeout_files,
):
    blf_path, ldf_path = routed_timeout_files
    manager = TraceSessionManager()
    trace_id = manager.load_trace(str(blf_path), str(ldf_path))["trace_id"]
    mapping = manager.set_channel_mapping(
        trace_id,
        [
            {
                "analysis_channel": 1,
                "bus_type": "can",
                "logical_network": "FL_CANFD_IC",
                "mapping_source": "user",
            },
            {
                "analysis_channel": 8,
                "bus_type": "lin",
                "logical_network": "FL_LIN_TDL1",
                "ecu_channel": "LIN02",
                "mapping_source": "test report",
            },
            {
                "analysis_channel": 9,
                "bus_type": "lin",
                "logical_network": "FL_LIN_TDL2",
                "ecu_channel": "LIN06",
                "mapping_source": "test report",
            },
        ],
    )
    result = manager.analyze_routed_signal_timeout(
        trace_id,
        source_channel=1,
        source_message="0x207",
        target_channels=[8, 9],
        target_frame="0x2A",
        signal_name="TimeoutStatus",
        timeout_value=0,
        configured_timeout_ms=500,
    )

    assert mapping["mapping_inferred"] is False
    assert result["detected_source_stop_event_count"] == 1
    event = result["events"][0]
    targets = {item["analysis_channel"]: item for item in event["targets"]}
    assert targets[8]["missed_bus_frame_count"] == 1
    assert targets[9]["missed_bus_frame_count"] == 2
    assert targets[8]["first_target_old_after_source_stop"]["timestamp"] < (
        event["deadline_timestamp"]
    )
    assert targets[8]["source_to_timeout_ms"] == pytest.approx(520.0)
    assert targets[9]["source_to_timeout_ms"] == pytest.approx(540.0)
    assert targets[9]["exceeds_expected_max"] is True
    statistics = {
        item["analysis_channel"]: item for item in result["per_channel_statistics"]
    }
    assert statistics[8]["source_to_timeout_ms"]["p95"] == pytest.approx(520.0)
    assert statistics[9]["source_to_timeout_ms"]["max"] == pytest.approx(540.0)
    assert result["abnormal_event_count"] == 1
    assert result["root_cause_inferred"] is False
    assert result["timestamp_reference"]["capture_point"] == "UNKNOWN"


def test_routed_timeout_supports_per_channel_ids_and_direct_transition(tmp_path: Path):
    store = CompactFrameStore()
    for timestamp in (0.0, 0.1, 1.0):
        _append_frame(store, timestamp, 1, 0x207, 0x3F, "can")
    _append_frame(store, 0.09, 8, 0x2A, 0x3F, "lin")
    _append_frame(store, 0.61, 8, 0x2A, 0x00, "lin")
    _append_frame(store, 0.09, 9, 0x2B, 0x3F, "lin")
    _append_frame(store, 0.62, 9, 0x2B, 0x00, "lin")
    messages = {
        name: DatabaseMessage(
            path=tmp_path / name,
            bus_type="lin",
            frame_id=frame_id,
            name="LinStatus",
            signal_names=("TimeoutStatus",),
            decoder=_ByteDecoder(),
        )
        for name, frame_id in (("lin8.ldf", 0x2A), ("lin9.ldf", 0x2B))
    }

    result = analyze_routed_signal_timeout(
        store=store,
        database=_PerChannelDatabase(messages),
        source_channel=1,
        source_message="0x207",
        target_channels=[8, 9],
        target_frame="LinStatus",
        signal_name="TimeoutStatus",
        timeout_value=0,
        configured_timeout_ms=500,
        target_database_files={"8": "lin8.ldf", "9": "lin9.ldf"},
    )

    targets = {
        item["analysis_channel"]: item for item in result["events"][0]["targets"]
    }
    assert result["target"]["frame_ids_by_channel"] == {"8": 0x2A, "9": 0x2B}
    assert targets[8]["first_target_old_after_source_stop"] is None
    assert targets[8]["last_target_old_before_timeout"]["timestamp"] == 0.09
    assert targets[8]["source_to_timeout_ms"] == pytest.approx(510.0)
    assert targets[9]["source_to_timeout_ms"] == pytest.approx(520.0)
