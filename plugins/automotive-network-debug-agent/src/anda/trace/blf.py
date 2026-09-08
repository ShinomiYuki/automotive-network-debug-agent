"""
文件用途：
- 负责读取 Vector BLF 文件并转换为项目内部 RawFrame。
- 当前使用 python-can 的 BLFReader。
- 本模块只负责“读”，不负责 DBC 解码和诊断结论。

Channel 约定：
- python-can 从 BLF 二进制读取后返回 0-based channel。
- 本项目对外统一为 Vector/CANoe 工程人员使用的 1-based CAN1/CAN2。
"""

from collections.abc import Iterator
from pathlib import Path

import can

from anda.common.errors import TraceInputError
from anda.trace.models import RawFrame


def iter_blf(path: str | Path) -> Iterator[RawFrame]:
    path = Path(path)
    if not path.is_file():
        raise TraceInputError(f"BLF 文件不存在: {path}")
    if path.suffix.lower() != ".blf":
        raise TraceInputError(f"Trace 输入必须是 .blf 文件: {path}")

    try:
        with can.BLFReader(str(path)) as reader:
            for msg in reader:
                data = bytes(msg.data or b"")
                raw_channel = getattr(msg, "channel", None)
                # python-can 把 BLF 中从 1 开始的 Channel 减一后返回。
                # 对外还原成 CANoe 界面一致的 1-based 编号，避免用户查询 CAN1 时传 0。
                channel = (
                    int(raw_channel) + 1 if isinstance(raw_channel, int) else None
                )
                yield RawFrame(
                    timestamp=float(msg.timestamp),
                    channel=channel,
                    arbitration_id=int(msg.arbitration_id),
                    dlc=int(getattr(msg, "dlc", len(data))),
                    data=data,
                    is_extended_id=bool(getattr(msg, "is_extended_id", False)),
                    is_fd=bool(getattr(msg, "is_fd", False)),
                    is_rx=getattr(msg, "is_rx", None),
                    bitrate_switch=bool(getattr(msg, "bitrate_switch", False)),
                    error_state_indicator=bool(
                        getattr(msg, "error_state_indicator", False)
                    ),
                    is_error_frame=bool(getattr(msg, "is_error_frame", False)),
                )
    except TraceInputError:
        raise
    except Exception as exc:
        raise TraceInputError(f"BLF 读取失败: {path.name}: {exc}") from exc
