"""
测试夹具说明：
- 运行时生成小型真实 BLF 与 DBC，端到端经过 python-can 和 cantools。
- 测试数据完全合成，不包含公司日志或工程数据库。
"""

from pathlib import Path

import can
import pytest


@pytest.fixture()
def trace_files(tmp_path: Path) -> tuple[Path, Path]:
    dbc_path = tmp_path / "test.dbc"
    dbc_path.write_text(
        """VERSION ""

NS_ :

BS_:

BU_: ECU

BO_ 256 EngineData: 8 ECU
 SG_ EngineSpeed : 0|16@1+ (0.125,0) [0|8000] "rpm" ECU
 SG_ VehicleSpeed : 16|16@1+ (0.01,0) [0|250] "km/h" ECU
""",
        encoding="ascii",
    )

    base = 1_700_000_000.0
    messages: list[can.Message] = []
    for index in range(220):
        speed = 1000 + (index % 5) * 100
        raw_speed = int(speed / 0.125)
        messages.append(
            can.Message(
                timestamp=base + index * 0.01,
                arbitration_id=0x100,
                channel=0,
                data=raw_speed.to_bytes(2, "little") + bytes(6),
                is_extended_id=False,
            )
        )

    for timestamp in (base + 0.001, base + 0.016, base + 0.041):
        messages.append(
            can.Message(
                timestamp=timestamp,
                arbitration_id=0x200,
                channel=1,
                data=bytes.fromhex("01 02 03 04"),
                is_extended_id=False,
            )
        )

    messages.extend(
        [
            can.Message(
                timestamp=base + 0.005,
                arbitration_id=0x300,
                channel=0,
                data=bytes(range(16)),
                is_extended_id=False,
                is_fd=True,
                bitrate_switch=True,
            ),
            can.Message(
                timestamp=base + 0.0205,
                arbitration_id=0x400,
                channel=0,
                data=bytes.fromhex("AA"),
                is_extended_id=False,
            ),
        ]
    )

    blf_path = tmp_path / "test.blf"
    with can.BLFWriter(str(blf_path)) as writer:
        for message in sorted(messages, key=lambda item: item.timestamp):
            writer(message)

    return blf_path, dbc_path
