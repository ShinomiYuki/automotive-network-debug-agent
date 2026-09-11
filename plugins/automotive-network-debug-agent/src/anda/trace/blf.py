"""
文件用途：
- 读取 Vector BLF 中的 CAN、CAN FD 与 LIN 帧并转换为内部 RawFrame。
- 只解包调查需要的总线对象，跳过文本等无关 BLF 对象。

Channel 约定：vblf 保留 BLF 的 1-based Channel，本项目直接对外使用同一编号。
"""

import os
import struct
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC
from io import BytesIO
from pathlib import Path

from can.util import dlc2len
from vblf.can import (
    CanErrorFrame,
    CanErrorFrameExt,
    CanFdMessage64,
    CanMessage,
    CanMessage2,
)
from vblf.constants import ObjFlags, ObjType
from vblf.general import ObjectHeader
from vblf.lin import LinMessage, LinMessage2
from vblf.reader import (
    OBJ_SIGNATURE,
    OBJ_SIGNATURE_SIZE,
    BlfReader,
    LogContainer,
    ObjectHeaderBase,
)

from anda.common.errors import TraceInputError
from anda.trace.models import RawFrame

ProgressCallback = Callable[[int, int, int | None], None]

_CAN_EXTENDED_MASK = 0x80000000
_CAN_ID_MASK = 0x1FFFFFFF
_DIRECTION_TX = 0x01
_OLD_FD_FRAME = 0x01
_OLD_FD_BRS = 0x02
_OLD_FD_ESI = 0x04
_FD64_FRAME = 0x1000
_FD64_BRS = 0x2000
_FD64_ESI = 0x4000
_PROGRESS_INTERVAL = 100_000


def read_blf_metadata(path: str | Path) -> dict:
    """读取 BLF 文件头，并明确时间戳能与不能证明的语义。"""
    path = Path(path)
    if not path.is_file():
        raise TraceInputError(f"BLF 文件不存在: {path}")
    try:
        with BlfReader(str(path)) as reader:
            statistics = reader.file_statistics
            start = (
                statistics.measurement_start_time.to_datetime()
                .replace(tzinfo=UTC)
                .timestamp()
            )
            try:
                last = (
                    statistics.last_object_time.to_datetime()
                    .replace(tzinfo=UTC)
                    .timestamp()
                )
            except (AttributeError, OSError, OverflowError, ValueError):
                last = None
            application = getattr(
                statistics.application_id, "name", str(statistics.application_id)
            )
            return {
                "measurement_start_timestamp": start,
                "last_object_header_timestamp": last,
                "application_id": application,
                "application_version": (
                    f"{statistics.application_major}."
                    f"{statistics.application_minor}."
                    f"{statistics.application_build}"
                ),
                "api_number": statistics.api_number,
                "declared_object_count": statistics.object_count,
                "timestamp_reference": {
                    "value": "BLF_MEASUREMENT_START_PLUS_OBJECT_OFFSET",
                    "calculation": (
                        "FileStatistics.measurement_start_time + "
                        "ObjectHeader.object_time_stamp × ObjectHeader resolution"
                    ),
                    "object_resolution": "TIME_TEN_MICS_OR_TIME_ONE_NANS",
                    "clock_domain": "BLF_LOGGER_CLOCK",
                    "capture_point": "UNKNOWN",
                    "ecu_internal_send_time": "UNKNOWN",
                    "certainty": "PARTIAL",
                    "warning": (
                        "该显示时间来自 BLF 记录器对象头；未提供接口采集点配置时，"
                        "不能等同于 ECU 内部发送时刻。"
                    ),
                },
            }
    except Exception as exc:
        raise TraceInputError(f"BLF 文件头读取失败: {path.name}: {exc}") from exc


