"""显式启用的本机 COM 空白工程验证；不连接设备或发送任何报文。"""

import os
import platform

import pytest

from anda.tsmaster.backend import ComBackend, processes
from anda.tsmaster.models import Bench

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ANDA_TSMASTER_COM_SMOKE") != "1"
        or platform.system() != "Windows",
        reason="需要显式 ANDA_TSMASTER_COM_SMOKE=1；仅验证独立离线 COM 工程",
    ),
]


def test_owned_offline_project_databases_mapping_reload_and_close(tmp_path):
    assert not processes(), "已有 TSMaster 时不接管；请先关闭用户实例"
    paths = []
    for number in (1, 2):
        path = tmp_path / f"synthetic{number}.dbc"
        path.write_text(
            f"""VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ {256 * number} Example{number}: 8 ECU
 SG_ Value : 0|8@1+ (1,0) [0|255] "" ECU
""",
            encoding="ascii",
        )
        paths.append(str(path))
    common = {
        "device_type": 1,
        "device_subtype": -1,
        "device_index": 0,
        "device_name": "Virtual",
        "arbitration_kbps": 500,
        "data_kbps": 2000,
        "controller_type": 1,
        "termination": False,
    }
    config = Bench.model_validate(
        {
            "channels": [
                {
                    **common,
                    "network": "synthetic_source",
                    "logical_channel": 1,
                    "role": "source",
                    "device_channel": 0,
                    "allowed_ids": [256],
                },
                {
                    **common,
                    "network": "synthetic_target",
                    "logical_channel": 2,
                    "role": "target",
                    "device_channel": 1,
                },
            ],
            "databases": [
                {
                    "path": paths[i],
                    "network": f"synthetic_{role}",
                    "logical_channel": i + 1,
                }
                for i, role in enumerate(("source", "target"))
            ],
        }
    )
    backend = ComBackend()
    closed = None
    try:
        opened = backend.open()
        assert len(opened["owned_process_ids"]) == 1
        project = str(tmp_path / "synthetic.T7z")
        backend.prepare_project(project, None)
        configured = backend.configure(config)
        assert len(configured["databases"]) == 2
        backend.save(project)
        assert backend.reload(project)["mappings_verified"]
        backend.enable_feedback()
        assert not backend.connected
        assert not backend.logging
    finally:
        closed = backend.close()
    assert closed["cleanup_errors"] == []
    assert closed["remaining_process_ids"] == []
