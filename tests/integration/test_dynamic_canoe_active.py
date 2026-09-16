"""显式开启的 CANoe Active Smoke；Online 用例保留 cfg 的真实硬件绑定。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from anda.dynamic.session import DynamicSessionManager

_VIEW_ONLY_DETECTED = False

pytestmark = pytest.mark.skipif(
    os.environ.get("ANDA_CANOE_ACTIVE_SMOKE") != "1",
    reason="仅在明确设置 ANDA_CANOE_ACTIVE_SMOKE=1 时运行 CANoe 主动验收",
)

CAPL_BRIDGE = r"""
long ANDA_HexNibble(char value)
{
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'A' && value <= 'F') return value - 'A' + 10;
  if (value >= 'a' && value <= 'f') return value - 'a' + 10;
  return -1;
}

long ANDA_DlcFromLength(long payloadLength)
{
  if (payloadLength <= 8) return payloadLength;
  if (payloadLength <= 12) return 9;
  if (payloadLength <= 16) return 10;
  if (payloadLength <= 20) return 11;
  if (payloadLength <= 24) return 12;
  if (payloadLength <= 32) return 13;
  if (payloadLength <= 48) return 14;
  if (payloadLength <= 64) return 15;
  return -1;
}

long ANDA_TransmitCanFrame(long channel, long arbitrationId, long flags, char dataHex[])
{
  long index;
  long high;
  long low;
  long payloadLength;
  long dlc;

  if (channel < 1 || channel > 255) return 0;
  if ((flags & 4) != 0 && (flags & 2) == 0) return 0;
  payloadLength = strlen(dataHex) / 2;
  dlc = ANDA_DlcFromLength(payloadLength);
  if (dlc < 0) return 0;
  if ((flags & 2) == 0 && payloadLength > 8) return 0;
  if ((flags & 1) != 0)
  {
    ANDA_RawTx.id = mkExtId(arbitrationId);
  }
  else
  {
    ANDA_RawTx.id = arbitrationId;
  }
  ANDA_RawTx.CAN = channel;
  ANDA_RawTx.FDF = (flags & 2) / 2;
  ANDA_RawTx.BRS = (flags & 4) / 4;
  ANDA_RawTx.dlc = dlc;
  for (index = 0; index < payloadLength; index++)
  {
    high = ANDA_HexNibble(dataHex[index * 2]);
    low = ANDA_HexNibble(dataHex[index * 2 + 1]);
    if (high < 0 || low < 0) return 0;
    ANDA_RawTx.byte(index) = high * 16 + low;
  }
  output(ANDA_RawTx);
  return 1;
}

