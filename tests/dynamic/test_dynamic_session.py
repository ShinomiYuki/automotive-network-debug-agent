"""Dynamic Session、Runtime Policy、影子配置和发送上限的确定性测试。"""

from __future__ import annotations

from pathlib import Path
from threading import get_ident
from typing import Any, ClassVar

import pytest

from anda.common.errors import DynamicInputError, DynamicPolicyError, DynamicStateError
from anda.dynamic.pycanoe import _bounded_value
from anda.dynamic.session import DynamicSessionManager


class FakeBackend:
    instances: ClassVar[list[FakeBackend]] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.thread_ids: set[int] = set()
        self.status = {
            "connected": True,
            "canoe_version": {"full_name": "CANoe fake"},
            "measurement": "stopped",
            "animation": "stopped",
            "playback_cursor": {"seconds": 0.5},
        }
        type(self).instances.append(self)

    @classmethod
    def probe_environment(cls) -> dict[str, Any]:
        return {"backend": "fake", "backend_available": True}

    def _call(self, name: str, *args: Any) -> None:
        self.thread_ids.add(get_ident())
        self.calls.append((name, args))

    def initialize(self, spec: dict[str, Any]) -> dict[str, Any]:
        self._call("initialize", spec)
        self.spec = spec
        return dict(self.status)

    def get_status(self) -> dict[str, Any]:
        self._call("get_status")
        return dict(self.status)

    def control_measurement(self, action: str) -> dict[str, Any]:
        self._call("control_measurement", action)
        self.status["measurement"] = "running" if action == "start" else "stopped"
        return dict(self.status)

    def transmit_can_frame(self, *args: Any) -> dict[str, Any]:
        self._call("transmit_can_frame", *args)
        return {"success": True, "count": args[-2]}

    def close(self) -> dict[str, Any]:
        self._call("close")
        self.status["connected"] = False
        return {"closed": True, "cleanup_errors": []}


@pytest.fixture(autouse=True)
def _clear_fake_instances():
    FakeBackend.instances.clear()


def test_health_does_not_create_backend(tmp_path: Path):
    manager = DynamicSessionManager(FakeBackend, temp_root=tmp_path)

    health = manager.get_health()

    assert health["status"] == "available"
    assert health["backend_probe"]["backend"] == "fake"
    assert FakeBackend.instances == []


def test_arbitrary_backend_values_are_bounded():
    value = [{"text": "x" * 3_000, "values": list(range(200))}] * 150

    bounded = _bounded_value(value)

    assert len(bounded) == 100
    assert len(bounded[0]["text"]) == 2_000
    assert len(bounded[0]["values"]) == 100


def test_shadow_configuration_is_used_and_original_is_preserved(tmp_path: Path):
    configuration = tmp_path / "project.cfg"
    configuration.write_bytes(b"original configuration")
    manager = DynamicSessionManager(FakeBackend, temp_root=tmp_path / "sessions")

    created = manager.create_dynamic_session(configuration_path=str(configuration))
    session_id = created["dynamic_session_id"]
    backend = FakeBackend.instances[0]
    shadow = Path(backend.spec["configuration_path"])

    assert created["mode"] == "offline"
    assert created["configuration_origin"] == "shadow_copy"
    assert shadow.parent == configuration.parent
    assert shadow != configuration
    assert shadow.read_bytes() == configuration.read_bytes()

    closed = manager.close_dynamic_session(session_id)

    assert closed["original_configuration_unchanged"] is True
    assert closed["shadow_configuration_removed"] is True
    assert configuration.read_bytes() == b"original configuration"


def test_default_policy_blocks_active_mutation_and_only_one_session(tmp_path: Path):
    configuration = tmp_path / "project.cfg"
    configuration.write_text("cfg", encoding="utf-8")
    manager = DynamicSessionManager(FakeBackend, temp_root=tmp_path / "sessions")
    created = manager.create_dynamic_session(configuration_path=str(configuration))

    with pytest.raises(DynamicPolicyError, match="frame_injection"):
        manager.transmit_can_frame(
            created["dynamic_session_id"], 1, 0x123, "01 02"
        )
    with pytest.raises(DynamicStateError, match="已有活动 Session"):
        manager.create_dynamic_session(configuration_path=str(configuration))

    manager.close_dynamic_session(created["dynamic_session_id"])


