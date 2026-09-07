"""
文件用途：
- 管理进程内 Trace Session，保证同一份未变化的输入只解析一次。
- 提供摘要、帧查询、时序统计和 DBC/ARXML 信号解码能力。

会话生命周期与 MCP Server 进程一致。帧数据保存在紧凑内存数组中，不写大型缓存文件；
服务重启后需重新 load_trace，这是一期为简单性与磁盘占用做出的明确取舍。
"""

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from anda.common.errors import TraceDatabaseError, TraceInputError, TraceNotFoundError
from anda.trace.blf import iter_blf
from anda.trace.dbc import NetworkDatabase
from anda.trace.store import CompactFrameStore
from anda.trace.timing import analyze_timestamps

MAX_FRAME_RESULTS = 200
MAX_SIGNAL_RESULTS = 200


@dataclass(slots=True)
class TraceSession:
    """一份已解析日志及其可选网络数据库。"""

    blf_path: Path
    database_path: Path | None
    store: CompactFrameStore
    database: NetworkDatabase | None


class TraceSessionManager:
    """创建并查询可复用的 Trace Session。"""

    def __init__(self) -> None:
        self._sessions: dict[str, TraceSession] = {}
        self._source_keys: dict[tuple, str] = {}
        # load_trace 可能被多个 MCP Client 同时调用；串行化加载可避免同一 BLF 被重复解析。
        self._load_lock = Lock()

    @staticmethod
    def _existing_file(path_value: str, label: str) -> Path:
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise TraceInputError(f"{label}文件不存在: {path}")
        return path

    @staticmethod
    def _file_identity(path: Path) -> tuple[str, int, int]:
        stat = path.stat()
        return (str(path).casefold(), stat.st_size, stat.st_mtime_ns)

    def load_trace(self, blf_path: str, database_path: str | None = None) -> dict:
        blf = self._existing_file(blf_path, "BLF ")
        database_file = (
            self._existing_file(database_path, "数据库 ") if database_path else None
        )
        source_key = (
            self._file_identity(blf),
            self._file_identity(database_file) if database_file else None,
        )

        with self._load_lock:
            existing_id = self._source_keys.get(source_key)
            if existing_id is not None:
                session = self._sessions[existing_id]
                return self._load_result(existing_id, session, reused=True)

            # 先加载体积小的数据库，避免数据库无效时仍完整解析 BLF。
            database = NetworkDatabase(database_file) if database_file else None
            store = CompactFrameStore()
            for frame in iter_blf(blf):
                store.append(frame)

            trace_id = str(uuid4())
            session = TraceSession(
                blf_path=blf,
                database_path=database_file,
                store=store,
                database=database,
            )
            self._sessions[trace_id] = session
            self._source_keys[source_key] = trace_id
            return self._load_result(trace_id, session, reused=False)

    @staticmethod
    def _load_result(trace_id: str, session: TraceSession, reused: bool) -> dict:
        return {
            "trace_id": trace_id,
            "frame_count": len(session.store),
            "database_loaded": session.database is not None,
            "reused": reused,
        }

    def _get(self, trace_id: str) -> TraceSession:
        try:
            return self._sessions[trace_id]
        except KeyError as exc:
            raise TraceNotFoundError(f"未知 trace_id: {trace_id}") from exc

    def get_summary(self, trace_id: str) -> dict:
        session = self._get(trace_id)
        store = session.store
        start = store.start_timestamp
        end = store.end_timestamp
        return {
            "trace_id": trace_id,
            "source_file": session.blf_path.name,
            "database_file": (
                session.database_path.name if session.database_path is not None else None
            ),
            "database_loaded": session.database is not None,
            "start_timestamp": start,
            "end_timestamp": end,
            "duration_seconds": end - start if start is not None and end is not None else 0.0,
            "frame_count": len(store),
            "channels": sorted(store.channel_values),
            "arbitration_id_count": len(store.arbitration_id_values),
            "classic_can_frame_count": store.classic_can_count,
            "can_fd_frame_count": store.can_fd_count,
            "error_frame_count": store.error_frame_count,
        }

    def find_messages(
        self,
        trace_id: str,
        arbitration_id: int | None = None,
        channel: int | None = None,
        limit: int = 100,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
    ) -> dict:
        self._validate_query(arbitration_id, channel, limit, start_timestamp, end_timestamp)
        session = self._get(trace_id)
        applied_limit = min(limit, MAX_FRAME_RESULTS)

        total_count = 0
        frames = []
        for position in session.store.matching_indices(
            arbitration_id, channel, start_timestamp, end_timestamp
        ):
            total_count += 1
            if len(frames) < applied_limit:
                frames.append(session.store.frame_dict(position))

        return {
            "trace_id": trace_id,
            "filters": {
                "arbitration_id": arbitration_id,
                "channel": channel,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
            },
            "total_count": total_count,
            "returned_count": len(frames),
            "limit_applied": applied_limit,
            "frames": frames,
        }

    def get_message_timing(
        self,
        trace_id: str,
        arbitration_id: int,
        channel: int | None = None,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
    ) -> dict:
        self._validate_query(arbitration_id, channel, 1, start_timestamp, end_timestamp)
        session = self._get(trace_id)
        timestamps = [
            session.store.timestamp_at(position)
            for position in session.store.matching_indices(
                arbitration_id, channel, start_timestamp, end_timestamp
            )
        ]
        result = analyze_timestamps(timestamps)
        result.update(
            {
                "trace_id": trace_id,
                "arbitration_id": arbitration_id,
                "arbitration_id_hex": f"0x{arbitration_id:X}",
                "channel": channel,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
            }
        )
        return result

    def decode_signal(
        self,
        trace_id: str,
        arbitration_id: int,
        signal_name: str,
        channel: int | None = None,
        limit: int = 100,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
    ) -> dict:
        self._validate_query(arbitration_id, channel, limit, start_timestamp, end_timestamp)
        if not signal_name.strip():
            raise TraceInputError("signal_name 不能为空")

        session = self._get(trace_id)
        database = session.database
        if database is None:
            raise TraceDatabaseError("当前 Trace 未加载 DBC/ARXML 数据库")
        database.require_signal(arbitration_id, signal_name)

        applied_limit = min(limit, MAX_SIGNAL_RESULTS)
        sample_count = 0
        decode_error_count = 0
        numeric_min: float | None = None
        numeric_max: float | None = None
        samples = []
        for position in session.store.matching_indices(
            arbitration_id, channel, start_timestamp, end_timestamp
        ):
            try:
                decoded = database.decode(
                    arbitration_id, session.store.payload_at(position)
                )
            except TraceDatabaseError:
                decode_error_count += 1
                continue

            value = decoded[signal_name]
            sample_count += 1
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric_value = float(value)
                numeric_min = (
                    numeric_value if numeric_min is None else min(numeric_min, numeric_value)
                )
                numeric_max = (
                    numeric_value if numeric_max is None else max(numeric_max, numeric_value)
                )
            if len(samples) < applied_limit:
                samples.append(
                    {
                        "timestamp": session.store.timestamp_at(position),
                        "channel": session.store.channel_at(position),
                        "value": value,
                    }
                )

        return {
            "trace_id": trace_id,
            "arbitration_id": arbitration_id,
            "arbitration_id_hex": f"0x{arbitration_id:X}",
            "channel": channel,
            "signal_name": signal_name,
            "sample_count": sample_count,
            "decode_error_count": decode_error_count,
            "returned_count": len(samples),
            "limit_applied": applied_limit,
            "min": numeric_min,
            "max": numeric_max,
            "samples": samples,
        }

    @staticmethod
    def _validate_query(
        arbitration_id: int | None,
        channel: int | None,
        limit: int,
        start_timestamp: float | None,
        end_timestamp: float | None,
    ) -> None:
        if arbitration_id is not None and not 0 <= arbitration_id <= 0x1FFFFFFF:
            raise TraceInputError("arbitration_id 必须在 0 到 0x1FFFFFFF 之间")
        if channel is not None and channel < 1:
            raise TraceInputError("channel 使用 1-based 编号，必须大于等于 1")
        if limit < 1:
            raise TraceInputError("limit 必须大于等于 1")
        if (
            start_timestamp is not None
            and end_timestamp is not None
            and start_timestamp > end_timestamp
        ):
            raise TraceInputError("start_timestamp 不能晚于 end_timestamp")
