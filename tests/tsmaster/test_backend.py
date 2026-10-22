"""COM void/out 语义、帧编码与清理状态的模拟验证。"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from anda.tsmaster.backend import MAPPING_FIELDS, ComBackend
from anda.tsmaster.models import Bench, Frame


@pytest.fixture()
def automation(monkeypatch):
    client = ModuleType("win32com.client")
    client.Record = lambda *_: SimpleNamespace()
    client.VARIANT = lambda _, data: data
    package = ModuleType("win32com")
    package.client = client
    pythoncom = ModuleType("pythoncom")
    pythoncom.VT_ARRAY, pythoncom.VT_I1 = 0x2000, 16
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    backend = ComBackend()
    monkeypatch.setattr(backend, "assert_owner", lambda: None)
    backend.app = SimpleNamespace()
    return backend


@pytest.mark.parametrize("length,expected_dlc", [(8, 8), (12, 9), (64, 15)])
def test_com_canfd_uses_encoded_dlc_zero_based_index_and_signed_payload(
    automation, length, expected_dlc
):
    received = []
    automation.com = SimpleNamespace(transmit_canfd_async=received.append)
    selected = Frame(
        channel=3,
        arbitration_id=0x123,
        is_extended_id=False,
        is_fd=True,
        bitrate_switch=True,
        data_hex=(b"\xff" * length).hex(),
        count=1,
        interval_ms=1,
    )
    automation.send(selected)
    assert received[0].FIdxChn == 2
    assert received[0].FDLC == expected_dlc
    assert bytes(v & 255 for v in received[0].FDatas[:length]) == b"\xff" * length
    assert received[0].FIsBRS


def test_void_logging_is_not_interpreted_as_c_error_code_and_failed_stop_stays_active(
    automation,
):
    automation.com = SimpleNamespace(
        start_logging=lambda path: None,
        stop_logging=lambda: (_ for _ in ()).throw(RuntimeError("stop failed")),
    )
    automation.app.disconnect = lambda: None
    automation.app.get_system_var_generic = lambda _: (True, "0")
    automation.start_logging("synthetic.blf")
    assert automation.logging
    errors = automation.cleanup()
    assert errors[0]["operation"] == "stop_logging"
    assert automation.logging
    assert not automation.connected


def test_replay_uses_out_index_zero_accepts_void_and_verifies_readback(automation):
    calls = []
    config = ("ANDA_source_only", "source.blf", False, False, 0, 0, True, True, "3,0,0")
    automation.log = SimpleNamespace(
        add_online_replay_config=lambda _: 0,
        set_online_replay_config=lambda *args: calls.append(args),
        get_online_replay_config=lambda _: config,
        get_online_replay_status=lambda _: (0, 0),
        start_online_replay=lambda index: calls.append(index),
    )
    automation.start_replay("source.blf", "3,0,0")
    assert automation.replay_index == 0
    assert calls[0] == (0, *config)
    assert calls[-1] == 0
    automation.log.get_online_replay_config = lambda _: (*config[:-1], "3,2,0")
    with pytest.raises(RuntimeError, match="读回不符"):
        automation.start_replay("source.blf", "3,0,0")


def test_stop_replay_must_leave_running_state_before_resource_is_deleted(automation):
    deleted = []
    automation.replay_index = 0
    automation.log = SimpleNamespace(
        stop_online_replay=lambda _: None,
        get_online_replay_status=lambda _: (1, 100),
        del_online_replay_config=deleted.append,
    )
    automation.app.disconnect = lambda: None
    automation.app.get_system_var_generic = lambda _: (True, "0")
    errors = automation.cleanup()
    assert errors[0]["operation"] == "stop_replay"
    assert automation.replay_index == 0
    assert deleted == []


def test_com_mapping_void_success_and_database_out_id_zero_are_valid(
    automation, tmp_path
):
    dbc = tmp_path / "synthetic.dbc"
    dbc.write_text('VERSION ""', encoding="ascii")
    common = {
        "device_type": 3,
        "device_subtype": 8,
        "device_index": 0,
        "device_name": "Synthetic",
        "arbitration_kbps": 500,
        "data_kbps": 2000,
        "controller_type": 1,
        "termination": False,
    }
    bench = Bench.model_validate(
        {
            "channels": [
                {
                    **common,
                    "network": "source",
                    "logical_channel": 1,
                    "device_channel": 0,
                    "role": "source",
                    "allowed_ids": [256],
                },
                {
                    **common,
                    "network": "target",
                    "logical_channel": 2,
                    "device_channel": 1,
                    "role": "target",
                },
            ],
            "databases": [
                {"path": str(dbc), "network": "source", "logical_channel": 1}
            ],
        }
    )
    records = {}
    configured_rates = []
    automation.quiesce = lambda: None
    automation.app = SimpleNamespace(
        get_application_list=lambda: "TSMaster",
        set_current_application=lambda _: None,
        get_current_application=lambda: "TSMaster",
        set_can_channel_count=lambda _: None,
        set_lin_channel_count=lambda _: None,
        get_can_channel_count=lambda: 2,
        del_mapping=lambda *_: None,
        set_mapping=lambda record: (
            records.setdefault(record.FAppChannelIndex, record) and None
        ),
        get_mapping=lambda _, index: records[index],
        configure_baudrate_canfd=lambda *args: configured_rates.append(args),
    )
    automation.db = SimpleNamespace(
        load_can_db=lambda *_: 0, get_can_db_count=lambda: 1, get_can_db_id=lambda _: 0
    )
    result = automation.configure(bench)
    assert result["databases"][0]["database_id"] == 0
    assert records[1].FHWChannelIndex == 1
    assert set(vars(records[0])) == set(MAPPING_FIELDS)
    assert configured_rates[1] == (1, 500, 2000, 1, 0, False)
