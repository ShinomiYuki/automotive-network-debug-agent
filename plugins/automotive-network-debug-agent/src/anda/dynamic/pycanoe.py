"""py-canoe 26.4.1 Adapter；隔离版本相关 API 与 CANoe COM 细节。"""

from __future__ import annotations

import csv
import gc
import platform
import subprocess
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from anda.common.errors import (
    DynamicBackendError,
    DynamicInputError,
    DynamicStateError,
    DynamicUnsupportedError,
)
from anda.trace.dbc import DATABASE_EXTENSIONS, NetworkDatabase

MAX_WRITE_LINES = 200
MAX_WRITE_CHARS = 16_000
MAX_MODEL_RESULTS = 100
MAX_CAPL_ARGUMENTS = 10
CANOE_TICKS_PER_SECOND = 100_000
MAX_VALUE_ITEMS = 100
MAX_VALUE_CHARS = 2_000


def _utc_timestamp() -> float:
    return datetime.now(UTC).timestamp()


def _exception_details(exc: BaseException) -> dict[str, Any]:
    details: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc)[:2000],
    }
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        details["hresult"] = args[0]
    if len(args) > 2 and isinstance(args[2], tuple):
        info = args[2]
        if len(info) > 1 and info[1]:
            details["source"] = str(info[1])[:500]
        if len(info) > 2 and info[2]:
            details["description"] = str(info[2])[:2000]
    return details


def _bounded_text(text: str | None, line_limit: int, char_limit: int) -> dict[str, Any]:
    normalized = text or ""
    lines = normalized.splitlines()
    applied_lines = min(max(1, line_limit), MAX_WRITE_LINES)
    applied_chars = min(max(1, char_limit), MAX_WRITE_CHARS)
    tail = "\n".join(lines[-applied_lines:])
    if len(tail) > applied_chars:
        tail = tail[-applied_chars:]
    return {
        "text": tail,
        "total_line_count": len(lines),
        "returned_line_count": len(tail.splitlines()),
        "line_limit_applied": applied_lines,
        "char_limit_applied": applied_chars,
        "truncated": len(lines) > applied_lines
        or len("\n".join(lines[-applied_lines:])) > applied_chars,
    }


def _bounded_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:MAX_VALUE_ITEMS]).hex(" ").upper()
    if depth >= 3:
        return str(value)[:MAX_VALUE_CHARS]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _bounded_value(item, depth + 1)
            for key, item in list(value.items())[:MAX_VALUE_ITEMS]
        }
    if isinstance(value, set):
        value = sorted(value, key=repr)
    if isinstance(value, (list, tuple)):
        return [
            _bounded_value(item, depth + 1) for item in list(value)[:MAX_VALUE_ITEMS]
        ]
    return str(value)[:MAX_VALUE_CHARS]


