"""
文件用途：
- 使用标准库 array 和连续 bytearray 紧凑保存 Trace 帧。
- 为 CAN ID 查询建立轻量索引，避免每次查询扫描并重新解析 BLF。

设计说明：
- 不长期保留 can.Message 或 RawFrame 对象，降低大型 BLF 的 Python 对象开销。
- payload 按实际长度连续保存，不生成固定 64 字节磁盘副本，因此不会额外占用大量存储。
- 仅建立一份 CAN ID -> 帧序号索引；Channel 查询按紧凑数组扫描，避免为每帧重复建立多套索引。
"""

from array import array
from collections.abc import Iterator

from anda.trace.models import RawFrame

_UNKNOWN_CHANNEL = 0xFFFF
_UNKNOWN_DIRECTION = 2
_FLAG_EXTENDED = 1 << 0
_FLAG_FD = 1 << 1
_FLAG_BRS = 1 << 2
_FLAG_ESI = 1 << 3
_FLAG_ERROR = 1 << 4


class CompactFrameStore:
    """面向只追加、重复查询场景的紧凑帧存储。"""

    def __init__(self) -> None:
        self.timestamps = array("d")
        self.channels = array("H")
        self.arbitration_ids = array("I")
        self.dlcs = array("B")
        self.directions = array("B")
        self.flags = array("B")
        self.payload_offsets = array("Q", [0])
        self.payload = bytearray()
        self._id_index: dict[int, array] = {}

        self.channel_values: set[int] = set()
        self.arbitration_id_values: set[int] = set()
        self.classic_can_count = 0
        self.can_fd_count = 0
        self.error_frame_count = 0
        self.start_timestamp: float | None = None
        self.end_timestamp: float | None = None

    def __len__(self) -> int:
        return len(self.timestamps)

    def append(self, frame: RawFrame) -> None:
        """追加一帧，并只保存查询需要的紧凑字段。"""
        position = len(self.timestamps)
        self.timestamps.append(frame.timestamp)
        self.channels.append(
            frame.channel if frame.channel is not None else _UNKNOWN_CHANNEL
        )
        self.arbitration_ids.append(frame.arbitration_id)
        self.dlcs.append(min(frame.dlc, 0xFF))
        direction = (
            0 if frame.is_rx is True else 1 if frame.is_rx is False else _UNKNOWN_DIRECTION
        )
        self.directions.append(direction)

        flags = 0
        if frame.is_extended_id:
            flags |= _FLAG_EXTENDED
        if frame.is_fd:
            flags |= _FLAG_FD
        if frame.bitrate_switch:
            flags |= _FLAG_BRS
        if frame.error_state_indicator:
            flags |= _FLAG_ESI
        if frame.is_error_frame:
            flags |= _FLAG_ERROR
        self.flags.append(flags)

        self.payload.extend(frame.data)
        self.payload_offsets.append(len(self.payload))
        self._id_index.setdefault(frame.arbitration_id, array("Q")).append(position)

        if frame.channel is not None:
            self.channel_values.add(frame.channel)
        self.arbitration_id_values.add(frame.arbitration_id)
        if frame.is_fd:
            self.can_fd_count += 1
        else:
            self.classic_can_count += 1
        if frame.is_error_frame:
            self.error_frame_count += 1

        if self.start_timestamp is None or frame.timestamp < self.start_timestamp:
            self.start_timestamp = frame.timestamp
        if self.end_timestamp is None or frame.timestamp > self.end_timestamp:
            self.end_timestamp = frame.timestamp

    def matching_indices(
        self,
        arbitration_id: int | None,
        channel: int | None,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
    ) -> Iterator[int]:
        """按 ID、Channel 和绝对时间范围返回匹配帧序号。"""
        positions = (
            self._id_index.get(arbitration_id, ())
            if arbitration_id is not None
            else range(len(self))
        )
        for position in positions:
            if channel is not None and self.channel_at(position) != channel:
                continue
            timestamp = self.timestamps[position]
            if start_timestamp is not None and timestamp < start_timestamp:
                continue
            if end_timestamp is not None and timestamp > end_timestamp:
                continue
            yield position

    def channel_at(self, position: int) -> int | None:
        value = self.channels[position]
        return None if value == _UNKNOWN_CHANNEL else value

    def timestamp_at(self, position: int) -> float:
        return self.timestamps[position]

    def payload_at(self, position: int) -> bytes:
        start = self.payload_offsets[position]
        end = self.payload_offsets[position + 1]
        return bytes(self.payload[start:end])

    def frame_dict(self, position: int) -> dict:
        """仅在结果需要返回给调用方时构造单帧字典。"""
        flags = self.flags[position]
        direction = self.directions[position]
        data = self.payload_at(position)
        arbitration_id = self.arbitration_ids[position]
        return {
            "timestamp": self.timestamps[position],
            "channel": self.channel_at(position),
            "arbitration_id": arbitration_id,
            "arbitration_id_hex": f"0x{arbitration_id:X}",
            "dlc": self.dlcs[position],
            "data_hex": data.hex(" ").upper(),
            "is_extended_id": bool(flags & _FLAG_EXTENDED),
            "is_fd": bool(flags & _FLAG_FD),
            "bitrate_switch": bool(flags & _FLAG_BRS),
            "error_state_indicator": bool(flags & _FLAG_ESI),
            "is_error_frame": bool(flags & _FLAG_ERROR),
            "direction": "rx" if direction == 0 else "tx" if direction == 1 else None,
        }
