"""模拟接口经过真实 BLF 判定，验证有限执行与清理边界。"""

import json
import threading
import time
from pathlib import Path

import can
import pytest

from anda.tsmaster.session import TSMasterSessions


def bench():
    common = {
        "device_type": 3,
        "device_subtype": 13,
        "device_index": 0,
        "device_name": "Synthetic device",
        "arbitration_kbps": 500,
        "data_kbps": 2000,
        "controller_type": 1,
        "termination": False,
    }
    return {
        "channels": [
            {
                **common,
                "network": "source",
                "logical_channel": 1,
                "device_channel": 0,
                "role": "source",
                "allowed_ids": [0x100],
            },
            {
                **common,
                "network": "target",
                "logical_channel": 2,
                "device_channel": 1,
                "role": "target",
                "response_ids": [0x708],
            },
        ]
    }


def frame(**overrides):
    return {
        "channel": 1,
        "arbitration_id": 0x100,
        "is_extended_id": False,
        "is_fd": False,
        "bitrate_switch": False,
        "data_hex": "01",
        "count": 2,
        "interval_ms": 2,
        **overrides,
    }


def route():
    endpoint = {"is_extended_id": False, "is_fd": False}
    return {
        "name": "synthetic",
        "source": {**endpoint, "channel": 1, "arbitration_id": 0x100},
        "target": {**endpoint, "channel": 2, "arbitration_id": 0x200},
        "window_seconds": 0.02,
    }


CONTEXT = {
    "capture_complete": True,
    "direction_verified": True,
    "isolated": True,
    "clock_verified": True,
    "data_frames_only_verified": True,
    "recorded_channels": [1, 2],
}


class FakeBackend:
    def __init__(self, failure=None, target=True):
        self.failure = failure
        self.target = target
        self.logging = False
        self.calls = []
        self.thread_ids = []
        self.writer = None
        self.send_started = threading.Event()
        self.replay_index = None

    def record(self, action):
        self.calls.append(action)
        self.thread_ids.append(threading.get_ident())
        if self.failure == action:
            raise RuntimeError("synthetic " + action)

    def open(self):
        self.record("open")
        return {"synthetic": True}

    def prepare_project(self, path, template):
        self.record("prepare")
        Path(path).write_bytes(b"synthetic")
        return {"path": path}

    def configure(self, config):
        self.record("configure")
        return {"synthetic": True}

    def save(self, path):
        self.record("save")

    def reload(self, path):
        self.record("reload")
        return {"synthetic": True}

    def connect(self):
        self.record("connect")

    def enable_feedback(self):
        self.record("enable_feedback")

    def start_logging(self, path):
        self.logging = True
        self.record("start_logging")
        self.writer = can.BLFWriter(path)

    def send(self, selected):
        self.send_started.set()
        self.record("send")
        now = time.time()
        self.writer(
            can.Message(
                timestamp=now,
                channel=selected.channel - 1,
                arbitration_id=selected.arbitration_id,
                is_extended_id=False,
                data=bytes.fromhex(selected.data_hex),
                is_rx=False,
            )
        )
        if self.target:
            self.writer(
                can.Message(
                    timestamp=now + 0.001,
                    channel=1,
                    arbitration_id=0x200,
                    is_extended_id=False,
                    data=b"\x99",
                    is_rx=True,
                )
            )

    def start_replay(self, path, mappings):
        self.replay_index = 0
        self.record("start_replay")

    def replay_status(self):
        return 1, 100  # 100% 仍运行，不能认定完成。

    def wait(self, duration):
        self.record("wait")
        time.sleep(duration)

    def cleanup(self):
        self.record("cleanup")
        if self.writer:
            self.writer.stop()
            self.writer = None
        self.logging = False
        self.replay_index = None
        return []

    def close(self):
        self.record("close")
        return {"cleanup_errors": []}


def create(tmp_path, backend):
    sessions = TSMasterSessions(lambda: backend)
    status = sessions.create(str(tmp_path), "test.T7z", bench(), True)
    assert status["state"] == "ready"
    return sessions, status["session_id"]


def run(sessions, session_id, **overrides):
    arguments = {
        "cases": [route()],
        "frames": [frame()],
        "replay": None,
        "baseline_seconds": 0,
        "response_seconds": 0.025,
        "timeout_seconds": 2,
        "evidence_context": CONTEXT,
        "bus_authorized": True,
        **overrides,
    }
    return sessions.run(session_id, **arguments)


def finish(sessions, session_id):
    sessions._sessions[session_id]["future"].result(timeout=5)
    return sessions.result(session_id)


