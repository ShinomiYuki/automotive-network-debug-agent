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

    def search(self, query: str) -> list[dict]:
        """按报文名或信号名搜索轻量候选，不返回整个数据库。"""
        normalized_query = query.casefold()
        results = []
        for message in self.db.messages:
            message_name = message.name
            matched_signals = [
                signal.name
                for signal in message.signals
                if normalized_query in signal.name.casefold()
            ]
            message_matched = normalized_query in message_name.casefold()
            if not message_matched and not matched_signals:
                continue

            exact_signal = any(
                signal_name.casefold() == normalized_query
                for signal_name in matched_signals
            )
            exact_message = message_name.casefold() == normalized_query
            results.append(
                {
                    "arbitration_id": message.frame_id,
                    "arbitration_id_hex": f"0x{message.frame_id:X}",
                    "message_name": message_name,
                    "message_name_exact": exact_message,
                    "matched_signal_names": matched_signals,
                    "signal_name_exact": exact_signal,
                }
            )

        return sorted(
            results,
            key=lambda item: (
                not item["signal_name_exact"],
                not item["message_name_exact"],
                item["arbitration_id"],
            ),
        )

    def decode(self, arbitration_id: int, data: bytes) -> dict:
        message = self.get_message(arbitration_id)
        try:
            return message.decode(data, decode_choices=False, scaling=True)
        except DecodeError as exc:
            raise TraceDatabaseError(
                f"CAN ID 0x{arbitration_id:X} payload 无法按数据库解码: {exc}"
            ) from exc