def test_failed_creation_releases_session_reservation(tmp_path: Path):
    class FailOnceBackend(FakeBackend):
        attempts = 0

        def initialize(self, spec: dict[str, Any]) -> dict[str, Any]:
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise RuntimeError("intentional initialize failure")
            return super().initialize(spec)

    configuration = tmp_path / "project.cfg"
    configuration.write_text("cfg", encoding="utf-8")
    manager = DynamicSessionManager(
        FailOnceBackend, temp_root=tmp_path / "sessions"
    )

    with pytest.raises(RuntimeError, match="intentional initialize failure"):
        manager.create_dynamic_session(configuration_path=str(configuration))

    created = manager.create_dynamic_session(configuration_path=str(configuration))
    assert created["created"] is True
    manager.close_dynamic_session(created["dynamic_session_id"])


def test_backend_calls_stay_on_one_worker_thread(tmp_path: Path):
    configuration = tmp_path / "project.cfg"
    configuration.write_text("cfg", encoding="utf-8")
    manager = DynamicSessionManager(FakeBackend, temp_root=tmp_path / "sessions")
    created = manager.create_dynamic_session(
        configuration_path=str(configuration), mode="online"
    )
    session_id = created["dynamic_session_id"]

    manager.get_dynamic_status(session_id)
    manager.control_measurement(session_id, "start")
    manager.control_measurement(session_id, "stop")
    manager.close_dynamic_session(session_id)

    assert len(FakeBackend.instances[0].thread_ids) == 1


def test_raw_frame_limits_are_enforced_before_backend_call(tmp_path: Path):
    configuration = tmp_path / "project.cfg"
    configuration.write_text("cfg", encoding="utf-8")
    manager = DynamicSessionManager(FakeBackend, temp_root=tmp_path / "sessions")
    created = manager.create_dynamic_session(
        configuration_path=str(configuration),
        runtime_policy=["observation", "frame_injection"],
    )
    session_id = created["dynamic_session_id"]

    with pytest.raises(DynamicInputError, match="最多 8 字节"):
        manager.transmit_can_frame(session_id, 1, 0x123, "00" * 9)
    with pytest.raises(DynamicInputError, match="至少为 10"):
        manager.transmit_can_frame(
            session_id, 1, 0x123, "00", count=2, interval_ms=1
        )
    with pytest.raises(DynamicInputError, match="不能超过 10 秒"):
        manager.transmit_can_frame(
            session_id, 1, 0x123, "00", count=100, interval_ms=102
        )

    result = manager.transmit_can_frame(
        session_id,
        1,
        0x18DAF110,
        "00" * 64,
        is_extended=True,
        is_fd=True,
        brs=True,
        count=2,
        interval_ms=10,
    )
    assert result == {"success": True, "count": 2}
    manager.close_dynamic_session(session_id)


def test_observe_bus_window_reuses_trace_core(trace_files, tmp_path: Path):
    blf, database = trace_files
    manager = DynamicSessionManager(FakeBackend, temp_root=tmp_path / "sessions")
    created = manager.create_dynamic_session(
        blf_path=str(blf), database_paths=[str(database)]
    )
    session_id = created["dynamic_session_id"]
    trace_id = created["trace"]["trace_id"]
    status = manager._trace.get_load_status(trace_id, wait_seconds=5)
    assert status["status"] == "ready"

    observed = manager.observe_bus_window(
        session_id,
        start_timestamp=1_700_000_000.0,
        end_timestamp=1_700_000_000.03,
        arbitration_id=0x100,
        limit=10,
    )

    assert observed["window_basis"] == "explicit_range"
    assert observed["returned_count"] == 4
    assert set(observed["frames"][0]) == {
        "timestamp",
        "bus",
        "channel",
        "direction",
        "arbitration_id",
        "arbitration_id_hex",
        "frame_type",
        "dlc",
        "payload",
    }
    manager.close_dynamic_session(session_id)