class PyCanoeBackend:
    """持久 CANoe Application Adapter；实例必须只在其 STA 线程中使用。"""

    PY_CANOE_VERSION = "26.4.1"

    def __init__(self) -> None:
        self._canoe: Any = None
        self._wait: Any = None
        self._bus_type: Any = None
        self._version: dict[str, Any] = {}
        self._mode = "offline"
        self._configuration_origin = "temporary_configuration"
        self._temporary_configuration_path: str | None = None
        self._animation_state = "stopped"
        self._replay_states: dict[str, str] = {}
        self._active_logging_blocks: set[str] = set()
        self._active_test_modules: list[Any] = []
        self._raw_frame_capl_function = "ANDA_TransmitCanFrame"
        self._initialization_warnings: list[str] = []
        self._mode_verified = False
        self._instance_mode = "managed"
        self._owns_canoe_process = False
        self._owned_process_ids: set[int] = set()
        self._can_quit = False
        self._measurement_started_by_session = False
        self._network_database: NetworkDatabase | None = None
        self._network_database_paths: tuple[Path, ...] = ()

    @classmethod
    def probe_environment(cls) -> dict[str, Any]:
        """不启动 CANoe，报告安装、COM 注册与 Adapter 能力边界。"""
        py_canoe_available = find_spec("py_canoe") is not None
        pywin32_available = find_spec("win32com") is not None
        try:
            py_canoe_version = version("py-canoe") if py_canoe_available else None
        except PackageNotFoundError:
            py_canoe_version = None

        com_registration: dict[str, Any] = {"registered": False}
        if platform.system() == "Windows":
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_CLASSES_ROOT, r"CANoe.Application\CLSID"
                ) as key:
                    clsid = winreg.QueryValueEx(key, None)[0]
                with winreg.OpenKey(
                    winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32"
                ) as key:
                    server = winreg.QueryValueEx(key, None)[0]
                executable = str(server).strip().strip('"')
                com_registration = {
                    "registered": True,
                    "progid": "CANoe.Application",
                    "clsid": clsid,
                    "local_server": executable,
                    "local_server_exists": Path(executable).is_file(),
                }
                if Path(executable).is_file():
                    try:
                        import win32api

                        file_info = win32api.GetFileVersionInfo(executable, "\\")
                        ms = int(file_info["FileVersionMS"])
                        ls = int(file_info["FileVersionLS"])
                        com_registration["file_version"] = ".".join(
                            str(value)
                            for value in (
                                ms >> 16,
                                ms & 0xFFFF,
                                ls >> 16,
                                ls & 0xFFFF,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - 版本信息是可选探测
                        com_registration["file_version_error"] = str(exc)[:500]
            except OSError as exc:
                com_registration["error"] = str(exc)

        backend_available = (
            platform.system() == "Windows"
            and py_canoe_available
            and py_canoe_version == cls.PY_CANOE_VERSION
            and pywin32_available
            and bool(com_registration["registered"])
        )
        conditional = (
            "conditional_valid_canoe_license_and_configuration"
            if backend_available
            else "unsupported_backend_unavailable"
        )
        return {
            "backend": "py-canoe",
            "backend_available": backend_available,
            "windows": platform.system() == "Windows",
            "platform": platform.platform(),
            "py_canoe": {
                "installed": py_canoe_available,
                "version": py_canoe_version,
                "validated_version": cls.PY_CANOE_VERSION,
                "version_matches": py_canoe_version == cls.PY_CANOE_VERSION,
            },
            "pywin32_available": pywin32_available,
            "canoe_com": com_registration,
            "capabilities": {
                "offline": conditional,
                "replay": conditional,
                "test": conditional,
                "diagnostic": conditional,
                "raw_can_injection": (
                    "conditional_capl_function_ANDA_TransmitCanFrame"
                    if backend_available
                    else "unsupported_backend_unavailable"
                ),
            },
            "license": "unverified_until_dynamic_session",
        }

    def initialize(self, spec: dict[str, Any]) -> dict[str, Any]:
        try:
            installed_version = version("py-canoe")
        except PackageNotFoundError as exc:
            raise DynamicUnsupportedError(
                "py-canoe==26.4.1 不可用；请在选定 Python 环境安装 canoe 可选依赖"
            ) from exc
        if installed_version != self.PY_CANOE_VERSION:
            raise DynamicUnsupportedError(
                "Dynamic Core 只验证了 py-canoe==26.4.1；"
                f"当前安装版本为 {installed_version}"
            )
        try:
            from py_canoe import BusType, CANoe, wait
        except Exception as exc:
            raise DynamicUnsupportedError(
                "py-canoe==26.4.1 不可用；请在选定 Python 环境安装 canoe 可选依赖"
            ) from exc

        self._wait = wait
        self._bus_type = BusType
        self._mode = spec["mode"]
        self._instance_mode = spec.get("instance_mode", "managed")
        self._configuration_origin = spec["configuration_origin"]
        self._raw_frame_capl_function = spec.get(
            "raw_frame_capl_function", "ANDA_TransmitCanFrame"
        )
        before = _canoe_processes()
        self._canoe = CANoe(py_canoe_log_dir=spec["temp_directory"])
        active_com = _has_active_canoe_application()
        if self._instance_mode == "managed":
            if before or active_com:
                raise DynamicStateError(
                    "managed 模式检测到既有 CANoe 实例；拒绝复用用户进程，"
                    "请关闭该实例或明确使用 attached"
                )
        elif self._instance_mode == "attached":
            if len(before) != 1 or not active_com:
                raise DynamicStateError(
                    "attached 模式要求恰好一个已运行且已注册 COM 的 CANoe 实例；"
                    "无法可靠确定目标时拒绝附着"
                )
        else:
            raise DynamicInputError("instance_mode 仅支持 managed 或 attached")

        if self._instance_mode == "attached":
            if not self._canoe.attach_to_active_application():
                raise DynamicBackendError("无法附着到已运行的 CANoe")
            self._verify_attached_mode()
            self._version = self._canoe.get_canoe_version_info()
            self._owns_canoe_process = False
            self._can_quit = False
            return self.get_status()

        configuration_path = spec.get("configuration_path")
        if configuration_path:
            opened = self._canoe.open(
                configuration_path,
                visible=bool(spec.get("visible", False)),
                auto_save=False,
                prompt_user=False,
                timeout=60,
            )
        else:
            opened = self._canoe.new(
                auto_save=False,
                prompt_user=False,
                timeout=30,
            )
            if opened:
                self._canoe.application.visible = bool(spec.get("visible", False))
        if not opened:
            raise DynamicBackendError("CANoe Configuration 打开或创建失败")

        self._owns_canoe_process = True
        self._can_quit = True
        after = _canoe_processes()
        if len(after) != 1:
            raise DynamicStateError(
                "managed 模式启动后无法唯一确认 CANoe 进程所有权；拒绝继续"
            )
        self._owned_process_ids = set(after) - set(before)

        self._version = self._canoe.get_canoe_version_info()
        if not configuration_path:
            temporary_configuration = (
                Path(spec["temp_directory"]) / "anda-temporary.cfg"
            )
            saved = self._canoe.application.configuration.save_as(
                str(temporary_configuration),
                int(self._version["major"]),
                0,
                prompt_user=False,
                create_dir=True,
            )
            if saved:
                self._temporary_configuration_path = str(temporary_configuration)
            else:
                self._initialization_warnings.append(
                    "CANoe 拒绝保存 Temporary Configuration；"
                    "Session 继续使用不落盘的临时配置"
                )
        self._set_configuration_mode(self._mode)
        self._prepare_databases(spec.get("databases", []))
        blf_path = spec.get("blf_path")
        if blf_path and not self._canoe.add_offline_source_log_file(blf_path):
            raise DynamicBackendError(f"无法添加 BLF Offline Source：{blf_path}")
        return self.get_status()

    def _verify_attached_mode(self) -> None:
        configuration = self._canoe.application.configuration
        com_object = configuration.com_object
        mode_member = (
            "mode"
            if hasattr(com_object, "mode")
            else "Mode"
            if hasattr(com_object, "Mode")
            else None
        )
        if mode_member is None:
            raise DynamicStateError(
                "当前 CANoe COM 类型库不能确认 attached Configuration 模式；拒绝附着"
            )
        actual = int(getattr(com_object, mode_member))
        actual_mode = "offline" if actual == 1 else "online" if actual == 0 else None
        if actual_mode is None or actual_mode != self._mode:
            raise DynamicStateError(
                f"attached Configuration 模式为 {actual_mode or actual}，"
                f"与明确请求的 {self._mode} 不一致"
            )
        self._mode_verified = True

    def _set_configuration_mode(self, mode: str) -> None:
        configuration = self._canoe.application.configuration
        com_object = configuration.com_object
        mode_member = (
            "mode"
            if hasattr(com_object, "mode")
            else "Mode"
            if hasattr(com_object, "Mode")
            else None
        )
        if mode_member is not None:
            expected = 1 if mode == "offline" else 0
            try:
                setattr(com_object, mode_member, expected)
                actual = int(getattr(com_object, mode_member))
            except Exception as exc:
                raise DynamicBackendError(
                    f"无法把 CANoe Configuration 切换为 {mode}：{exc}"
                ) from exc
            if actual != expected:
                raise DynamicBackendError(
                    f"CANoe Configuration Mode 校验失败：期望 {expected}，实际 {actual}"
                )
            self._mode_verified = True
            return
        message = (
            "当前 CANoe COM 类型库不提供 Configuration.Mode；"
            "Adapter 将依赖 Offline Source/现有配置决定模式"
        )
        self._initialization_warnings.append(message)
        if mode == "online":
            raise DynamicUnsupportedError(
                "当前 CANoe 版本无法通过 COM 确认 Online Mode；"
                "为避免误用真实总线，拒绝创建 online Session"
            )

    def _prepare_databases(self, databases: list[dict[str, Any]]) -> None:
        if not databases:
            return
        created_networks: set[str] = set()
        for item in databases:
            path = item["path"]
            network = item["network_name"]
            if (
                self._configuration_origin == "temporary_configuration"
                and network not in created_networks
            ):
                bus_type = (
                    self._bus_type.CAN if item["bus"] == "can" else self._bus_type.LIN
                )
                created = self._canoe.add_netWork(
                    network,
                    network_type=bus_type,
                    sw_channel=int(item["channel"]),
                )
                if created is None:
                    self._initialization_warnings.append(
                        f"未能显式创建网络 {network}，将尝试按 Channel 添加数据库"
                    )
                else:
                    created_networks.add(network)
            target: str | int = (
                network if network in created_networks else int(item["channel"])
            )
            if not self._canoe.add_database(path, target):
                raise DynamicBackendError(f"无法添加网络数据库：{path}")

    def get_status(self) -> dict[str, Any]:
        if self._canoe is None or self._canoe.application is None:
            return {"connected": False}
        measurement = self._canoe.application.measurement
        running = bool(measurement.running)
        cursor = self._read_cursor() if running else None
        configuration = self._canoe.application.configuration
        return {
            "connected": True,
            "instance_mode": self._instance_mode,
            "owns_canoe_process": self._owns_canoe_process,
            "can_quit": self._can_quit,
            "canoe_version": self._version,
            "license_view_only": "view only"
            in str(self._version.get("full_name", "")).casefold(),
            "configuration": {
                "full_name": str(getattr(configuration, "full_name", "") or ""),
                "name": str(getattr(configuration, "name", "") or ""),
                "origin": self._configuration_origin,
                "temporary_path": self._temporary_configuration_path,
                "modified": bool(getattr(configuration, "modified", False)),
            },
            "mode": self._mode,
            "mode_verified": self._mode_verified,
            "measurement": "running" if running else "stopped",
            "animation": self._animation_state,
            "playback_cursor": cursor,
            "replay_count": len(self._replay_states),
            "replay": dict(list(self._replay_states.items())[:MAX_MODEL_RESULTS]),
            "active_test_count": len(self._active_test_modules),
            "active_tests": [
                self._test_name(item)
                for item in self._active_test_modules[:MAX_MODEL_RESULTS]
            ],
            "initialization_warnings": list(self._initialization_warnings),
        }

    def _read_cursor(self) -> dict[str, Any] | None:
        try:
            simulation = self._canoe.application.simulation
            low = int(simulation.current_time) & 0xFFFFFFFF
            high = int(simulation.current_time_high) & 0xFFFFFFFF
            ticks = (high << 32) | low
            return {
                "ticks": ticks,
                "ticks_per_second": CANOE_TICKS_PER_SECOND,
                "seconds": ticks / CANOE_TICKS_PER_SECOND,
            }
        except Exception:  # noqa: BLE001 - 旧版 COM 可能抛出多种 pywin32 异常
            return None

    def compile_capl(self, write_line_limit: int = 40) -> dict[str, Any]:
        try:
            result = self._canoe.compile_all_capl_nodes(wait_time=1)
            if result is None:
                payload = self._last_capl_compile_result()
                # py-canoe 已吞掉底层异常时，即便最后一次 CompileResult 恰好为
                # 成功也不能把本次调用判为成功，否则可能复用上一次的陈旧结果。
                payload["success"] = False
                payload["adapter_error"] = "py-canoe compile_all_capl_nodes 返回 None"
                payload["write_window"] = self.read_write_window(write_line_limit, 8000)
                return payload
            payload = self._capl_compile_result_payload(result)
            payload["write_window"] = self.read_write_window(write_line_limit, 8000)
            return payload
        except Exception as exc:
            raise DynamicBackendError(f"CAPL 编译失败：{exc}") from exc

    def _last_capl_compile_result(self) -> dict[str, Any]:
        """即使 py-canoe 吞掉 Compile 异常，也回读 CANoe 最后一次编译结果。"""
        try:
            result = self._canoe.application.capl.compile_result
            return self._capl_compile_result_payload(result)
        except Exception as exc:  # noqa: BLE001 - 不覆盖原始编译失败
            return {
                "success": False,
                "result_code": None,
                "error_count": None,
                "warning_count": None,
                "error": "CANoe CompileResult 无法读取",
                "node": "",
                "source_file": "",
                "compile_result_error": _exception_details(exc),
            }

    @staticmethod
    def _capl_compile_result_payload(result: Any) -> dict[str, Any]:
        result_code = int(result.result)
        return {
            "success": result_code == 0,
            "result_code": result_code,
            "error_count": 0 if result_code == 0 else None,
            "warning_count": None,
            "error": str(result.error_message or "")[:2000],
            "node": str(result.node_name or "")[:500],
            "source_file": str(result.source_file or "")[:1000],
        }

    def control_measurement(self, action: str) -> dict[str, Any]:
        was_running = bool(self._canoe.get_measurement_running_status())
        if action == "start":
            success = self._canoe.start_measurement(timeout=30)
            if success and not was_running:
                self._measurement_started_by_session = True
        elif action == "stop":
            success = self._canoe.stop_measurement(timeout=30)
            if success:
                self._animation_state = "stopped"
                self._measurement_started_by_session = False
        elif action == "reset":
            success = self._canoe.reset_measurement(timeout=30)
        else:
            raise DynamicInputError("measurement action 仅支持 start、stop、reset")
        if not success:
            raise DynamicBackendError(f"CANoe Measurement {action} 失败")
        return self.get_status()

    def control_offline_animation(
        self, action: str, animation_delay_ms: int = 100
    ) -> dict[str, Any]:
        if self._mode != "offline":
            raise DynamicStateError("Offline Animation 仅允许 offline Session")
        measurement = self._canoe.application.measurement
        try:
            if action == "start":
                success = self._canoe.start_measurement_in_animation_mode(
                    animation_delay=animation_delay_ms,
                    timeout=30,
                )
                if not success:
                    raise DynamicBackendError("CANoe Offline Animation 启动失败")
                self._animation_state = "running"
                self._measurement_started_by_session = True
            elif action == "pause":
                if not self._canoe.break_measurement_in_offline_mode():
                    raise DynamicBackendError("CANoe Offline Animation 暂停失败")
                self._animation_state = "paused"
            elif action == "resume":
                if self._animation_state != "paused":
                    raise DynamicStateError("只有 paused Animation 可以 resume")
                measurement.com_object.Animate()
                self._animation_state = "running"
            elif action == "step":
                if not self._canoe.step_measurement_event_in_single_step():
                    raise DynamicBackendError("CANoe Offline Animation 单步失败")
                self._animation_state = "paused"
            elif action == "reset":
                if not self._canoe.reset_measurement_in_offline_mode():
                    raise DynamicBackendError("CANoe Offline Animation 重置失败")
                self._animation_state = "paused"
            elif action == "stop":
                if not self._canoe.stop_measurement(timeout=30):
                    raise DynamicBackendError("CANoe Offline Animation 停止失败")
                self._animation_state = "stopped"
                self._measurement_started_by_session = False
            else:
                raise DynamicInputError(
                    "offline animation action 仅支持 start、pause、resume、step、reset、stop"
                )
        except (DynamicBackendError, DynamicInputError, DynamicStateError):
            raise
        except Exception as exc:
            raise DynamicBackendError(
                f"CANoe Offline Animation {action} 失败：{exc}"
            ) from exc
        return self.get_status()

    def wait_runtime_event(self, event: str, timeout_seconds: float) -> dict[str, Any]:
        allowed = {
            "measurement_started",
            "measurement_stopped",
            "test_started",
            "test_stopped",
            "test_verdict_fail",
        }
        if event not in allowed:
            raise DynamicInputError(
                "event 仅支持 measurement_started、measurement_stopped、"
                "test_started、test_stopped、test_verdict_fail"
            )
        deadline = time.monotonic() + timeout_seconds
        matched = False
        evidence = "pywin32_com_event_sink"
        while True:
            measurement_events = self._canoe.application.measurement.measurement_events
            if event == "measurement_started":
                matched = bool(measurement_events.START)
            elif event == "measurement_stopped":
                matched = bool(measurement_events.STOP)
            else:
                matched = any(
                    self._test_event(item, event) for item in self._active_test_modules
                )
            if matched or time.monotonic() >= deadline:
                break
            self._canoe.application.pump_messages()
            self._wait(min(0.02, max(0.0, deadline - time.monotonic())))
        return {
            "event": event,
            "matched": matched,
            "timed_out": not matched,
            "timeout_seconds": timeout_seconds,
            "observed_at": _utc_timestamp(),
            "evidence": evidence,
            "status": self.get_status(),
        }

    @staticmethod
    def _test_event(test_module: Any, event: str) -> bool:
        events = test_module.test_module_events
        return bool(
            events.TM_STARTED
            if event == "test_started"
            else events.TM_STOPPED
            if event == "test_stopped"
            else events.TC_FAIL
        )

    def get_signal(
        self, bus: str, channel: int, message: str, signal: str
    ) -> dict[str, Any]:
        physical, timestamp = self._canoe.get_signal_value(
            bus, channel, message, signal, raw_value=False, return_timestamp=True
        )
        raw = self._canoe.get_signal_value(
            bus, channel, message, signal, raw_value=True
        )
        if physical is None and raw is None:
            raise DynamicBackendError(
                f"CANoe 未返回 Signal：{bus}{channel} {message}::{signal}"
            )
        return {
            "bus": bus,
            "channel": channel,
            "message": message,
            "signal": signal,
            "physical_value": physical,
            "raw_value": raw,
            "timestamp": timestamp,
            "timestamp_source": "py_canoe_read_time_utc",
        }

    def get_variable(self, kind: str, name: str) -> dict[str, Any]:
        if kind == "system":
            value, timestamp = self._canoe.get_system_variable_value(
                name, return_timestamp=True
            )
        elif kind == "environment":
            value, timestamp = self._canoe.get_environment_variable_value(
                name, return_timestamp=True
            )
        else:
            raise DynamicInputError("variable kind 仅支持 system 或 environment")
        return {
            "kind": kind,
            "name": name,
            "value": _bounded_value(value),
            "timestamp": timestamp,
            "timestamp_source": "py_canoe_read_time_utc",
        }

    def read_write_window(
        self, line_limit: int = 50, char_limit: int = 12_000
    ) -> dict[str, Any]:
        return _bounded_text(
            self._canoe.read_text_from_write_window(), line_limit, char_limit
        )

    def get_bus_statistics(self, channel: int) -> dict[str, Any]:
        result = self._canoe.get_can_bus_statistics(channel)
        if not result:
            raise DynamicUnsupportedError(
                f"当前 Configuration/CANoe 不提供 CAN{channel} Bus Statistics"
            )
        return {"bus": "can", "channel": channel, "statistics": result}

    def inspect_safe_capabilities(self) -> dict[str, Any]:
        """只枚举可用对象；不启动 Replay、诊断、发送或测试。"""
        result: dict[str, Any] = {}
        try:
            blocks = self._replay_blocks()
            result["replay"] = {
                "status": "supported",
                "block_count": len(blocks),
                "block_names": list(blocks)[:MAX_MODEL_RESULTS],
            }
        except Exception as exc:  # noqa: BLE001 - 探测项必须相互隔离
            result["replay"] = {
                "status": "unsupported",
                "error": _exception_details(exc),
            }
        try:
            environments = self._canoe.get_test_environments()
            result["test"] = {
                "status": "supported",
                "environment_count": len(environments),
                "environment_names": list(environments)[:MAX_MODEL_RESULTS],
            }
        except Exception as exc:  # noqa: BLE001
            result["test"] = {
                "status": "unsupported",
                "error": _exception_details(exc),
            }
        try:
            networks = self._canoe.application.networks
            networks.fetch_diagnostic_devices()
            names = list(networks.diagnostic_devices)
            result["diagnostic"] = {
                "status": "supported",
                "device_count": len(names),
                "device_names": names[:MAX_MODEL_RESULTS],
                "request_sent": False,
            }
        except Exception as exc:  # noqa: BLE001
            result["diagnostic"] = {
                "status": "unsupported",
                "request_sent": False,
                "error": _exception_details(exc),
            }
        try:
            blocks = self._canoe.get_logging_blocks()
            result["logging"] = {
                "status": "supported",
                "block_count": len(blocks),
            }
        except Exception as exc:  # noqa: BLE001
            result["logging"] = {
                "status": "unsupported",
                "error": _exception_details(exc),
            }
        try:
            function = self._get_capl_function(self._raw_frame_capl_function)
            result["raw_can_injection"] = {
                "status": "supported"
                if int(function.ParameterCount) == 4
                else "unsupported",
                "backend": f"capl:{self._raw_frame_capl_function}",
                "parameter_count": int(function.ParameterCount),
                "frame_sent": False,
            }
        except Exception as exc:  # noqa: BLE001
            result["raw_can_injection"] = {
                "status": "unsupported",
                "backend": f"capl:{self._raw_frame_capl_function}",
                "frame_sent": False,
                "error": _exception_details(exc),
            }
        return result

    def inspect_network_model(
        self, kind: str, query: str, bus: str, limit: int
    ) -> dict[str, Any]:
        needle = query.casefold()
        applied_limit = min(limit, MAX_MODEL_RESULTS)
        if kind == "database":
            values = []
            for path in self._canoe.get_simulation_database_paths():
                if not needle or needle in str(path).casefold():
                    values.append({"name": Path(path).name, "path": str(path)})
        elif kind == "node":
            raw = self._canoe.get_bus_nodes_info(bus)
            values = []
            for name, item in raw.items():
                plain = {
                    key: value
                    for key, value in item.items()
                    if key != "com_obj"
                    and isinstance(value, (str, int, float, bool, type(None)))
                }
                if (
                    not needle
                    or needle in str(name).casefold()
                    or needle in str(plain).casefold()
                ):
                    values.append(plain)
        elif kind in {"message", "signal"}:
            database = self._current_network_database()
            values = []
            for message in database.messages:
                if bus and message.bus_type != bus.casefold():
                    continue
                if kind == "message":
                    item = {
                        "database_file": message.path.name,
                        "bus_type": message.bus_type,
                        "frame_id": message.frame_id,
                        "frame_id_hex": f"0x{message.frame_id:X}",
                        "message": message.name,
                        "signal_count": len(message.signal_names),
                    }
                    haystack = f"{message.name} {message.frame_id:X} {message.path.name}".casefold()
                    if not needle or needle in haystack:
                        values.append(item)
                else:
                    for signal in message.signal_names:
                        haystack = (
                            f"{message.name} {signal} {message.path.name}".casefold()
                        )
                        if not needle or needle in haystack:
                            values.append(
                                {
                                    "database_file": message.path.name,
                                    "bus_type": message.bus_type,
                                    "frame_id": message.frame_id,
                                    "frame_id_hex": f"0x{message.frame_id:X}",
                                    "message": message.name,
                                    "signal": signal,
                                }
                            )
        else:
            raise DynamicInputError("kind 仅支持 database、node、message、signal")
        total = len(values)
        return {
            "kind": kind,
            "query": query,
            "bus": bus,
            "total_count": total,
            "returned_count": min(total, applied_limit),
            "limit_applied": applied_limit,
            "items": values[:applied_limit],
        }

    def _current_network_database(self) -> NetworkDatabase:
        paths = tuple(
            Path(value).resolve()
            for value in self._canoe.get_simulation_database_paths()
            if Path(value).is_file()
            and Path(value).suffix.casefold() in DATABASE_EXTENSIONS
        )
        if not paths:
            raise DynamicUnsupportedError(
                "当前 CANoe Configuration 未加载可解析的 DBC/ARXML/LDF 数据库"
            )
        if self._network_database is None or paths != self._network_database_paths:
            self._network_database = NetworkDatabase(paths)
            self._network_database_paths = paths
        return self._network_database

    def get_debug_snapshot(
        self,
        signals: list[dict[str, Any]],
        variables: list[dict[str, Any]],
        write_line_limit: int,
    ) -> dict[str, Any]:
        timestamp = _utc_timestamp()
        signal_results = []
        for request in signals[:10]:
            try:
                signal_results.append({"ok": True, **self.get_signal(**request)})
            except Exception as exc:  # noqa: BLE001 - 快照保留局部失败
                signal_results.append(
                    {"ok": False, "request": request, "error": _exception_details(exc)}
                )
        variable_results = []
        for request in variables[:10]:
            try:
                variable_results.append({"ok": True, **self.get_variable(**request)})
            except Exception as exc:  # noqa: BLE001 - 快照保留局部失败
                variable_results.append(
                    {"ok": False, "request": request, "error": _exception_details(exc)}
                )
        return {
            "captured_at": timestamp,
            "status": self.get_status(),
            "write_window": self.read_write_window(write_line_limit, 12_000),
            "signals": signal_results,
            "variables": variable_results,
        }

    def set_signal(
        self,
        bus: str,
        channel: int,
        message: str,
        signal: str,
        value: float,
        raw_value: bool,
    ) -> dict[str, Any]:
        before = self._canoe.get_signal_value(
            bus, channel, message, signal, raw_value=raw_value
        )
        success = self._canoe.set_signal_value(
            bus, channel, message, signal, value, raw_value=raw_value
        )
        if not success:
            raise DynamicBackendError(f"设置 Signal 失败：{message}::{signal}")
        after = self._canoe.get_signal_value(
            bus, channel, message, signal, raw_value=raw_value
        )
        return {
            "success": True,
            "bus": bus,
            "channel": channel,
            "message": message,
            "signal": signal,
            "raw_value_mode": raw_value,
            "old_value": _bounded_value(before),
            "new_value": _bounded_value(after),
            "timestamp": _utc_timestamp(),
        }

    def set_variable(self, kind: str, name: str, value: Any) -> dict[str, Any]:
        before = self.get_variable(kind, name)["value"]
        success = (
            self._canoe.set_system_variable_value(name, value)
            if kind == "system"
            else self._canoe.set_environment_variable_value(name, value)
            if kind == "environment"
            else False
        )
        if kind not in {"system", "environment"}:
            raise DynamicInputError("variable kind 仅支持 system 或 environment")
        if not success:
            raise DynamicBackendError(f"设置 {kind} Variable 失败：{name}")
        after = self.get_variable(kind, name)["value"]
        return {
            "success": True,
            "kind": kind,
            "name": name,
            "old_value": _bounded_value(before),
            "new_value": _bounded_value(after),
            "timestamp": _utc_timestamp(),
        }

    def _get_capl_function(self, name: str) -> Any:
        try:
            function = self._canoe.application.com_object.CAPL.GetFunction(name)
            if function is None:
                raise DynamicUnsupportedError(f"CAPL Function 不存在：{name}")
            return function
        except DynamicUnsupportedError:
            raise
        except Exception as exc:
            raise DynamicUnsupportedError(
                f"CAPL Function 不存在或尚未在 Measurement.OnInit 暴露：{name}"
            ) from exc

    def call_capl_function(self, name: str, arguments: list[Any]) -> dict[str, Any]:
        if not name.strip() or len(arguments) > MAX_CAPL_ARGUMENTS:
            raise DynamicInputError(
                f"CAPL Function 名称不能为空，参数最多 {MAX_CAPL_ARGUMENTS} 个"
            )
        function = self._get_capl_function(name)
        expected = int(function.ParameterCount)
        if expected != len(arguments):
            raise DynamicInputError(
                f"CAPL Function {name} 需要 {expected} 个参数，实际 {len(arguments)} 个"
            )
        try:
            return_value = function.Call(*arguments)
        except Exception as exc:
            raise DynamicBackendError(f"调用 CAPL Function {name} 失败：{exc}") from exc
        return {
            "success": True,
            "function": name,
            "argument_count": len(arguments),
            "return_value": _bounded_value(return_value),
            "timestamp": _utc_timestamp(),
        }

    def transmit_can_frame(
        self,
        channel: int,
        arbitration_id: int,
        data: bytes,
        is_extended: bool,
        is_fd: bool,
        brs: bool,
        count: int,
        interval_ms: int,
    ) -> dict[str, Any]:
        function = self._get_capl_function(self._raw_frame_capl_function)
        if int(function.ParameterCount) != 4:
            raise DynamicUnsupportedError(
                f"{self._raw_frame_capl_function} 必须接受 4 个参数："
                "channel、arbitration_id、flags、data_hex"
            )
        flags = int(is_extended) | (int(is_fd) << 1) | (int(brs) << 2)
        payload_hex = data.hex().upper()
        for index in range(count):
            try:
                accepted = function.Call(channel, arbitration_id, flags, payload_hex)
            except Exception as exc:
                raise DynamicBackendError(
                    f"Raw CAN Frame 第 {index + 1}/{count} 次发送失败：{exc}"
                ) from exc
            if not bool(accepted):
                raise DynamicUnsupportedError(
                    "Raw CAN CAPL Bridge 拒绝发送参数；请核对 Channel、ID、"
                    "Classic/FD、BRS 与 Payload 长度"
                )
            if index + 1 < count:
                self._wait(interval_ms / 1000)
        return {
            "success": True,
            "backend": f"capl:{self._raw_frame_capl_function}",
            "channel": channel,
            "arbitration_id": arbitration_id,
            "arbitration_id_hex": f"0x{arbitration_id:X}",
            "data_hex": data.hex(" ").upper(),
            "is_extended": is_extended,
            "is_fd": is_fd,
            "brs": brs,
            "count": count,
            "interval_ms": interval_ms,
            "timestamp": _utc_timestamp(),
        }

    def send_diagnostic_request(
        self, ecu_qualifier: str, request: str, timeout_seconds: float
    ) -> dict[str, Any]:
        response = self._canoe.send_diag_request(
            ecu_qualifier,
            request,
            request_in_bytes=True,
            return_sender_name=True,
            response_in_bytearray=False,
            timeout=timeout_seconds,
        )
        return {
            "ecu_qualifier": ecu_qualifier,
            "request": request,
            "response": str(response)[:8000],
            "timestamp": _utc_timestamp(),
        }

    def _replay_blocks(self) -> dict[str, Any]:
        collection = self._canoe.application.configuration.simulation_setup.com_object.ReplayCollection
        return {
            str(collection.Item(index).Name): collection.Item(index)
            for index in range(1, int(collection.Count) + 1)
        }

    def configure_replay(self, block_name: str, source_path: str) -> dict[str, Any]:
        blocks = self._replay_blocks()
        if block_name in blocks:
            block = blocks[block_name]
        else:
            if len(self._replay_states) >= MAX_MODEL_RESULTS:
                raise DynamicInputError(
                    f"单个 Session 最多配置 {MAX_MODEL_RESULTS} 个 Replay Block"
                )
            collection = self._canoe.application.configuration.simulation_setup.com_object.ReplayCollection
            block = collection.Add(block_name)
        block.Path = source_path
        self._replay_states[block_name] = "configured"
        return {
            "block_name": block_name,
            "source_path": source_path,
            "state": "configured",
        }

    def control_replay(self, block_name: str, action: str) -> dict[str, Any]:
        block = self._replay_blocks().get(block_name)
        if block is None:
            raise DynamicInputError(f"Replay Block 不存在：{block_name}")
        if action == "start":
            block.Start()
            state = "running"
        elif action == "stop":
            block.Stop()
            state = "stopped"
        elif action == "reset":
            if hasattr(block, "Reset"):
                block.Reset()
            else:
                block.Stop()
                block.Path = block.Path
            state = "configured"
        else:
            raise DynamicInputError("replay action 仅支持 start、stop、reset")
        self._replay_states[block_name] = state
        return {"block_name": block_name, "action": action, "state": state}

    def manage_logging(
        self, action: str, output_path: str | None, block_name: str | None
    ) -> dict[str, Any]:
        blocks = self._canoe.get_logging_blocks()
        if action == "list":
            items = [
                {
                    "index": index,
                    "full_name": str(block.full_name),
                    "active": bool(block.trigger.active),
                }
                for index, block in enumerate(blocks, 1)
            ]
            return {"count": len(items), "items": items[:MAX_MODEL_RESULTS]}
        if action == "configure":
            if not output_path:
                raise DynamicInputError("logging configure 必须提供 output_path")
            block = self._canoe.add_logging_block(output_path)
            return {
                "action": action,
                "full_name": str(block.full_name),
                "active": bool(block.trigger.active),
            }
        target = next(
            (
                block
                for block in blocks
                if str(block.full_name).casefold()
                == str(block_name or output_path or "").casefold()
                or Path(str(block.full_name)).name.casefold()
                == str(block_name or "").casefold()
            ),
            None,
        )
        if target is None:
            raise DynamicInputError("未找到指定 Logging Block")
        if action == "start":
            target.trigger.start()
            self._active_logging_blocks.add(str(target.full_name))
        elif action == "stop":
            target.trigger.stop()
            self._active_logging_blocks.discard(str(target.full_name))
        else:
            raise DynamicInputError(
                "logging action 仅支持 list、configure、start、stop"
            )
        return {
            "action": action,
            "full_name": str(target.full_name),
            "active": bool(target.trigger.active),
        }

    def list_tests(self, limit: int) -> dict[str, Any]:
        environments = self._canoe.get_test_environments()
        items = []
        total_modules = 0
        applied_limit = min(limit, MAX_MODEL_RESULTS)
        remaining = applied_limit
        for env_name, environment in environments.items():
            modules = []
            for module_name, module in environment.get_all_test_modules().items():
                total_modules += 1
                if remaining > 0:
                    modules.append(
                        {
                            "name": module_name,
                            "full_name": str(getattr(module, "full_name", "")),
                            "enabled": bool(getattr(module, "enabled", False)),
                            "verdict": int(getattr(module, "verdict", 0)),
                        }
                    )
                    remaining -= 1
            items.append(
                {
                    "name": env_name,
                    "full_name": str(getattr(environment, "full_name", "")),
                    "modules": modules,
                }
            )
        return {
            "environment_count": len(items),
            "module_count": total_modules,
            "returned_module_count": min(total_modules, applied_limit),
            "limit_applied": applied_limit,
            "environments": items[:MAX_MODEL_RESULTS],
        }

    def _find_test_module(self, name: str) -> Any:
        for environment in self._canoe.get_test_environments().values():
            modules = environment.get_all_test_modules()
            if name in modules:
                return modules[name]
        raise DynamicInputError(f"Test Module 不存在：{name}")

    @staticmethod
    def _test_name(module: Any) -> str:
        return str(getattr(module, "name", ""))

    def start_test(self, scope: str, name: str | None) -> dict[str, Any]:
        if not self._canoe.get_measurement_running_status():
            raise DynamicStateError("启动 Test 前 Measurement 必须已经运行")
        environments = self._canoe.get_test_environments()
        started: list[Any] = []
        if scope == "module":
            if not name:
                raise DynamicInputError("scope=module 必须提供 name")
            module = self._find_test_module(name)
            if not module.start():
                raise DynamicBackendError(f"Test Module 未触发 OnStart：{name}")
            started.append(module)
        elif scope == "environment":
            if not name or name not in environments:
                raise DynamicInputError(f"Test Environment 不存在：{name}")
            environments[name].execute_all()
            started.extend(environments[name].get_all_test_modules().values())
        elif scope == "all":
            for environment in environments.values():
                environment.execute_all()
                started.extend(environment.get_all_test_modules().values())
        else:
            raise DynamicInputError("test scope 仅支持 module、environment、all")
        self._active_test_modules = list(dict.fromkeys(started))
        started_names = [self._test_name(item) for item in started]
        return {
            "scope": scope,
            "name": name,
            "started_module_count": len(started_names),
            "started_modules": started_names[:MAX_MODEL_RESULTS],
            "truncated": len(started_names) > MAX_MODEL_RESULTS,
            "timestamp": _utc_timestamp(),
        }

    def get_test_result(self, name: str | None, limit: int) -> dict[str, Any]:
        modules = (
            [self._find_test_module(name)] if name else list(self._active_test_modules)
        )
        results = []
        for module in modules[: min(limit, MAX_MODEL_RESULTS)]:
            events = module.test_module_events
            stop_reason = int(events.TM_STOP_REASON)
            results.append(
                {
                    "name": self._test_name(module),
                    "running": bool(events.TM_STARTED and not events.TM_STOPPED),
                    "stopped": bool(events.TM_STOPPED),
                    "verdict_fail_event": bool(events.TC_FAIL),
                    "verdict": int(module.verdict),
                    "verdict_name": module.VALUE_TABLE_VERDICT.get(
                        int(module.verdict), "Unknown"
                    ),
                    "stop_reason": stop_reason,
                    "stop_reason_name": events.VALUE_TABLE_STOP_REASON.get(
                        stop_reason, "Unknown"
                    ),
                }
            )
        return {
            "total_count": len(modules),
            "returned_count": len(results),
            "limit_applied": min(limit, MAX_MODEL_RESULTS),
            "results": results,
        }

    def close(self) -> dict[str, Any]:
        errors = []
        event_sinks: list[Any] = []
        if self._canoe is None or self._canoe.application is None:
            errors.extend(self._release_process_resources())
            return {"closed": not errors, "cleanup_errors": errors}
        for module in self._active_test_modules:
            try:
                events = module.test_module_events
                if events.TM_STARTED and not events.TM_STOPPED:
                    module.stop(timeout=10)
            except Exception as exc:  # noqa: BLE001 - 继续清理其余资源
                errors.append(
                    {"operation": "stop_test", "error": _exception_details(exc)}
                )
        for name, state in list(self._replay_states.items()):
            if state == "running":
                try:
                    self.control_replay(name, "stop")
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {"operation": "stop_replay", "error": _exception_details(exc)}
                    )
        if self._active_logging_blocks:
            try:
                for block in self._canoe.get_logging_blocks():
                    if str(block.full_name) in self._active_logging_blocks:
                        block.trigger.stop()
                self._active_logging_blocks.clear()
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {"operation": "stop_logging", "error": _exception_details(exc)}
                )
        try:
            if (
                self._measurement_started_by_session
                and self._canoe.get_measurement_running_status()
            ):
                self._canoe.stop_measurement(timeout=30)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {"operation": "stop_measurement", "error": _exception_details(exc)}
            )
        try:
            application = self._canoe.application
            event_sinks = _event_sink_disconnect_targets(application, include_gc=True)
            if self._can_quit:
                quit_ok = bool(self._canoe.quit(timeout=30))
                if not quit_ok:
                    errors.append(
                        {
                            "operation": "quit",
                            "error": {"message": "CANoe quit returned false"},
                        }
                    )
                elif not _wait_for_canoe_process_exit(self._owned_process_ids, 15):
                    errors.append(
                        {
                            "operation": "wait_for_process_exit",
                            "error": {
                                "message": "CANoe Quit 已返回，但自有进程未在 15 秒内退出"
                            },
                        }
                    )
            else:
                application._release_event_sinks()
                self._canoe._reset_application()
        except Exception as exc:  # noqa: BLE001
            operation = "quit" if self._can_quit else "release_attachment"
            errors.append({"operation": operation, "error": _exception_details(exc)})
        finally:
            _disarm_event_sink_targets(event_sinks)
            event_sinks.clear()
            self._canoe = None
            self._owned_process_ids.clear()
            errors.extend(self._release_process_resources())
        return {"closed": not errors, "cleanup_errors": errors}

    @staticmethod
    def _release_process_resources() -> list[dict[str, Any]]:
        errors = []
        try:
            import logging

            from py_canoe.helpers.common import logger

            for handler in logger.handlers[:]:
                if isinstance(handler, logging.FileHandler):
                    logger.removeHandler(handler)
                    handler.close()
        except Exception as exc:  # noqa: BLE001 - 日志清理不得阻止 Session 关闭
            errors.append(
                {"operation": "close_log_handler", "error": _exception_details(exc)}
            )
        # BackendWorker 在 close 返回后立即结束专用 STA 线程。这里不显式调用
        # CoUninitialize：CANoe 退出后 pywin32 仍可能持有失效的事件 Gateway，
        # CoUninitialize 会在原生层释放它们并向 stderr 打印 Windows fatal exception。
        # 让专用线程退出时由 Windows 回收该 apartment，避免在长驻主进程中触发
        # 不可捕获的原生异常输出。
        return errors


