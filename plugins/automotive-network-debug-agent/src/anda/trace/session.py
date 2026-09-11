"""
文件用途：
- 管理进程内 Trace Session，保证同一份未变化的输入只解析一次。
- 提供后台索引状态、摘要、帧查询、时序统计和 DBC/ARXML/LDF 信号解码能力。

会话生命周期与 MCP Server 进程一致。帧数据保存在紧凑内存数组中，不写大型缓存文件；
服务重启后需重新 load_trace，这是一期为简单性与磁盘占用做出的明确取舍。
"""

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Semaphore, Thread
from time import perf_counter
from uuid import uuid4

from anda.common.errors import (
    TraceDatabaseError,
    TraceInputError,
    TraceNotFoundError,
    TraceNotReadyError,
)
from anda.trace.blf import iter_blf, read_blf_metadata
from anda.trace.dbc import NetworkDatabase, resolve_database_files
from anda.trace.routed_timeout import analyze_routed_signal_timeout
from anda.trace.store import CompactFrameStore
from anda.trace.timing import analyze_timestamps

MAX_FRAME_RESULTS = 200
MAX_SIGNAL_RESULTS = 200
MAX_DATABASE_RESULTS = 20


@dataclass(slots=True)
class TraceSession:
    """一份已解析日志及其可选网络数据库。"""

    blf_path: Path
    database_paths: tuple[Path, ...]
    store: CompactFrameStore
    database: NetworkDatabase | None
    metadata: dict
    channel_mapping: dict[tuple[str, int], dict] = field(default_factory=dict)


@dataclass(slots=True)
class TraceLoadTask:
    """一项不会阻塞 MCP Tool 生命周期的后台 Trace 索引任务。"""

    trace_id: str
    source_key: tuple
    blf_path: Path
    database_paths: tuple[Path, ...]
    status: str = "queued"
    stage: str = "queued"
    scanned_object_count: int = 0
    indexed_frame_count: int = 0
    total_object_count: int | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    done: Event = field(default_factory=Event)