on message 0x555
{
  @demo::level_two_1::sys_var2 = this.byte(0);
  write("ANDA_RX_555=%d", this.byte(0));
}
"""


def _copy_safe_assets(tmp_path: Path, *, include_capl_bridge: bool = True) -> Path:
    source_value = os.environ.get("ANDA_CANOE_SMOKE_ASSET_ROOT")
    if not source_value:
        pytest.skip("未提供 ANDA_CANOE_SMOKE_ASSET_ROOT")
    source = Path(source_value).resolve()
    required = [
        source / "demo_dev.cfg",
        source / "demo_test_setup.cfg",
        source / "Nodes" / "system_variables.can",
        source / "Logs" / "demo_log.blf",
    ]
    if not all(path.is_file() for path in required):
        pytest.skip("安全演示资产不完整")
    target = tmp_path / "canoe-safe-assets"
    shutil.copytree(source, target)
    if include_capl_bridge:
        capl = target / "Nodes" / "system_variables.can"
        existing = capl.read_text(encoding="cp1252")
        marker = "variables\n{"
        assert marker in existing
        existing = existing.replace(marker, marker + "\n  message *ANDA_RawTx;", 1)
        capl.write_text(existing + CAPL_BRIDGE, encoding="cp1252")
    return target


def test_canoe_active_online_hardware_smoke(tmp_path: Path):
    global _VIEW_ONLY_DETECTED
    assets = _copy_safe_assets(tmp_path)
    manager = DynamicSessionManager(temp_root=tmp_path / "sessions")
    created = manager.create_dynamic_session(
        configuration_path=str(assets / "demo_dev.cfg"),
        mode="online",
        instance_mode="managed",
        license_confirmed=True,
        runtime_policy=[
            "observation",
            "measurement",
            "signal_write",
            "variable_write",
            "capl_call",
            "frame_injection",
            "replay",
            "logging",
        ],
        raw_frame_capl_function="ANDA_TransmitCanFrame",
    )
    session_id = created["dynamic_session_id"]
    try:
        if created["backend"]["license_view_only"]:
            _VIEW_ONLY_DETECTED = True
            pytest.skip("CANoe 当前为 View Only，Active Smoke pending")
        assert created["owns_canoe_process"] is True
        assert created["can_quit"] is True
        compiled = manager.compile_capl(session_id)
        assert compiled["success"] is True, json.dumps(
            compiled, ensure_ascii=False, default=str
        )
        manager.control_measurement(session_id, "start")
        assert (
            manager.wait_runtime_event(session_id, "measurement_started", 10)[
                "observed"
            ]
            is True
        )

        signal_before = manager.get_signal(
            session_id, "CAN", 1, "LightState", "FlashLight"
        )["value"]
        manager.set_signal(session_id, "CAN", 1, "LightState", "FlashLight", 1)
        assert (
            manager.get_signal(session_id, "CAN", 1, "LightState", "FlashLight")[
                "value"
            ]
            == 1
        )
        manager.set_signal(
            session_id,
            "CAN",
            1,
            "LightState",
            "FlashLight",
            signal_before,
        )

        variable_name = "demo::level_two_1::sys_var2"
        variable_before = manager.get_variable(session_id, "system", variable_name)[
            "value"
        ]
        manager.set_variable(session_id, "system", variable_name, 20)
        assert manager.get_variable(session_id, "system", variable_name)["value"] == 20
        manager.set_variable(session_id, "system", variable_name, variable_before)

        assert (
            manager.call_capl_function(session_id, "addition_function", [100, 200])[
                "return_value"
            ]
            == 300
        )

        manager.transmit_can_frame(session_id, 1, 0x555, "A5 5A")
        assert (
            manager.get_variable(session_id, "system", variable_name)["value"] == 0xA5
        )
        manager.set_variable(session_id, "system", variable_name, variable_before)

        manager.configure_replay(
            session_id, "DemoReplayBlock", str(assets / "Logs" / "demo_log.blf")
        )
        assert (
            manager.control_replay(session_id, "DemoReplayBlock", "start")["state"]
            == "running"
        )
        assert (
            manager.control_replay(session_id, "DemoReplayBlock", "stop")["state"]
            == "stopped"
        )

        log_path = tmp_path / "active-smoke.blf"
        configured_log = manager.manage_logging(
            session_id, "configure", str(log_path), None
        )
        manager.manage_logging(session_id, "start", None, configured_log["full_name"])
        manager.manage_logging(session_id, "stop", None, configured_log["full_name"])
    finally:
        closed = manager.close_dynamic_session(session_id)
        assert closed["closed"] is True


def test_canoe_active_test_module_smoke(tmp_path: Path):
    if _VIEW_ONLY_DETECTED:
        pytest.skip("本轮已确认 CANoe 为 View Only，Test Module Smoke pending")
    assets = _copy_safe_assets(tmp_path, include_capl_bridge=False)
    manager = DynamicSessionManager(temp_root=tmp_path / "test-sessions")
    created = manager.create_dynamic_session(
        configuration_path=str(assets / "demo_test_setup.cfg"),
        mode="online",
        instance_mode="managed",
        license_confirmed=True,
        runtime_policy=["observation", "measurement", "test"],
    )
    session_id = created["dynamic_session_id"]
    try:
        if created["backend"]["license_view_only"]:
            pytest.skip("CANoe 当前为 View Only，Test Module Smoke pending")
        compiled = manager.compile_capl(session_id)
        assert compiled["success"] is True, json.dumps(
            compiled, ensure_ascii=False, default=str
        )
        manager.control_measurement(session_id, "start")
        listed = manager.list_tests(session_id)
        assert listed["module_count"] >= 1
        module = listed["environments"][0]["modules"][0]["name"]
        started = manager.start_test(session_id, "module", module)
        assert started["started_module_count"] == 1
        manager.wait_runtime_event(session_id, "test_stopped", 30)
        assert manager.get_test_result(session_id, module)["returned_count"] == 1
    finally:
        closed = manager.close_dynamic_session(session_id)
        assert closed["closed"] is True