@dataclass(slots=True)
class _CompatCanFdMessage:
    """兼容 Vector/python-can 实际写出的 116 字节 CAN_FD_MESSAGE。"""

    header: ObjectHeader
    channel: int
    flags: int
    dlc: int
    frame_id: int
    frame_length: int
    arb_bit_count: int
    canfd_flags: int
    valid_data_bytes: int
    data: bytes

    _FORMAT = struct.Struct("<HBBLLBBB5x64s")

    @classmethod
    def unpack(cls, data: bytes) -> "_CompatCanFdMessage":
        # vblf 0.3.1 按 120 字节结构解包，python-can 4.6.1 写出的标准对象是
        # 32 字节 ObjectHeader + 84 字节负载。这里按 Vector/python-can 布局读取，
        # 同时允许对象末尾存在额外填充字节。
        header = ObjectHeader.unpack(data)
        values = cls._FORMAT.unpack_from(data, ObjectHeader.SIZE)
        return cls(header, *values)


_SUPPORTED_OBJECTS = {
    ObjType.CAN_MESSAGE: CanMessage,
    ObjType.CAN_MESSAGE2: CanMessage2,
    ObjType.CAN_FD_MESSAGE: _CompatCanFdMessage,
    ObjType.CAN_FD_MESSAGE_64: CanFdMessage64,
    ObjType.CAN_ERROR: CanErrorFrame,
    ObjType.CAN_ERROR_EXT: CanErrorFrameExt,
    ObjType.LIN_MESSAGE: LinMessage,
    ObjType.LIN_MESSAGE2: LinMessage2,
}


class _BusObjectReader(BlfReader):
    """只解包受支持总线对象的 vblf Reader。"""

    def __init__(self, file, progress_callback: ProgressCallback | None = None):
        self._progress_callback = progress_callback
        self._scanned_objects = 0
        self._indexed_frames = 0
        super().__init__(file)

    def mark_indexed(self) -> None:
        self._indexed_frames += 1

    def report_progress(self, *, final: bool = False) -> None:
        if self._progress_callback is None:
            return
        if final or self._scanned_objects % _PROGRESS_INTERVAL == 0:
            self._progress_callback(
                self._scanned_objects,
                self._indexed_frames,
                self.file_statistics.object_count or None,
            )

    def _generate_objects(self, stream):
        while True:
            signature = stream.read(OBJ_SIGNATURE_SIZE)
            if len(signature) != OBJ_SIGNATURE_SIZE:
                self._incomplete_data = signature
                break
            if signature != OBJ_SIGNATURE:
                stream.seek(1 - OBJ_SIGNATURE_SIZE, os.SEEK_CUR)
                continue

            header_data = signature + stream.read(
                ObjectHeaderBase.SIZE - OBJ_SIGNATURE_SIZE
            )
            if len(header_data) < ObjectHeaderBase.SIZE:
                self._incomplete_data = header_data
                break
            header = ObjectHeaderBase.unpack(header_data)
            object_data = header_data + stream.read(
                header.object_size - ObjectHeaderBase.SIZE
            )
            if len(object_data) < header.object_size:
                self._incomplete_data = object_data
                break

            if header.object_type == ObjType.LOG_CONTAINER:
                container = LogContainer.unpack(object_data)
                uncompressed = (
                    zlib.decompress(container.data)
                    if self.file_statistics.compression_level > 0
                    else container.data
                )
                uncompressed = self._incomplete_data + uncompressed
                self._incomplete_data = b""
                yield from self._generate_objects(BytesIO(uncompressed))
                continue

            self._scanned_objects += 1
            self.report_progress()
            object_class = _SUPPORTED_OBJECTS.get(header.object_type)
            if object_class is not None:
                yield object_class.unpack(object_data)


def iter_blf(
    path: str | Path,
    progress_callback: ProgressCallback | None = None,
) -> Iterator[RawFrame]:
    path = Path(path)
    if not path.is_file():
        raise TraceInputError(f"BLF 文件不存在: {path}")
    if path.suffix.lower() != ".blf":
        raise TraceInputError(f"Trace 输入必须是 .blf 文件: {path}")

    try:
        with _BusObjectReader(str(path), progress_callback) as reader:
            start = (
                reader.file_statistics.measurement_start_time.to_datetime()
                .replace(tzinfo=UTC)
                .timestamp()
            )
            for item in reader:
                frame = _to_raw_frame(item, start)
                reader.mark_indexed()
                yield frame
            reader.report_progress(final=True)
    except TraceInputError:
        raise
    except Exception as exc:
        raise TraceInputError(f"BLF 读取失败: {path.name}: {exc}") from exc