class TraceSessionManager:
    """创建并查询可复用的 Trace Session。"""

    def __init__(self) -> None:
        self._sessions: dict[str, TraceSession] = {}
        self._source_keys: dict[tuple, str] = {}
        self._load_tasks: dict[str, TraceLoadTask] = {}
        self._pending_keys: dict[tuple, str] = {}
        self._load_lock = Lock()
        self._load_slot = Semaphore(1)

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

    def _prepare_inputs(
        self,
        blf_path: str,
        database_path: str | None,
        database_paths: list[str] | None,
    ) -> tuple[Path, tuple[Path, ...], tuple]:
        blf = self._existing_file(blf_path, "BLF ")
        database_files = resolve_database_files(database_path, database_paths)
        source_key = (
            self._file_identity(blf),
            tuple(self._file_identity(path) for path in database_files),
        )
        return blf, database_files, source_key

    def load_trace(
        self,
        blf_path: str,
        database_path: str | None = None,
        database_paths: list[str] | None = None,
    ) -> dict:
        """同步加载入口，供 Core 调用和确定性测试使用。"""
        blf, database_files, source_key = self._prepare_inputs(
            blf_path, database_path, database_paths
        )

        with self._load_lock:
            existing_id = self._source_keys.get(source_key)
            if existing_id is not None:
                session = self._sessions[existing_id]
                return self._load_result(existing_id, session, reused=True)
        with self._load_slot:
            session = self._build_session(blf, database_files)
        trace_id = str(uuid4())
        with self._load_lock:
            existing_id = self._source_keys.get(source_key)
            if existing_id is not None:
                return self._load_result(
                    existing_id, self._sessions[existing_id], reused=True
                )
            self._sessions[trace_id] = session
            self._source_keys[source_key] = trace_id
            return self._load_result(trace_id, session, reused=False)

    def start_load_trace(
        self,
        blf_path: str,
        database_path: str | None = None,
        database_paths: list[str] | None = None,
    ) -> dict:
        """快速返回 trace_id，并在 daemon 线程中建立大型 Trace 索引。"""
        blf, database_files, source_key = self._prepare_inputs(
            blf_path, database_path, database_paths
        )
        with self._load_lock:
            ready_id = self._source_keys.get(source_key)
            if ready_id is not None:
                return self._load_result(
                    ready_id, self._sessions[ready_id], reused=True
                )
            pending_id = self._pending_keys.get(source_key)
            if pending_id is not None:
                return self._task_result(self._load_tasks[pending_id], reused=True)

            trace_id = str(uuid4())
            task = TraceLoadTask(trace_id, source_key, blf, database_files)
            self._load_tasks[trace_id] = task
            self._pending_keys[source_key] = trace_id
        Thread(
            target=self._run_background_load,
            args=(task,),
            name=f"trace-load-{trace_id[:8]}",
            daemon=True,
        ).start()
        return self._task_result(task, reused=False)

    def get_load_status(self, trace_id: str, wait_seconds: float = 0) -> dict:
        if not 0 <= wait_seconds <= 55:
            raise TraceInputError("wait_seconds 必须在 0 到 55 之间")
        with self._load_lock:
            task = self._load_tasks.get(trace_id)
            session = self._sessions.get(trace_id)
        if task is None:
            if session is None:
                raise TraceNotFoundError(f"未知 trace_id: {trace_id}")
            return self._load_result(trace_id, session, reused=True)
        if wait_seconds:
            task.done.wait(wait_seconds)
        return self._task_result(task, reused=True)

    def _run_background_load(self, task: TraceLoadTask) -> None:
        started = perf_counter()
        task.status = "loading"
        task.stage = "waiting_for_worker"
        try:
            with self._load_slot:
                task.stage = "loading_database"

                def progress(scanned: int, indexed: int, total: int | None) -> None:
                    task.stage = "indexing_blf"
                    task.scanned_object_count = scanned
                    task.indexed_frame_count = indexed
                    task.total_object_count = total
                    task.elapsed_seconds = perf_counter() - started

                session = self._build_session(
                    task.blf_path, task.database_paths, progress
                )
            with self._load_lock:
                self._sessions[task.trace_id] = session
                self._source_keys[task.source_key] = task.trace_id
                self._pending_keys.pop(task.source_key, None)
            task.indexed_frame_count = len(session.store)
            task.status = "ready"
            task.stage = "ready"
        # 后台边界必须捕获并序列化所有失败，否则调用方只会看到永久 loading。
        except Exception as exc:  # noqa: BLE001
            with self._load_lock:
                self._pending_keys.pop(task.source_key, None)
            task.error = f"{type(exc).__name__}: {exc}"
            task.status = "failed"
            task.stage = "failed"
        finally:
            task.elapsed_seconds = perf_counter() - started
            task.done.set()

    @staticmethod
    def _build_session(
        blf: Path,
        database_files: tuple[Path, ...],
        progress_callback=None,
    ) -> TraceSession:
        metadata = read_blf_metadata(blf)
        database = NetworkDatabase(database_files) if database_files else None
        store = CompactFrameStore()
        for frame in iter_blf(blf, progress_callback):
            store.append(frame)
        return TraceSession(blf, database_files, store, database, metadata)

    @staticmethod
    def _load_result(trace_id: str, session: TraceSession, reused: bool) -> dict:
        return {
            "trace_id": trace_id,
            "status": "ready",
            "index_ready": True,
            "frame_count": len(session.store),
            "database_loaded": session.database is not None,
            "database_file_count": len(session.database_paths),
            "reused": reused,
        }

    @staticmethod
    def _task_result(task: TraceLoadTask, reused: bool) -> dict:
        total = task.total_object_count
        progress = (
            min(100.0, round(task.scanned_object_count * 100 / total, 2))
            if total
            else None
        )
        return {
            "trace_id": task.trace_id,
            "status": task.status,
            "index_ready": task.status == "ready",
            "stage": task.stage,
            "scanned_object_count": task.scanned_object_count,
            "indexed_frame_count": task.indexed_frame_count,
            "total_object_count": total,
            "progress_percent": progress,
            "elapsed_seconds": round(task.elapsed_seconds, 3),
            "database_file_count": len(task.database_paths),
            "reused": reused,
            "error": task.error,
        }

    def _get(self, trace_id: str) -> TraceSession:
        session = self._sessions.get(trace_id)
        if session is not None:
            return session
        task = self._load_tasks.get(trace_id)
        if task is not None:
            if task.status == "failed":
                raise TraceInputError(f"Trace 索引失败：{task.error}")
            raise TraceNotReadyError(
                f"Trace 索引尚未完成（{task.stage}）；请调用 get_trace_load_status "
                "并复用同一 trace_id，不要重复 load_trace"
            )
        raise TraceNotFoundError(f"未知 trace_id: {trace_id}")

    def get_summary(self, trace_id: str) -> dict:
        session = self._get(trace_id)
        store = session.store
        start = store.start_timestamp
        end = store.end_timestamp
        return {
            "trace_id": trace_id,
            "source_file": session.blf_path.name,
            "database_file": (
                session.database_paths[0].name
                if len(session.database_paths) == 1
                else None
            ),
            "database_files": [path.name for path in session.database_paths],
            "database_loaded": session.database is not None,
            "start_timestamp": start,
            "end_timestamp": end,
            "duration_seconds": end - start
            if start is not None and end is not None
            else 0.0,
            "frame_count": len(store),
            "channels": sorted(store.channel_values),
            "arbitration_id_count": len(store.arbitration_id_values),
            "bus_types": sorted(
                ({"can"} if store.classic_can_count or store.can_fd_count else set())
                | ({"lin"} if store.lin_frame_count else set())
            ),
            "classic_can_frame_count": store.classic_can_count,
            "can_fd_frame_count": store.can_fd_count,
            "lin_frame_count": store.lin_frame_count,
            "error_frame_count": store.error_frame_count,
            "timestamp_reference": session.metadata["timestamp_reference"],
            "blf_metadata": {
                key: value
                for key, value in session.metadata.items()
                if key != "timestamp_reference"
            },
            "channel_mapping": sorted(
                session.channel_mapping.values(),
                key=lambda item: (item["bus_type"], item["analysis_channel"]),
            ),
            "channel_mapping_count": len(session.channel_mapping),
            "channel_mapping_completeness": "UNKNOWN_WITHOUT_REQUIRED_CHANNEL_SET",
        }

    def set_channel_mapping(self, trace_id: str, mappings: list[dict]) -> dict:
        """登记用户明确给出的分析仪、逻辑网段和 ECU Channel 对应关系。"""
        session = self._get(trace_id)
        normalized = _validate_channel_mappings(mappings)
        for item in normalized:
            key = (item["bus_type"], item["analysis_channel"])
            existing = session.channel_mapping.get(key)
            if existing is not None and existing != item:
                raise TraceInputError(
                    f"{item['bus_type'].upper()}{item['analysis_channel']} 已有不同映射；"
                    "请明确修正后重新加载 Trace，不能自动覆盖"
                )
            session.channel_mapping[key] = item
        return {
            "trace_id": trace_id,
            "mapping_count": len(session.channel_mapping),
            "mappings": sorted(
                session.channel_mapping.values(),
                key=lambda item: (item["bus_type"], item["analysis_channel"]),
            ),
            "mapping_basis": "explicit_user_or_user_supplied_evidence",
            "mapping_inferred": False,
        }

    def require_channel_mapping(
        self, trace_id: str, channels: list[tuple[str, int]]
    ) -> list[dict]:
        session = self._get(trace_id)
        missing = [
            f"{bus.upper()}{channel}"
            for bus, channel in channels
            if (bus, channel) not in session.channel_mapping
        ]
        if missing:
            raise TraceInputError(
                "跨网段分析前必须通过 set_channel_mapping 明确提供映射；缺少："
                + ", ".join(missing)
            )
        return [session.channel_mapping[item] for item in channels]

    def find_messages(
        self,
        trace_id: str,
        arbitration_id: int | None = None,
        channel: int | None = None,
        limit: int = 100,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
        bus_type: str | None = None,
    ) -> dict:
        bus_type = self._validate_query(
            arbitration_id, channel, limit, start_timestamp, end_timestamp, bus_type
        )
        session = self._get(trace_id)
        applied_limit = min(limit, MAX_FRAME_RESULTS)

        total_count = 0
        frames = []
        for position in session.store.matching_indices(
            arbitration_id, channel, start_timestamp, end_timestamp, bus_type
        ):
            total_count += 1
            if len(frames) < applied_limit:
                frames.append(session.store.frame_dict(position))

        return {
            "trace_id": trace_id,
            "filters": {
                "arbitration_id": arbitration_id,
                "bus_type": bus_type,
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
        bus_type: str | None = None,
    ) -> dict:
        bus_type = self._validate_query(
            arbitration_id, channel, 1, start_timestamp, end_timestamp, bus_type
        )
        session = self._get(trace_id)
        timestamps = [
            session.store.timestamp_at(position)
            for position in session.store.matching_indices(
                arbitration_id, channel, start_timestamp, end_timestamp, bus_type
            )
        ]
        result = analyze_timestamps(timestamps)
        result.update(
            {
                "trace_id": trace_id,
                "arbitration_id": arbitration_id,
                "arbitration_id_hex": f"0x{arbitration_id:X}",
                "frame_id": arbitration_id,
                "frame_id_hex": f"0x{arbitration_id:X}",
                "bus_type": bus_type,
                "channel": channel,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
            }
        )
        return result

    def analyze_routed_signal_timeout(
        self,
        trace_id: str,
        source_channel: int,
        source_message: str,
        target_channels: list[int],
        target_frame: str,
        signal_name: str,
        timeout_value: object,
        configured_timeout_ms: float,
        source_bus_type: str = "can",
        target_bus_type: str = "lin",
        source_database_file: str | None = None,
        target_database_files: dict[str, str] | None = None,
    ) -> dict:
        """一次关联源停止与多个目标 timeout 值切换，不推断运行时根因。"""
        source_bus_type = source_bus_type.strip().casefold()
        target_bus_type = target_bus_type.strip().casefold()
        mappings = self.require_channel_mapping(
            trace_id,
            [(source_bus_type, source_channel)]
            + [(target_bus_type, channel) for channel in target_channels],
        )
        session = self._get(trace_id)
        result = analyze_routed_signal_timeout(
            store=session.store,
            database=session.database,
            source_channel=source_channel,
            source_message=source_message,
            target_channels=target_channels,
            target_frame=target_frame,
            signal_name=signal_name,
            timeout_value=timeout_value,
            configured_timeout_ms=configured_timeout_ms,
            source_bus_type=source_bus_type,
            target_bus_type=target_bus_type,
            source_database_file=source_database_file,
            target_database_files=target_database_files,
        )
        result.update(
            {
                "trace_id": trace_id,
                "channel_mapping": mappings,
                "timestamp_reference": session.metadata["timestamp_reference"],
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
        bus_type: str | None = None,
        database_file: str | None = None,
    ) -> dict:
        bus_type = self._validate_query(
            arbitration_id, channel, limit, start_timestamp, end_timestamp, bus_type
        )
        if not signal_name.strip():
            raise TraceInputError("signal_name 不能为空")

        session = self._get(trace_id)
        database = session.database
        if database is None:
            raise TraceDatabaseError("当前 Trace 未加载 DBC/ARXML/LDF 数据库")
        message = database.resolve_signal(
            arbitration_id,
            signal_name,
            bus_type=bus_type,
            database_file=database_file,
        )

        applied_limit = min(limit, MAX_SIGNAL_RESULTS)
        sample_count = 0
        decode_error_count = 0
        numeric_min: float | None = None
        numeric_max: float | None = None
        samples = []
        for position in session.store.matching_indices(
            arbitration_id,
            channel,
            start_timestamp,
            end_timestamp,
            message.bus_type,
        ):
            try:
                decoded = message.decode(session.store.payload_at(position))
            except TraceDatabaseError:
                decode_error_count += 1
                continue

            value = decoded[signal_name]
            sample_count += 1
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric_value = float(value)
                numeric_min = (
                    numeric_value
                    if numeric_min is None
                    else min(numeric_min, numeric_value)
                )
                numeric_max = (
                    numeric_value
                    if numeric_max is None
                    else max(numeric_max, numeric_value)
                )
            if len(samples) < applied_limit:
                samples.append(
                    {
                        "timestamp": session.store.timestamp_at(position),
                        "channel": session.store.channel_at(position),
                        "bus_type": message.bus_type,
                        "value": value,
                    }
                )

        return {
            "trace_id": trace_id,
            "arbitration_id": arbitration_id,
            "arbitration_id_hex": f"0x{arbitration_id:X}",
            "frame_id": arbitration_id,
            "frame_id_hex": f"0x{arbitration_id:X}",
            "bus_type": message.bus_type,
            "message_name": message.name,
            "database_file": message.path.name,
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

    def search_database(
        self, trace_id: str, query: str, limit: int = MAX_DATABASE_RESULTS
    ) -> dict:
        """搜索报文名或信号名，为后续精确解码提供有限候选。"""
        normalized_query = query.strip()
        if not normalized_query:
            raise TraceInputError("query 不能为空")
        if limit < 1:
            raise TraceInputError("limit 必须大于等于 1")

        session = self._get(trace_id)
        database = session.database
        if database is None:
            raise TraceDatabaseError("当前 Trace 未加载 DBC/ARXML/LDF 数据库")

        matches = database.search(normalized_query)
        applied_limit = min(limit, MAX_DATABASE_RESULTS)
        return {
            "trace_id": trace_id,
            "query": normalized_query,
            "total_count": len(matches),
            "returned_count": min(len(matches), applied_limit),
            "limit_applied": applied_limit,
            "matches": matches[:applied_limit],
        }

    @staticmethod
    def _validate_query(
        arbitration_id: int | None,
        channel: int | None,
        limit: int,
        start_timestamp: float | None,
        end_timestamp: float | None,
        bus_type: str | None = None,
    ) -> str | None:
        if bus_type is not None:
            bus_type = bus_type.casefold()
            if bus_type not in {"can", "lin"}:
                raise TraceInputError("bus_type 仅支持 can 或 lin")
        if arbitration_id is not None and not 0 <= arbitration_id <= 0x1FFFFFFF:
            raise TraceInputError("arbitration_id 必须在 0 到 0x1FFFFFFF 之间")
        if bus_type == "lin" and arbitration_id is not None and arbitration_id > 0x3F:
            raise TraceInputError("LIN frame ID 必须在 0x00 到 0x3F 之间")
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
        return bus_type


def _validate_channel_mappings(mappings: list[dict]) -> list[dict]:
    if not mappings or len(mappings) > 64:
        raise TraceInputError("mappings 必须包含 1 到 64 条明确 Channel 映射")
    result = []
    seen: set[tuple[str, int]] = set()
    for raw in mappings:
        try:
            channel = int(raw["analysis_channel"])
            bus_type = str(raw["bus_type"]).strip().casefold()
            logical_network = str(raw["logical_network"]).strip()
            mapping_source = str(raw["mapping_source"]).strip()
        except (KeyError, TypeError, ValueError) as error:
            raise TraceInputError(
                "每条映射必须包含 analysis_channel、bus_type、logical_network、"
                "mapping_source"
            ) from error
        if channel < 1:
            raise TraceInputError("analysis_channel 使用 1-based 编号")
        if bus_type not in {"can", "lin"}:
            raise TraceInputError("Channel 映射的 bus_type 仅支持 can 或 lin")
        if not logical_network or not mapping_source:
            raise TraceInputError("logical_network 和 mapping_source 不能为空")
        key = (bus_type, channel)
        if key in seen:
            raise TraceInputError(
                f"mappings 中重复定义 {bus_type.upper()}{channel}"
            )
        seen.add(key)
        result.append(
            {
                "analysis_channel": channel,
                "bus_type": bus_type,
                "logical_network": logical_network,
                "ecu_channel": (
                    str(raw["ecu_channel"]).strip()
                    if raw.get("ecu_channel") is not None
                    else None
                ),
                "mapping_source": mapping_source,
                "evidence": (
                    str(raw["evidence"]).strip()
                    if raw.get("evidence") is not None
                    else None
                ),
            }
        )
    return result