def test_session_runs_on_one_thread_records_before_send_and_analyzes_after_close(
    tmp_path,
):
    backend = FakeBackend()
    sessions, session_id = create(tmp_path, backend)
    try:
        run(sessions, session_id)
        result = finish(sessions, session_id)
        assert result["results"][0]["status"] == "observed"
        assert result["results"][0]["matched_count"] == 2
        assert (
            backend.calls.index("start_logging")
            < backend.calls.index("send")
            < backend.calls.index("cleanup")
        )
        assert backend.calls.count("send") == 2
        assert len(set(backend.thread_ids)) == 1
        report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        assert report["full_diagnostic_transaction"] == "not_executed"
        assert report["analysis"]["frames_scanned"] == 4
        assert "evidence" not in sessions.status(session_id)["result"]
    finally:
        assert sessions.close(session_id)["state"] == "closed"


@pytest.mark.parametrize("failure", ["connect", "start_logging", "send", "wait"])
def test_exception_cleans_and_does_not_claim_pass(tmp_path, failure):
    backend = FakeBackend(failure=failure)
    sessions, session_id = create(tmp_path, backend)
    try:
        run(sessions, session_id)
        result = finish(sessions, session_id)
        assert result["execution"] == "interrupted"
        assert "cleanup" in backend.calls
        assert not backend.logging
        assert not any(item["status"] == "observed" for item in result["results"])
    finally:
        sessions.close(session_id)


def test_cancel_long_finite_send_preserves_incomplete_result_and_stops(tmp_path):
    backend = FakeBackend()
    sessions, session_id = create(tmp_path, backend)
    try:
        run(
            sessions,
            session_id,
            frames=[frame(count=1000, interval_ms=2)],
            timeout_seconds=5,
        )
        assert backend.send_started.wait(2)
        sessions.stop(session_id)
        result = finish(sessions, session_id)
        assert result["execution"] == "interrupted"
        assert backend.calls.count("send") < 1000
        assert result["results"][0]["status"] == "insufficient_evidence"
    finally:
        sessions.close(session_id)


def test_known_missing_target_is_not_observed_only_with_capture_confirmation(tmp_path):
    backend = FakeBackend(target=False)
    sessions, session_id = create(tmp_path, backend)
    try:
        run(sessions, session_id)
        assert finish(sessions, session_id)["results"][0]["status"] == "not_observed"
        run(sessions, session_id, evidence_context={})
        assert (
            finish(sessions, session_id)["results"][0]["status"]
            == "insufficient_evidence"
        )
    finally:
        sessions.close(session_id)


def test_validation_and_authorization_precede_connection(tmp_path):
    backend = FakeBackend()
    sessions = TSMasterSessions(lambda: backend)
    with pytest.raises(ValueError):
        sessions.create(str(tmp_path), "test.T7z", bench())
    assert backend.calls == []
    status = sessions.create(str(tmp_path), "test.T7z", bench(), True)
    session_id = status["session_id"]
    try:
        for arguments in (
            {"bus_authorized": False},
            {"frames": [frame(channel=2)]},
            {"response_seconds": 0.001},
            {"cases": [{"kind": "uds_transaction"}]},
        ):
            with pytest.raises((ValueError, KeyError)):
                run(sessions, session_id, **arguments)
        assert "connect" not in backend.calls
    finally:
        sessions.close(session_id)


def test_close_during_run_keeps_final_state_and_schedules_cleanup_on_same_thread(
    tmp_path,
):
    backend = FakeBackend()
    sessions, session_id = create(tmp_path, backend)
    run(
        sessions,
        session_id,
        frames=[frame(count=1000, interval_ms=2)],
        timeout_seconds=5,
    )
    assert backend.send_started.wait(2)
    status = sessions.close(session_id)
    assert status["state"] == "closed"
    assert backend.calls.index("cleanup") < backend.calls.index("close")
    assert len(set(backend.thread_ids)) == 1


def test_progress_100_without_completed_status_times_out_and_stops_replay(tmp_path):
    input_blf = tmp_path / "input.blf"
    with can.BLFWriter(str(input_blf)) as writer:
        writer(
            can.Message(
                timestamp=time.time(),
                arbitration_id=0x100,
                channel=0,
                data=b"\x01",
                is_extended_id=False,
            )
        )
    backend = FakeBackend()
    sessions, session_id = create(tmp_path, backend)
    replay = {
        "path": str(input_blf),
        "selections": [
            {
                "input_channel": 1,
                "logical_channel": 1,
                "arbitration_ids": [0x100],
                "directions": ["Rx"],
            }
        ],
        "start_seconds": 0,
        "end_seconds": 1,
        "max_frames": 10,
    }
    try:
        run(sessions, session_id, frames=None, replay=replay, timeout_seconds=0.1)
        result = finish(sessions, session_id)
        assert result["execution"] == "interrupted"
        assert result["error"]["type"] == "TimeoutError"
        assert backend.replay_index is None
        assert not backend.logging
    finally:
        sessions.close(session_id)