def _canoe_processes() -> dict[int, str]:
    """用系统进程表确认 CANoe 数量；查询失败时拒绝猜测所有权。"""
    if platform.system() != "Windows":
        raise DynamicUnsupportedError("CANoe ownership 仅支持 Windows")
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes: dict[int, str] = {}
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) < 2:
                continue
            name = row[0].strip()
            if name.casefold().startswith("canoe") and name.casefold().endswith(".exe"):
                processes[int(row[1].replace(",", ""))] = name
        return processes
    except Exception as exc:
        raise DynamicStateError(
            f"无法可靠枚举 CANoe 进程，拒绝判断实例所有权：{exc}"
        ) from exc


def _has_active_canoe_application() -> bool:
    """只查询 COM Running Object Table，不触发 CANoe 启动。"""
    try:
        import pythoncom

        pythoncom.GetActiveObject("CANoe.Application")
        return True
    except pythoncom.com_error:
        return False
    except Exception as exc:
        raise DynamicStateError(
            f"无法可靠查询 CANoe COM 活动实例，拒绝判断所有权：{exc}"
        ) from exc


def _wait_for_canoe_process_exit(process_ids: set[int], timeout: float) -> bool:
    """等待本 Session 启动的 CANoe PID 真正退出，避免下一 Session 竞态。"""
    if not process_ids:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_ids.isdisjoint(_canoe_processes()):
            return True
        time.sleep(0.1)
    return process_ids.isdisjoint(_canoe_processes())


