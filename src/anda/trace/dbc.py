"""
文件用途：
- 负责加载 DBC / ARXML，并提供按报文解码信号的基础能力。
- 当前使用 cantools。

当前支持 cantools 可直接加载的 DBC 与 ARXML；不实现自定义解析器。
"""

from pathlib import Path

import cantools
from cantools.database import DecodeError

from anda.common.errors import TraceDatabaseError, TraceInputError


class NetworkDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise TraceInputError(f"数据库文件不存在: {self.path}")
        if self.path.suffix.lower() not in {".dbc", ".arxml"}:
            raise TraceInputError(f"数据库仅支持 .dbc 或 .arxml: {self.path}")
        try:
            self.db = cantools.database.load_file(str(self.path))
        except Exception as exc:
            raise TraceDatabaseError(
                f"数据库加载失败: {self.path.name}: {exc}"
            ) from exc

    def get_message(self, arbitration_id: int):
        try:
            return self.db.get_message_by_frame_id(arbitration_id)
        except KeyError as exc:
            raise TraceDatabaseError(
                f"数据库中不存在 CAN ID 0x{arbitration_id:X}"
            ) from exc

    def require_signal(self, arbitration_id: int, signal_name: str) -> None:
        message = self.get_message(arbitration_id)
        if signal_name not in {signal.name for signal in message.signals}:
            raise TraceDatabaseError(
                f"CAN ID 0x{arbitration_id:X} 中不存在信号: {signal_name}"
            )

    def decode(self, arbitration_id: int, data: bytes) -> dict:
        message = self.get_message(arbitration_id)
        try:
            return message.decode(data, decode_choices=False, scaling=True)
        except DecodeError as exc:
            raise TraceDatabaseError(
                f"CAN ID 0x{arbitration_id:X} payload 无法按数据库解码: {exc}"
            ) from exc
