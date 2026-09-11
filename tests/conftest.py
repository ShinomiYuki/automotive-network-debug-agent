"""
测试夹具说明：
- 运行时生成小型真实 BLF 与 DBC，端到端经过 python-can 和 cantools。
- 测试数据完全合成，不包含公司日志或工程数据库。
"""

from pathlib import Path

import can
import pytest
from vblf.can import CanMessage
from vblf.constants import ObjFlags, ObjType
from vblf.general import ObjectHeader
from vblf.lin import LinMessage
from vblf.writer import BlfWriter


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


@pytest.fixture()
def lin_trace_files(tmp_path: Path) -> tuple[Path, Path]:
    """生成包含 LIN 0x2A 的真实 BLF 对象和最小 LDF。"""
    ldf_path = tmp_path / "test.ldf"
    ldf_path.write_text(
        """LIN_description_file;
LIN_protocol_version = "2.1";
LIN_language_version = "2.1";
LIN_speed = 19.2 kbps;

Nodes {
  Master: Master, 5 ms, 0.1 ms;
  Slaves: Slave;
}

Signals {
  TimeoutStatus: 8, 0, Master, Slave;
}

Frames {
  LinStatus: 42, Master, 8 {
    TimeoutStatus, 0;
  }
}

Node_attributes {
  Slave {
    LIN_protocol = "2.1";
    configured_NAD = 0x01;
    product_id = 0x0, 0x0, 0;
    P2_min = 50 ms;
    ST_min = 0 ms;
    N_As_timeout = 1000 ms;
    N_Cr_timeout = 1000 ms;
    configurable_frames {
      LinStatus;
    }
  }
}
""",
        encoding="ascii",
    )

    header = ObjectHeader.new(
        ObjectHeader.SIZE + LinMessage._FORMAT.size,
        ObjType.LIN_MESSAGE,
        ObjFlags.TIME_ONE_NANS,
        0,
        1_000_000,
    )
    message = LinMessage(
        header=header,
        channel=8,
        id=0x2A,
        dlc=8,
        data=bytes.fromhex("7F 00 00 00 00 00 00 00"),
        fsm_id=0,
        fsm_state=0,
        header_time=0,
        full_time=0,
        crc=0,
        dir=0,
        reserved=bytes(5),
    )
    blf_path = tmp_path / "lin.blf"
    with BlfWriter(blf_path) as writer:
        writer.write(message)

    return blf_path, ldf_path


@pytest.fixture()
def routed_timeout_files(tmp_path: Path) -> tuple[Path, Path]:
    """生成一段 CAN 停止后 LIN8/LIN9 分别晚 1/2 帧切换 timeout 的 BLF。"""
    ldf_path = tmp_path / "timeout.ldf"
    ldf_path.write_text(
        """LIN_description_file;
LIN_protocol_version = "2.1";
LIN_language_version = "2.1";
LIN_speed = 19.2 kbps;
Nodes { Master: Master, 5 ms, 0.1 ms; Slaves: Slave; }
Signals { TimeoutStatus: 8, 63, Master, Slave; }
Frames { LinStatus: 42, Master, 8 { TimeoutStatus, 0; } }
Node_attributes {
  Slave {
    LIN_protocol = "2.1";
    configured_NAD = 0x01;
    product_id = 0x0, 0x0, 0;
    P2_min = 50 ms;
    ST_min = 0 ms;
    N_As_timeout = 1000 ms;
    N_Cr_timeout = 1000 ms;
    configurable_frames { LinStatus; }
  }
}
""",
        encoding="ascii",
    )

    objects = []
    for milliseconds in (0, 100, 800):
        objects.append(
            CanMessage.new(
                ObjFlags.TIME_ONE_NANS,
                milliseconds * 1_000_000,
                1,
                0,
                8,
                0x207,
                bytes.fromhex("3F 00 00 00 00 00 00 00"),
            )
        )
    target_values = {
        8: [(580, 0x3F), (600, 0x3F), (620, 0x00), (820, 0x3F)],
        9: [(580, 0x3F), (600, 0x3F), (620, 0x3F), (640, 0x00), (840, 0x3F)],
    }
    for channel, samples in target_values.items():
        for milliseconds, value in samples:
            header = ObjectHeader.new(
                ObjectHeader.SIZE + LinMessage._FORMAT.size,
                ObjType.LIN_MESSAGE,
                ObjFlags.TIME_ONE_NANS,
                0,
                milliseconds * 1_000_000,
            )
            objects.append(
                LinMessage(
                    header=header,
                    channel=channel,
                    id=0x2A,
                    dlc=8,
                    data=bytes([value]) + bytes(7),
                    fsm_id=0,
                    fsm_state=0,
                    header_time=0,
                    full_time=0,
                    crc=0,
                    dir=0,
                    reserved=bytes(5),
                )
            )
    blf_path = tmp_path / "routed-timeout.blf"
    with BlfWriter(blf_path) as writer:
        for item in sorted(objects, key=lambda value: value.header.object_time_stamp):
            writer.write(item)
    return blf_path, ldf_path
