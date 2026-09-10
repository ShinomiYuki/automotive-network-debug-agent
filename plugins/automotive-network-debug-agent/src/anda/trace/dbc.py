"""
文件用途：
- 加载一个或多个 DBC、ARXML、LDF 文件。
- 为 CAN/CAN FD 与 LIN 提供数据库搜索和确定性信号解码。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cantools
import ldfparser
from cantools.database import DecodeError

from anda.common.errors import TraceDatabaseError, TraceInputError

DATABASE_EXTENSIONS = {".dbc", ".arxml", ".ldf"}


@dataclass(frozen=True, slots=True)
class DatabaseMessage:
    path: Path
    bus_type: str
    frame_id: int
    name: str
    signal_names: tuple[str, ...]
    decoder: Any

    def decode(self, data: bytes) -> dict:
        try:
            if self.bus_type == "lin":
                return self.decoder.decode(bytearray(data))
            return self.decoder.decode(data, decode_choices=False, scaling=True)
        except (DecodeError, ValueError, TypeError) as exc:
            raise TraceDatabaseError(
                f"{self.bus_type.upper()} ID 0x{self.frame_id:X} payload "
                f"无法按数据库 {self.path.name} 解码: {exc}"
            ) from exc


def resolve_database_files(
    database_path: str | None = None,
    database_paths: list[str] | None = None,
) -> tuple[Path, ...]:
    """展开用户明确提供的数据库文件或目录，并保持确定性顺序。"""
    raw_paths = []
    if database_path:
        raw_paths.append(database_path)
    if database_paths:
        raw_paths.extend(database_paths)
    if not raw_paths:
        return ()

    files: list[Path] = []
    for raw_path in raw_paths:
        if not raw_path.strip():
            raise TraceInputError("database_paths 不能包含空路径")
        path = Path(raw_path).expanduser().resolve()
        if path.is_file() and path.suffix.casefold() in DATABASE_EXTENSIONS:
            files.append(path)
            continue
        if path.is_dir():
            files.extend(
                candidate.resolve()
                for candidate in sorted(
                    path.rglob("*"), key=lambda item: str(item).casefold()
                )
                if candidate.is_file()
                and candidate.suffix.casefold() in DATABASE_EXTENSIONS
            )
            continue
        raise TraceInputError(
            f"数据库路径不存在，或不是 .dbc/.arxml/.ldf 文件或目录: {path}"
        )
    unique = tuple(dict.fromkeys(files))
    if not unique:
        raise TraceInputError("数据库路径中没有找到 .dbc、.arxml 或 .ldf 文件")
    return unique


class NetworkDatabase:
    def __init__(self, paths: Path | tuple[Path, ...]):
        self.paths = (paths,) if isinstance(paths, Path) else paths
        self.messages: list[DatabaseMessage] = []
        for path in self.paths:
            self.messages.extend(self._load_file(path))

    @staticmethod
    def _load_file(path: Path) -> list[DatabaseMessage]:
        try:
            if path.suffix.casefold() == ".ldf":
                database = ldfparser.parse_ldf(str(path))
                return [
                    DatabaseMessage(
                        path=path,
                        bus_type="lin",
                        frame_id=frame.frame_id,
                        name=frame.name,
                        signal_names=tuple(
                            signal.name for _, signal in frame.signal_map
                        ),
                        decoder=frame,
                    )
                    for frame in database.get_unconditional_frames()
                ]

            database = cantools.database.load_file(str(path))
            return [
                DatabaseMessage(
                    path=path,
                    bus_type="can",
                    frame_id=message.frame_id,
                    name=message.name,
                    signal_names=tuple(signal.name for signal in message.signals),
                    decoder=message,
                )
                for message in database.messages
            ]
        except Exception as exc:
            raise TraceDatabaseError(f"数据库加载失败: {path.name}: {exc}") from exc

    def resolve_signal(
        self,
        arbitration_id: int,
        signal_name: str,
        bus_type: str | None = None,
        database_file: str | None = None,
    ) -> DatabaseMessage:
        candidates = [
            message
            for message in self.messages
            if message.frame_id == arbitration_id
            and signal_name in message.signal_names
            and (bus_type is None or message.bus_type == bus_type)
            and (
                database_file is None
                or message.path.name.casefold() == database_file.casefold()
                or str(message.path).casefold() == database_file.casefold()
            )
        ]
        if not candidates:
            bus_label = f"{bus_type.upper()} " if bus_type else ""
            raise TraceDatabaseError(
                f"数据库中不存在 {bus_label}ID 0x{arbitration_id:X} 的信号: "
                f"{signal_name}"
            )
        if len(candidates) > 1:
            files = ", ".join(sorted({item.path.name for item in candidates}))
            raise TraceDatabaseError(
                f"信号匹配多个数据库，请通过 database_file 明确选择：{files}"
            )
        return candidates[0]

    def search(self, query: str) -> list[dict]:
        """按报文名或信号名搜索轻量候选，不返回整个数据库。"""
        normalized_query = query.casefold()
        results = []
        for message in self.messages:
            matched_signals = [
                signal_name
                for signal_name in message.signal_names
                if normalized_query in signal_name.casefold()
            ]
            message_matched = normalized_query in message.name.casefold()
            if not message_matched and not matched_signals:
                continue
            exact_signal = any(
                signal_name.casefold() == normalized_query
                for signal_name in matched_signals
            )
            results.append(
                {
                    "bus_type": message.bus_type,
                    "arbitration_id": message.frame_id,
                    "arbitration_id_hex": f"0x{message.frame_id:X}",
                    "frame_id": message.frame_id,
                    "frame_id_hex": f"0x{message.frame_id:X}",
                    "message_name": message.name,
                    "message_name_exact": message.name.casefold() == normalized_query,
                    "matched_signal_names": matched_signals,
                    "signal_name_exact": exact_signal,
                    "database_file": message.path.name,
                }
            )
        return sorted(
            results,
            key=lambda item: (
                not item["signal_name_exact"],
                not item["message_name_exact"],
                item["bus_type"],
                item["arbitration_id"],
                item["database_file"].casefold(),
            ),
        )
