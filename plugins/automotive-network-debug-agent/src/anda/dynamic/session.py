"""Dynamic Verification Core 的有状态 Session 编排。"""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from anda.common.errors import (
    DynamicInputError,
    DynamicSessionNotFoundError,
    DynamicStateError,
)
from anda.dynamic.models import (
    ConfigurationOrigin,
    DatabaseSpec,
    DynamicMode,
    DynamicSession,
    InstanceMode,
    RuntimeCapability,
)
from anda.dynamic.policy import normalize_runtime_capabilities, require_capability
from anda.dynamic.pycanoe import PyCanoeBackend
from anda.dynamic.worker import BackendWorker
from anda.trace.session import TraceSessionManager

MAX_OBSERVATION_RESULTS = 200
MAX_SNAPSHOT_ITEMS = 10
MAX_TEST_RESULTS = 100
MAX_FRAME_COUNT = 100
MAX_FRAME_DURATION_MS = 10_000


class DynamicSessionManager:
    """持有一个 CANoe Session，并复用 Trace Core 提供 BLF 窗口观察。"""

    def __init__(
        self,
        backend_factory: Callable[[], Any] = PyCanoeBackend,
        trace_manager: TraceSessionManager | None = None,
        temp_root: Path | None = None,
    ) -> None:
        self._backend_factory = backend_factory
        self._trace = trace_manager or TraceSessionManager()
        self._temp_root = temp_root or _default_temp_root()
        self._sessions: dict[str, DynamicSession] = {}
        self._active_session_id: str | None = None
        self._lock = Lock()

    def get_health(self) -> dict[str, Any]:
        """静态探测环境，不启动 CANoe，也不占用许可证。"""
        probe = self._backend_factory.probe_environment()
        return {
            "current_mcp": "dynamic",
            "status": "available" if probe.get("backend_available") else "unavailable",
            "stateful": True,
            "active_session_count": int(self._active_session_id is not None),
            "backend_probe": probe,
            "side_effect_levels": [
                "READ_ONLY",
                "LOCAL_MUTATION",
                "RUNTIME_MUTATION",
                "BUS_ACTIVE",
            ],
            "openai_api_required": False,
        }

    def create_dynamic_session(
        self,
        blf_path: str | None = None,
        database_paths: list[str] | None = None,
        database_mappings: list[dict[str, Any]] | None = None,
        configuration_path: str | None = None,
        mode: str = "auto",
        instance_mode: str = "managed",
        license_confirmed: bool = False,
        runtime_policy: list[str] | None = None,
        visible: bool = False,
        raw_frame_capl_function: str = "ANDA_TransmitCanFrame",
    ) -> dict[str, Any]:
        if not license_confirmed:
            raise DynamicInputError(
                "启动或附着 CANoe 前必须由用户明确确认有效许可证；"
                "确认后传入 license_confirmed=true"
            )
        resolved_instance_mode = _resolve_instance_mode(instance_mode)
        resolved_mode = _resolve_mode(mode, configuration_path, resolved_instance_mode)
        blf = _optional_file(blf_path, "BLF")
        configuration = _optional_file(configuration_path, "CANoe Configuration")
        if (
            resolved_instance_mode is InstanceMode.ATTACHED
            and configuration is not None
        ):
            raise DynamicInputError(
                "attached 模式不接受 configuration_path，避免替换用户当前 cfg"
            )
        if configuration is not None and configuration.suffix.casefold() != ".cfg":
            raise DynamicInputError("configuration_path 必须指向 CANoe .cfg 文件")
        if (
            resolved_instance_mode is InstanceMode.MANAGED
            and blf is None
            and configuration is None
        ):
            raise DynamicInputError("至少需要提供 blf_path 或 configuration_path")
        if (
            resolved_instance_mode is InstanceMode.MANAGED
            and resolved_mode is DynamicMode.ONLINE
            and configuration is None
        ):
            raise DynamicInputError("online Session 必须提供明确的 CANoe .cfg")

        database_files = tuple(
            _required_file(value, "网络数据库") for value in (database_paths or [])
        )
        database_specs = _database_specs(database_files, database_mappings)
        capabilities = normalize_runtime_capabilities(runtime_policy)
        session_id = str(uuid4())
        temp_directory = self._temp_root / session_id

        origin = (
            ConfigurationOrigin.ATTACHED_CONFIGURATION
            if resolved_instance_mode is InstanceMode.ATTACHED
            else ConfigurationOrigin.TEMPORARY_CONFIGURATION
        )
        original_digest = _file_digest(configuration) if configuration else None
        shadow = None
        with self._lock:
            if self._active_session_id is not None:
                raise DynamicStateError(
                    "当前 Dynamic MCP 已有活动 Session；请先关闭后再创建新 Session"
                )
            self._active_session_id = session_id

        try:
            temp_directory.mkdir(parents=True, exist_ok=False)
        except Exception:
            self._clear_active(session_id)
            raise

        if configuration is not None:
            origin = ConfigurationOrigin.SHADOW_COPY
            shadow = configuration.with_name(
                f"{configuration.stem}.__anda_{session_id[:8]}{configuration.suffix}"
            )
            if shadow.exists():
                _remove_directory(temp_directory)
                self._clear_active(session_id)
                raise DynamicStateError(f"Shadow Configuration 已存在：{shadow}")
            try:
                shutil.copy2(configuration, shadow)
            except OSError:
                _remove_directory(temp_directory)
                self._clear_active(session_id)
                raise

        trace_id = None
        try:
            if blf is not None:
                trace_result = self._trace.start_load_trace(
                    str(blf), database_paths=[str(path) for path in database_files]
                )
                trace_id = trace_result["trace_id"]
        except Exception:
            _remove_shadow(shadow)
            _remove_directory(temp_directory)
            self._clear_active(session_id)
            raise

        worker: BackendWorker | None = None
        try:
            worker = BackendWorker(self._backend_factory)
            backend_status = worker.call(
                "initialize",
                {
                    "mode": resolved_mode.value,
                    "instance_mode": resolved_instance_mode.value,
                    "configuration_origin": origin.value,
                    "configuration_path": str(shadow) if shadow else None,
                    "blf_path": str(blf) if blf else None,
                    "databases": (
                        [item.to_dict() for item in database_specs]
                        if resolved_instance_mode is InstanceMode.MANAGED
                        else []
                    ),
                    "temp_directory": str(temp_directory),
                    "visible": visible,
                    "raw_frame_capl_function": raw_frame_capl_function,
                },
                timeout=120,
            )
        except Exception:
            if worker is not None:
                try:
                    worker.call("close", timeout=30)
                except Exception:  # noqa: BLE001 - 创建失败后的尽力清理
                    worker.shutdown()
                else:
                    worker.shutdown()
            _remove_shadow(shadow)
            _remove_directory(temp_directory)
            if trace_id is not None:
                self._trace.release_trace(trace_id)
            self._clear_active(session_id)
            raise

        session = DynamicSession(
            dynamic_session_id=session_id,
            mode=resolved_mode,
            configuration_origin=origin,
            instance_mode=resolved_instance_mode,
            owns_canoe_process=bool(
                backend_status.get(
                    "owns_canoe_process",
                    resolved_instance_mode is InstanceMode.MANAGED,
                )
            ),
            can_quit=bool(
                backend_status.get(
                    "can_quit", resolved_instance_mode is InstanceMode.MANAGED
                )
            ),
            runtime_capabilities=capabilities,
            worker=worker,
            temp_directory=temp_directory,
            original_configuration=configuration,
            original_configuration_digest=original_digest,
            shadow_configuration=shadow,
            blf_path=blf,
            database_specs=database_specs,
            trace_id=trace_id,
            backend_status=backend_status,
        )
        with self._lock:
            self._sessions[session_id] = session
        return self._status_payload(session, backend_status, created=True)

    def get_dynamic_status(self, dynamic_session_id: str) -> dict[str, Any]:
        session = self._get(dynamic_session_id)
        backend = session.worker.call("get_status")
        session.backend_status = backend
        return self._status_payload(session, backend)

    def close_dynamic_session(self, dynamic_session_id: str) -> dict[str, Any]:
        session = self._get(dynamic_session_id, allow_closed=True)
        if session.closed:
            return {
                "dynamic_session_id": dynamic_session_id,
                "closed": True,
                "already_closed": True,
            }

        cleanup_errors: list[dict[str, Any]] = []
        if session.worker.alive:
            try:
                backend_result = session.worker.call("close", timeout=60)
                cleanup_errors.extend(backend_result.get("cleanup_errors", []))
            except Exception as exc:  # noqa: BLE001 - 仍需停止 worker
                cleanup_errors.append(
                    {"operation": "backend_close", "error": str(exc)[:2000]}
                )
            worker_stopped = session.worker.shutdown(timeout=15)
        else:
            worker_stopped = True
        trace_release = (
            self._trace.release_trace(session.trace_id)
            if session.trace_id is not None
            else None
        )
        if not worker_stopped:
            cleanup_errors.append(
                {"operation": "worker_shutdown", "error": "STA 线程未在期限内退出"}
            )
            return {
                "dynamic_session_id": dynamic_session_id,
                "closed": False,
                "already_closed": False,
                "original_configuration_unchanged": None,
                "shadow_configuration_removed": False,
                "temporary_directory_removed": False,
                "cleanup_pending": True,
                "trace_release": trace_release,
                "cleanup_errors": cleanup_errors,
            }

        original_unchanged = _verify_original(session)
        if not original_unchanged:
            cleanup_errors.append(
                {
                    "operation": "verify_original_configuration",
                    "error": "原始 CANoe Configuration 内容发生变化",
                }
            )
        _remove_shadow(session.shadow_configuration)
        temporary_directory_removed = _remove_directory(session.temp_directory)
        if not temporary_directory_removed:
            cleanup_errors.append(
                {
                    "operation": "remove_temporary_directory",
                    "error": "CANoe 退出后临时目录仍被占用",
                }
            )
        session.closed = True
        session.active_operations.clear()
        with self._lock:
            if self._active_session_id == dynamic_session_id:
                self._active_session_id = None
        return {
            "dynamic_session_id": dynamic_session_id,
            "closed": worker_stopped,
            "already_closed": False,
            "original_configuration_unchanged": original_unchanged,
            "shadow_configuration_removed": bool(
                session.shadow_configuration is None
                or not session.shadow_configuration.exists()
            ),
            "temporary_directory_removed": temporary_directory_removed,
            "trace_release": trace_release,
            "cleanup_errors": cleanup_errors,
        }

    def _clear_active(self, dynamic_session_id: str) -> None:
        with self._lock:
            if self._active_session_id == dynamic_session_id:
                self._active_session_id = None

    def compile_capl(
        self, dynamic_session_id: str, write_line_limit: int = 40
    ) -> dict[str, Any]:
        session = self._get(dynamic_session_id)
        return session.worker.call("compile_capl", write_line_limit)

    def control_measurement(
        self, dynamic_session_id: str, action: str
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.MEASUREMENT)
        normalized_action = action.strip().casefold()
        if session.mode is DynamicMode.OFFLINE and normalized_action in {
            "start",
            "reset",
        }:
            raise DynamicStateError(
                "offline Session 的启动/重置必须使用 control_offline_animation；"
                "拒绝普通 Measurement Start，以免旧版 CANoe 在无法确认模式时访问真实总线"
            )
        result = session.worker.call("control_measurement", normalized_action)
        _track_operation(session, "measurement", result.get("measurement") == "running")
        return result

    def control_offline_animation(
        self,
        dynamic_session_id: str,
        action: str,
        animation_delay_ms: int = 100,
    ) -> dict[str, Any]:
        session = self._authorized(
            dynamic_session_id, RuntimeCapability.OFFLINE_ANIMATION
        )
        if session.mode is not DynamicMode.OFFLINE:
            raise DynamicStateError("Offline Animation 仅允许 offline Session")
        if not 1 <= animation_delay_ms <= 10_000:
            raise DynamicInputError("animation_delay_ms 必须在 1 到 10000 之间")
        result = session.worker.call(
            "control_offline_animation",
            action.strip().casefold(),
            animation_delay_ms,
        )
        _track_operation(
            session, "offline_animation", result.get("animation") != "stopped"
        )
        return result

    def wait_runtime_event(
        self,
        dynamic_session_id: str,
        event: str,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        if not 0 <= timeout_seconds <= 55:
            raise DynamicInputError("timeout_seconds 必须在 0 到 55 之间")
        return session.worker.call(
            "wait_runtime_event",
            event.strip().casefold(),
            timeout_seconds,
            timeout=timeout_seconds + 10,
        )

    def observe_bus_window(
        self,
        dynamic_session_id: str,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
        around_cursor_seconds: float = 0.1,
        channel: int | None = None,
        bus_type: str | None = None,
        arbitration_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        if session.trace_id is None:
            raise DynamicStateError("observe_bus_window 需要创建 Session 时提供 BLF")
        if not 0 < around_cursor_seconds <= 60:
            raise DynamicInputError("around_cursor_seconds 必须大于 0 且不超过 60")
        if not 1 <= limit:
            raise DynamicInputError("limit 必须大于等于 1")

        cursor = session.worker.call("get_status").get("playback_cursor")
        window_basis = "explicit_range"
        if start_timestamp is None and end_timestamp is None:
            if not cursor or cursor.get("seconds") is None:
                raise DynamicStateError(
                    "CANoe 尚未提供 Playback Cursor；请启动/暂停 Offline Animation，"
                    "或显式提供 start_timestamp/end_timestamp"
                )
            summary = self._trace.get_summary(session.trace_id)
            trace_start = summary.get("start_timestamp")
            if trace_start is None:
                raise DynamicStateError("BLF 不包含可定位的时间戳")
            center = trace_start + float(cursor["seconds"])
            half_window = around_cursor_seconds / 2
            start_timestamp = center - half_window
            end_timestamp = center + half_window
            window_basis = "canoe_playback_cursor_mapped_to_trace_start"

        result = self._trace.find_messages(
            trace_id=session.trace_id,
            arbitration_id=arbitration_id,
            channel=channel,
            limit=min(limit, MAX_OBSERVATION_RESULTS),
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            bus_type=bus_type,
        )
        frames = [
            {
                "timestamp": item["timestamp"],
                "bus": item["bus_type"],
                "channel": item["channel"],
                "direction": item["direction"],
                "arbitration_id": item["arbitration_id"],
                "arbitration_id_hex": item["arbitration_id_hex"],
                "frame_type": (
                    "error"
                    if item["is_error_frame"]
                    else "can_fd"
                    if item["is_fd"]
                    else item["bus_type"]
                ),
                "dlc": item["dlc"],
                "payload": item["data_hex"],
            }
            for item in result["frames"]
        ]
        return {
            "dynamic_session_id": dynamic_session_id,
            "trace_id": session.trace_id,
            "window_basis": window_basis,
            "playback_cursor": cursor,
            "filters": result["filters"],
            "total_count": result["total_count"],
            "returned_count": len(frames),
            "limit_applied": result["limit_applied"],
            "frames": frames,
        }

    def get_signal(
        self,
        dynamic_session_id: str,
        bus: str,
        channel: int,
        message: str,
        signal: str,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        return session.worker.call(
            "get_signal", _bus(bus), _channel(channel), message, signal
        )

    def get_variable(
        self, dynamic_session_id: str, kind: str, name: str
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        return session.worker.call("get_variable", kind.strip().casefold(), name)

    def read_write_window(
        self,
        dynamic_session_id: str,
        line_limit: int = 50,
        char_limit: int = 12_000,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        return session.worker.call("read_write_window", line_limit, char_limit)

    def get_bus_statistics(
        self, dynamic_session_id: str, channel: int
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        return session.worker.call("get_bus_statistics", _channel(channel))

    def inspect_network_model(
        self,
        dynamic_session_id: str,
        kind: str,
        query: str = "",
        bus: str = "CAN",
        limit: int = 50,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        normalized_kind = kind.strip().casefold()
        if normalized_kind not in {"database", "node", "message", "signal"}:
            raise DynamicInputError("kind 仅支持 database、node、message、signal")
        if not 1 <= limit:
            raise DynamicInputError("limit 必须大于等于 1")
        if normalized_kind in {"message", "signal"} and session.trace_id is not None:
            if not query.strip():
                raise DynamicInputError(
                    "使用共享 Trace Database 浏览 message/signal 时 query 不能为空"
                )
            result = self._trace.search_database(
                session.trace_id, query, min(limit, 20)
            )
            return {
                "dynamic_session_id": dynamic_session_id,
                "source": "shared_trace_database",
                "kind": normalized_kind,
                **result,
            }
        return session.worker.call(
            "inspect_network_model",
            normalized_kind,
            query,
            bus.upper(),
            min(limit, 100),
        )

    def get_debug_snapshot(
        self,
        dynamic_session_id: str,
        signals: list[dict[str, Any]] | None = None,
        variables: list[dict[str, Any]] | None = None,
        write_line_limit: int = 50,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        if (
            len(signals or []) > MAX_SNAPSHOT_ITEMS
            or len(variables or []) > MAX_SNAPSHOT_ITEMS
        ):
            raise DynamicInputError("快照最多读取 10 个 Signal 和 10 个 Variable")
        return session.worker.call(
            "get_debug_snapshot", signals or [], variables or [], write_line_limit
        )

    def set_signal(
        self,
        dynamic_session_id: str,
        bus: str,
        channel: int,
        message: str,
        signal: str,
        value: float,
        raw_value: bool = False,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.SIGNAL_WRITE)
        return session.worker.call(
            "set_signal",
            _bus(bus),
            _channel(channel),
            message,
            signal,
            value,
            raw_value,
        )

    def set_variable(
        self, dynamic_session_id: str, kind: str, name: str, value: Any
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.VARIABLE_WRITE)
        return session.worker.call("set_variable", kind.strip().casefold(), name, value)

    def call_capl_function(
        self, dynamic_session_id: str, name: str, arguments: list[Any] | None = None
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.CAPL_CALL)
        return session.worker.call("call_capl_function", name, arguments or [])

    def transmit_can_frame(
        self,
        dynamic_session_id: str,
        channel: int,
        arbitration_id: int,
        data: str,
        is_extended: bool = False,
        is_fd: bool = False,
        brs: bool = False,
        count: int = 1,
        interval_ms: int = 0,
    ) -> dict[str, Any]:
        session = self._authorized(
            dynamic_session_id, RuntimeCapability.FRAME_INJECTION
        )
        payload = _payload(data)
        _validate_can_frame(
            arbitration_id, payload, is_extended, is_fd, brs, count, interval_ms
        )
        return session.worker.call(
            "transmit_can_frame",
            _channel(channel),
            arbitration_id,
            payload,
            is_extended,
            is_fd,
            brs,
            count,
            interval_ms,
            timeout=max(30, count * interval_ms / 1000 + 15),
        )

    def send_diagnostic_request(
        self,
        dynamic_session_id: str,
        ecu_qualifier: str,
        request: str,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.DIAGNOSTIC)
        if not ecu_qualifier.strip() or not request.strip():
            raise DynamicInputError("ecu_qualifier 和 request 不能为空")
        if not 0.1 <= timeout_seconds <= 55:
            raise DynamicInputError("timeout_seconds 必须在 0.1 到 55 之间")
        return session.worker.call(
            "send_diagnostic_request",
            ecu_qualifier,
            request,
            timeout_seconds,
            timeout=timeout_seconds + 15,
        )

    def configure_replay(
        self, dynamic_session_id: str, block_name: str, source_path: str
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.REPLAY)
        if not block_name.strip():
            raise DynamicInputError("block_name 不能为空")
        source = _required_file(source_path, "Replay Source")
        return session.worker.call("configure_replay", block_name, str(source))

    def control_replay(
        self, dynamic_session_id: str, block_name: str, action: str
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.REPLAY)
        result = session.worker.call(
            "control_replay", block_name, action.strip().casefold()
        )
        _track_operation(
            session, f"replay:{block_name}", result.get("state") == "running"
        )
        return result

    def manage_logging(
        self,
        dynamic_session_id: str,
        action: str,
        output_path: str | None = None,
        block_name: str | None = None,
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.LOGGING)
        normalized_action = action.strip().casefold()
        normalized_output = None
        if output_path is not None:
            normalized_output = str(Path(output_path).expanduser().resolve())
        result = session.worker.call(
            "manage_logging", normalized_action, normalized_output, block_name
        )
        if normalized_action in {"start", "stop"}:
            _track_operation(session, "logging", normalized_action == "start")
        return result

    def list_tests(self, dynamic_session_id: str, limit: int = 50) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        if limit < 1:
            raise DynamicInputError("limit 必须大于等于 1")
        return session.worker.call("list_tests", min(limit, MAX_TEST_RESULTS))

    def start_test(
        self, dynamic_session_id: str, scope: str, name: str | None = None
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.TEST)
        result = session.worker.call("start_test", scope.strip().casefold(), name)
        _track_operation(session, "test", True)
        return result

    def get_test_result(
        self, dynamic_session_id: str, name: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        session = self._authorized(dynamic_session_id, RuntimeCapability.OBSERVATION)
        if limit < 1:
            raise DynamicInputError("limit 必须大于等于 1")
        return session.worker.call(
            "get_test_result", name, min(limit, MAX_TEST_RESULTS)
        )

    def _get(
        self, dynamic_session_id: str, allow_closed: bool = False
    ) -> DynamicSession:
        session = self._sessions.get(dynamic_session_id)
        if session is None:
            raise DynamicSessionNotFoundError(
                f"未知 dynamic_session_id: {dynamic_session_id}"
            )
        if session.closed and not allow_closed:
            raise DynamicStateError(f"Dynamic Session 已关闭：{dynamic_session_id}")
        return session

    def _authorized(
        self, dynamic_session_id: str, capability: RuntimeCapability
    ) -> DynamicSession:
        session = self._get(dynamic_session_id)
        require_capability(session.runtime_capabilities, capability)
        return session

    def _status_payload(
        self,
        session: DynamicSession,
        backend: dict[str, Any],
        created: bool = False,
    ) -> dict[str, Any]:
        trace_status = None
        if session.trace_id is not None:
            trace_status = self._trace.get_load_status(session.trace_id)
        return {
            "dynamic_session_id": session.dynamic_session_id,
            "created": created,
            "closed": session.closed,
            "mode": session.mode.value,
            "instance_mode": session.instance_mode.value,
            "owns_canoe_process": session.owns_canoe_process,
            "can_quit": session.can_quit,
            "configuration_origin": session.configuration_origin.value,
            "configuration": (
                str(session.original_configuration)
                if session.original_configuration
                else None
            ),
            "shadow_configuration": (
                str(session.shadow_configuration)
                if session.shadow_configuration
                else None
            ),
            "blf_path": str(session.blf_path) if session.blf_path else None,
            "database_count": len(session.database_specs),
            "trace": trace_status,
            "runtime_policy": session.policy_dict(),
            "active_operations": sorted(session.active_operations),
            "hardware": (
                "configuration_defined_not_probed"
                if session.mode is DynamicMode.ONLINE
                else "not_active_offline_mode"
            ),
            "backend": backend,
        }


def _default_temp_root() -> Path:
    import os

    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".cache"
    return base / "AutomotiveNetworkDebugAgent" / "dynamic-sessions"


def _required_file(path_value: str, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise DynamicInputError(f"{label}文件不存在：{path}")
    return path


def _optional_file(path_value: str | None, label: str) -> Path | None:
    return _required_file(path_value, label) if path_value else None


def _resolve_instance_mode(value: str) -> InstanceMode:
    try:
        return InstanceMode(value.strip().casefold())
    except ValueError as exc:
        raise DynamicInputError("instance_mode 仅支持 managed 或 attached") from exc


def _resolve_mode(
    mode: str,
    configuration_path: str | None,
    instance_mode: InstanceMode,
) -> DynamicMode:
    try:
        requested = DynamicMode(mode.strip().casefold())
    except ValueError as exc:
        raise DynamicInputError("mode 仅支持 auto、offline、online") from exc
    if requested is DynamicMode.AUTO and instance_mode is InstanceMode.ATTACHED:
        raise DynamicInputError(
            "attached 模式必须明确指定 mode=offline 或 mode=online，不能猜测"
        )
    if requested is DynamicMode.AUTO:
        return DynamicMode.OFFLINE
    if (
        requested is DynamicMode.ONLINE
        and not configuration_path
        and instance_mode is InstanceMode.MANAGED
    ):
        raise DynamicInputError("online Session 必须提供 configuration_path")
    return requested


def _database_specs(
    database_files: tuple[Path, ...], mappings: list[dict[str, Any]] | None
) -> tuple[DatabaseSpec, ...]:
    mapping_by_path: dict[str, dict[str, Any]] = {}
    for item in mappings or []:
        if "path" not in item:
            raise DynamicInputError("database_mappings 每项必须包含 path")
        key = str(Path(str(item["path"])).expanduser().resolve()).casefold()
        mapping_by_path[key] = item
    specs = []
    for index, path in enumerate(database_files, 1):
        item = mapping_by_path.get(str(path).casefold(), {})
        default_bus = "lin" if path.suffix.casefold() == ".ldf" else "can"
        bus = str(item.get("bus", default_bus)).strip().casefold()
        if bus not in {"can", "lin"}:
            raise DynamicInputError("database bus 仅支持 can 或 lin")
        channel = _channel(int(item.get("channel", 1)))
        network_name = str(
            item.get("network_name", f"ANDA_{bus.upper()}{channel}_{index}")
        ).strip()
        if not network_name:
            raise DynamicInputError("database network_name 不能为空")
        specs.append(DatabaseSpec(path, bus, channel, network_name))
    unknown = sorted(
        set(mapping_by_path) - {str(path).casefold() for path in database_files}
    )
    if unknown:
        raise DynamicInputError("database_mappings 包含未列入 database_paths 的路径")
    return tuple(specs)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_original(session: DynamicSession) -> bool:
    if session.original_configuration is None:
        return True
    try:
        return (
            _file_digest(session.original_configuration)
            == session.original_configuration_digest
        )
    except OSError:
        return False


def _remove_shadow(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _remove_directory(path: Path) -> bool:
    for attempt in range(10):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt == 9:
                return False
            time.sleep(0.2)
    return False


def _track_operation(session: DynamicSession, name: str, active: bool) -> None:
    if active:
        session.active_operations.add(name)
    else:
        session.active_operations.discard(name)


def _bus(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"CAN", "LIN"}:
        raise DynamicInputError("bus 仅支持 CAN 或 LIN")
    return normalized


def _channel(value: int) -> int:
    if value < 1 or value > 255:
        raise DynamicInputError("channel 使用 1-based 编号，必须在 1 到 255 之间")
    return value


def _payload(value: str) -> bytes:
    compact = value.replace(" ", "").replace("_", "").replace("-", "")
    if compact.casefold().startswith("0x"):
        compact = compact[2:]
    if len(compact) % 2:
        raise DynamicInputError("data 必须包含完整字节的十六进制文本")
    try:
        return bytes.fromhex(compact)
    except ValueError as exc:
        raise DynamicInputError("data 必须是十六进制字节文本") from exc


def _validate_can_frame(
    arbitration_id: int,
    payload: bytes,
    is_extended: bool,
    is_fd: bool,
    brs: bool,
    count: int,
    interval_ms: int,
) -> None:
    maximum_id = 0x1FFFFFFF if is_extended else 0x7FF
    if not 0 <= arbitration_id <= maximum_id:
        raise DynamicInputError(
            f"arbitration_id 超出{'扩展' if is_extended else '标准'} CAN ID 范围"
        )
    maximum_length = 64 if is_fd else 8
    if len(payload) > maximum_length:
        raise DynamicInputError(
            f"{'CAN FD' if is_fd else 'Classic CAN'} Payload 最多 {maximum_length} 字节"
        )
    if brs and not is_fd:
        raise DynamicInputError("brs 仅适用于 CAN FD")
    if not 1 <= count <= MAX_FRAME_COUNT:
        raise DynamicInputError(f"count 必须在 1 到 {MAX_FRAME_COUNT} 之间")
    if interval_ms < 0:
        raise DynamicInputError("interval_ms 不能小于 0")
    if count > 1 and interval_ms < 10:
        raise DynamicInputError("重复发送时 interval_ms 至少为 10")
    if (count - 1) * interval_ms > MAX_FRAME_DURATION_MS:
        raise DynamicInputError("一次 Raw Frame Injection 总持续时间不能超过 10 秒")


__all__ = ["DynamicSessionManager"]
