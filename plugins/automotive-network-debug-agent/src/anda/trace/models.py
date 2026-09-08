"""
文件用途：
- 定义 Trace Core 内部使用的数据模型。
- 这些模型描述原始 CAN/CAN FD 帧、日志摘要与时序结果。

RawFrame 仅用于 BLF 读取边界；Trace Session 会立即把字段写入紧凑列式存储，
不会长期保留逐帧 Python 对象。
"""

from dataclasses import dataclass


@dataclass(slots=True)
class RawFrame:
    timestamp: float
    channel: int | None
    arbitration_id: int
    dlc: int
    data: bytes
    is_extended_id: bool
    is_fd: bool
    is_rx: bool | None
    bitrate_switch: bool = False
    error_state_indicator: bool = False
    is_error_frame: bool = False