def _is_canoe_event_sink(target: Any) -> bool:
    """只识别 CANoe/py-canoe 生成的 pywin32 事件 Gateway。"""
    try:
        identity = " ".join(
            f"{base.__module__}.{base.__name__}" for base in type(target).__mro__
        )
    except (AttributeError, TypeError):
        return False
    normalized = identity.upper().replace("-", "").replace("_", "")
    return (
        "PYCANOE." in identity.upper().replace("_", "")
        or "7F31DEB05BCC11D3856200105A3E017B" in normalized
    )


def _event_sink_disconnect_targets(
    root: Any, *, include_gc: bool = False
) -> list[Any]:
    """收集 pywin32 Connection Point，且不在 CANoe Quit 前断开事件。"""
    visited: set[int] = set()
    targets: list[Any] = []

    def walk(value: Any) -> None:
        if value is None or id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
            return
        try:
            attributes = vars(value)
        except TypeError:
            return
        target = attributes.get("_obj_", value)
        try:
            target_attributes = vars(target)
        except TypeError:
            target_attributes = {}
        if "_olecp" in target_attributes:
            targets.append(target)
        # 普通 COM proxy 不属于 Python 所有的对象图，不能继续递归；但事件
        # Gateway 同时拥有 _oleobj_ 与 _olecp，所以上面的识别必须先执行。
        if hasattr(value, "_oleobj_"):
            return
        for name, item in tuple(attributes.items()):
            if name not in {"com_object", "_olecp", "_olecp_cookie"}:
                walk(item)

    walk(root)
    if include_gc:
        # py-canoe 某些集合会创建未保存在 Application 对象图中的临时
        # WithEvents Gateway。它们仍由 pywin32 循环引用持有，需要在 Quit
        # 前保留引用、Quit 后失活，避免稍后在非 COM 线程中执行 __del__。
        for value in gc.get_objects():
            try:
                attributes = vars(value)
            except TypeError:
                continue
            target = attributes.get("_obj_", value)
            try:
                target_attributes = vars(target)
            except TypeError:
                continue
            if (
                "_olecp" in target_attributes
                and _is_canoe_event_sink(target)
                and all(existing is not target for existing in targets)
            ):
                targets.append(target)
    return targets


def _disarm_event_sink_targets(targets: list[Any]) -> int:
    """使已完成 CANoe Quit 的 Connection Point 析构不再调用 Unadvise。"""
    disarmed = 0
    for target in targets:
        try:
            attributes = vars(target)
        except TypeError:
            continue
        if "_olecp" not in attributes:
            continue
        attributes["_olecp"] = None
        attributes["_olecp_cookie"] = None
        if "APP_COM_OBJ" in attributes:
            attributes["APP_COM_OBJ"] = None
        disarmed += 1
    return disarmed


def _disarm_event_sink_disconnects(root: Any) -> int:
    """兼容测试与非退出场景：立即失活 root 下的 pywin32 事件连接。"""
    return _disarm_event_sink_targets(_event_sink_disconnect_targets(root))


__all__ = ["PyCanoeBackend", "_exception_details"]