def _timestamp(item, start_timestamp: float) -> float:
    flags = item.header.object_flags
    factor = 1e-5 if flags & ObjFlags.TIME_TEN_MICS else 1e-9
    return start_timestamp + item.header.object_time_stamp * factor


def _to_raw_frame(item, start_timestamp: float) -> RawFrame:
    timestamp = _timestamp(item, start_timestamp)
    if isinstance(item, (CanMessage, CanMessage2)):
        frame_id = item.frame_id
        return RawFrame(
            timestamp=timestamp,
            channel=item.channel,
            arbitration_id=frame_id & _CAN_ID_MASK,
            dlc=item.dlc,
            data=item.data[: item.dlc],
            is_extended_id=bool(frame_id & _CAN_EXTENDED_MASK),
            is_fd=False,
            is_rx=not bool(item.flags & _DIRECTION_TX),
        )
    if isinstance(item, _CompatCanFdMessage):
        frame_id = item.frame_id
        flags = int(item.canfd_flags)
        return RawFrame(
            timestamp=timestamp,
            channel=item.channel,
            arbitration_id=frame_id & _CAN_ID_MASK,
            dlc=dlc2len(item.dlc),
            data=item.data[: item.valid_data_bytes],
            is_extended_id=bool(frame_id & _CAN_EXTENDED_MASK),
            is_fd=bool(flags & _OLD_FD_FRAME),
            is_rx=not bool(item.flags & _DIRECTION_TX),
            bitrate_switch=bool(flags & _OLD_FD_BRS),
            error_state_indicator=bool(flags & _OLD_FD_ESI),
        )
    if isinstance(item, CanFdMessage64):
        frame_id = item.frame_id
        flags = int(item.flags)
        data = item.data[: item.valid_data_bytes].ljust(item.valid_data_bytes, b"\x00")
        return RawFrame(
            timestamp=timestamp,
            channel=item.channel,
            arbitration_id=frame_id & _CAN_ID_MASK,
            dlc=dlc2len(item.dlc),
            data=data,
            is_extended_id=bool(frame_id & _CAN_EXTENDED_MASK),
            is_fd=bool(flags & _FD64_FRAME),
            is_rx=not bool(item.dir),
            bitrate_switch=bool(flags & _FD64_BRS),
            error_state_indicator=bool(flags & _FD64_ESI),
        )
    if isinstance(item, CanErrorFrameExt):
        return RawFrame(
            timestamp=timestamp,
            channel=item.channel,
            arbitration_id=item.frame_id & _CAN_ID_MASK,
            dlc=item.dlc,
            data=item.data[: item.dlc],
            is_extended_id=bool(item.frame_id & _CAN_EXTENDED_MASK),
            is_fd=False,
            is_rx=None,
            is_error_frame=True,
        )
    if isinstance(item, CanErrorFrame):
        return RawFrame(
            timestamp=timestamp,
            channel=item.channel,
            arbitration_id=0,
            dlc=0,
            data=b"",
            is_extended_id=False,
            is_fd=False,
            is_rx=None,
            is_error_frame=True,
        )
    if isinstance(item, LinMessage):
        return RawFrame(
            timestamp=timestamp,
            channel=item.channel,
            arbitration_id=item.id,
            dlc=item.dlc,
            data=item.data[: item.dlc],
            is_extended_id=False,
            is_fd=False,
            is_rx=not bool(item.dir),
            bus_type="lin",
        )

    descriptor = item.lin_timestamp_event.lin_msg_descr_event
    event = descriptor.lin_synch_field_event.lin_bus_event
    return RawFrame(
        timestamp=timestamp,
        channel=event.channel,
        arbitration_id=descriptor.id,
        dlc=descriptor.dlc,
        data=item.data[: descriptor.dlc],
        is_extended_id=False,
        is_fd=False,
        is_rx=not bool(item.direction),
        bus_type="lin",
    )
