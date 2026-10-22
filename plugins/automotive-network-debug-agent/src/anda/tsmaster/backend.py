"""仅通过外部 COM 操作拥有的独立 TSMaster 工程。"""

from __future__ import annotations

import ctypes
import platform
import struct
import time
from collections import deque
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from can.util import len2dlc

from anda.tsmaster.models import Bench, Frame

PROG_ID = "TSMaster.TSApplication"
MAPPING_FIELDS = (
    "FAppName",
    "FAppChannelType",
    "FAppChannelIndex",
    "FHWDeviceType",
    "FHWDeviceSubType",
    "FHWIndex",
    "FHWChannelIndex",
    "FHWDeviceName",
    "FMappingDisabled",
)


def processes() -> set[int]:
    if platform.system() != "Windows":
        raise RuntimeError("TSMaster COM 执行仅支持 Windows")
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("exe", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Entry),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = set()
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(Entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            if entry.exe.casefold() == "tsmaster.exe":
                # Windows 可能保留已退出进程的快照项；只忽略已明确退出的 PID。
                kernel.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.GetExitCodeProcess.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.DWORD),
                ]
                handle = kernel.OpenProcess(0x1000, False, entry.pid)
                code = wintypes.DWORD(259)
                try:
                    if (
                        not handle
                        or not kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                        or code.value == 259
                    ):
                        result.add(entry.pid)
                finally:
                    if handle:
                        kernel.CloseHandle(handle)
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:
                    raise ctypes.WinError(ctypes.get_last_error())
                break
    finally:
        kernel.CloseHandle(snapshot)
    return result


def health() -> dict:
    result = {
        "platform": platform.system(),
        "python_bits": struct.calcsize("P") * 8,
        "pywin32_available": find_spec("pythoncom") is not None,
        "prog_id": PROG_ID,
        "activates_application": False,
        "com_registered": False,
        "active_process_ids": [],
        "hardware_verified": False,
        "full_diagnostic_transaction": False,
    }
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, PROG_ID + "\\CLSID") as key:
                result["clsid"] = winreg.QueryValue(key, None)
            result["com_registered"] = True
            result["active_process_ids"] = sorted(processes())
        except Exception as exc:  # noqa: BLE001 - 健康检查需要返回有限错误
            result["error"] = str(exc)
    return result


class ComBackend:
    """所有方法都在创建对象的同一 STA 线程执行，不附着已有实例。"""

    def __init__(self):
        self.app: Any = None
        self.com: Any = None
        self.form: Any = None
        self.log: Any = None
        self.db: Any = None
        self.owned_pids: set[int] = set()
        self.connected = False
        self.logging = False
        self.replay_index: int | None = None
        self._initialized = False
        self.feedback = deque(maxlen=200)
        self.feedback_count = 0
        self._events = None
        self._expected_mappings = {}
        self._database_paths = []

    def open(self):
        import pythoncom
        import win32com.client

        if processes():
            raise RuntimeError("已有 TSMaster 进程；拒绝接管或修改用户工程")
        pythoncom.CoInitialize()
        self._initialized = True
        try:
            try:
                pythoncom.GetActiveObject(PROG_ID)
            except pythoncom.com_error as exc:
                if exc.hresult != -2147221021:  # MK_E_UNAVAILABLE
                    raise RuntimeError("无法可靠确认 COM 活动对象为空") from exc
            else:
                raise RuntimeError("已有 TSMaster COM 活动对象；拒绝接管")
            self.app = win32com.client.DispatchEx(PROG_ID)
            deadline = time.monotonic() + 10
            while not (found := processes()) and time.monotonic() < deadline:
                time.sleep(0.1)
            if len(found) != 1:
                raise RuntimeError("创建后无法确认唯一 TSMaster 实例；不修改工程")
            self.owned_pids = found
            self.com, self.form = self.app.TSCOM(), self.app.TSFormManager()
            self.log, self.db = self.app.TSLog(), self.app.TSDB()
            self.form.show_main_form()
            self.app.disconnect()
        except BaseException:
            self.close()
            raise
        return {
            "owned_process_ids": sorted(self.owned_pids),
            "python_bits": struct.calcsize("P") * 8,
        }

    def assert_owner(self):
        if not self.app or not self.owned_pids or processes() != self.owned_pids:
            raise RuntimeError("TSMaster 实例归属变化；拒绝操作")

    def prepare_project(self, project_path: str, template: str | None):
        self.assert_owner()
        if template:
            self.form.load_project(template, True)
        else:
            self.form.create_project(True)
        self.quiesce()
        self.form.save_project(project_path)
        if not Path(project_path).exists():
            raise RuntimeError("save_project 未产生指定产物；不能认定工程保存成功")
        self.form.load_project(project_path, True)
        self.quiesce()
        return {
            "project_path": project_path,
            "save_artifact_exists": True,
            "reload_call_completed": True,
        }

    def quiesce(self):
        self.app.disconnect()
        self.connected = False
        self.log.del_online_replay_configs()
        self.com.delete_cyclic_msgs()
        self.com.can_rbs_stop()
        self.com.can_rbs_configure(False, False, False, 0)
        self.com.can_rbs_activate_all_networks(False, True)
        # 新建工程没有 SimpleGateway；加载工程另需明确人工审核。

    def configure(self, bench: Bench):
        import win32com.client

        self.assert_owner()
        self.quiesce()
        self.app.set_current_application("TSMaster")
        if self.app.get_current_application() != "TSMaster":
            raise RuntimeError("当前 TSMaster 应用名读回不符")
        self.app.set_can_channel_count(max(c.logical_channel for c in bench.channels))
        self.app.set_lin_channel_count(0)
        for index in range(self.app.get_can_channel_count()):
            self.app.del_mapping(0, index)
        mappings = []
        for channel in bench.channels:
            record = win32com.client.Record("TTSMapping", self.app)
            expected = dict(
                zip(
                    MAPPING_FIELDS,
                    (
                        "TSMaster",
                        0,
                        channel.logical_channel - 1,
                        channel.device_type,
                        channel.device_subtype,
                        channel.device_index,
                        channel.device_channel,
                        channel.device_name,
                        False,
                    ),
                    strict=True,
                )
            )
            for name, value in expected.items():
                setattr(record, name, value)
            self.app.set_mapping(record)
            actual = self.app.get_mapping(0, channel.logical_channel - 1)
            values = {name: getattr(actual, name) for name in MAPPING_FIELDS}
            if values != expected:
                raise RuntimeError(
                    f"通道映射读回不符: {channel.logical_channel}: {values}"
                )
            self._expected_mappings[channel.logical_channel - 1] = expected
            self.app.configure_baudrate_canfd(
                channel.logical_channel - 1,
                channel.arbitration_kbps,
                channel.data_kbps,
                channel.controller_type,
                0,
                channel.termination,
            )
            mappings.append(
                {
                    "network": channel.network,
                    "logical_channel": channel.logical_channel,
                    "mapping": values,
                    "baudrate_readback_verified": False,
                }
            )
        loaded = []
        by_path = {}
        for binding in bench.databases:
            by_path.setdefault(str(Path(binding.path).resolve()), []).append(binding)
        for path, bindings in by_path.items():
            supported = ",".join(str(b.logical_channel - 1) for b in bindings)
            db_id = self.db.load_can_db(path, supported)
            ids = [self.db.get_can_db_id(i) for i in range(self.db.get_can_db_count())]
            if db_id not in ids:
                raise RuntimeError("DBC 导入 ID 未出现在数据库枚举结果")
            loaded.extend(
                {**binding.model_dump(), "database_id": db_id} for binding in bindings
            )
        self._database_paths = list(by_path)
        return {"mappings": mappings, "databases": loaded}

    def save(self, path: str):
        self.assert_owner()
        self.form.save_project(path)
        if not Path(path).exists():
            raise RuntimeError("工程保存产物不存在")

    def reload(self, path: str):
        self.assert_owner()
        self.form.load_project(path, True)
        self.quiesce()
        for index, expected in self._expected_mappings.items():
            actual = self.app.get_mapping(0, index)
            if {name: getattr(actual, name) for name in MAPPING_FIELDS} != expected:
                raise RuntimeError(f"工程回载后的通道映射不符: {index + 1}")
        paths = []
        for index in range(self.db.get_can_db_count()):
            db_id = self.db.get_can_db_id(index)
            ok, path_value = self.db.get_can_db_info(db_id, 1, -1, -1)
            if not ok:
                raise RuntimeError("工程回载后的 DBC 路径读取失败")
            paths.append(str(Path(path_value).resolve()).casefold())
        if sorted(paths) != sorted(p.casefold() for p in self._database_paths):
            raise RuntimeError("工程回载后的数据库集合不符")
        return {
            "mappings_verified": True,
            "database_files_verified": True,
            "database_channel_membership_readback": "manual_verification_required",
        }

    def connect(self):
        self.assert_owner()
        self.app.connect()
        ok, value = self.app.get_system_var_generic("Application.Connected")
        if not ok or str(value).strip().casefold() not in {"1", "true"}:
            raise RuntimeError(f"连接状态未确认: {ok}, {value}")
        self.connected = True

    def enable_feedback(self):
        import win32com.client

        self.feedback.clear()
        self.feedback_count = 0
        backend = self

        class Events:
            def OnRxCANFD(
                self,
                channel,
                is_tx,
                extended,
                error,
                is_fd,
                brs,
                esi,
                dlc,
                identifier,
                timestamp_us,
                data,
            ):
                from can.util import dlc2len

                backend.feedback_count += 1
                backend.feedback.append(
                    {
                        "channel": channel + 1,
                        "direction": "Tx" if is_tx else "Rx",
                        "arbitration_id": identifier,
                        "timestamp_us": timestamp_us,
                        "is_extended_id": extended,
                        "is_fd": is_fd,
                        "bitrate_switch": brs,
                        "is_error_frame": error,
                        "error_state_indicator": esi,
                        "data_hex": bytes(v & 255 for v in data[: dlc2len(dlc)]).hex(),
                    }
                )

        self._events = win32com.client.WithEvents(self.com, Events)
        self.com.enable_event_can(False)
        self.com.enable_event_canfd(True)

    def start_logging(self, path: str):
        self.logging = True  # 即使 COM 调用中途异常，也必须尝试 stop。
        self.com.start_logging(path)

    def send(self, frame: Frame):
        import pythoncom
        import win32com.client

        data = bytes.fromhex(frame.data_hex)
        record = win32com.client.Record("TCANFD" if frame.is_fd else "TCAN", self.app)
        record.FIdxChn = frame.channel - 1
        record.FIdentifier = frame.arbitration_id
        record.FIsExtendedId = frame.is_extended_id
        record.FDLC = len2dlc(len(data))
        if frame.is_fd:
            record.FIsEDL, record.FIsBRS, record.FIsESI = (
                True,
                frame.bitrate_switch,
                False,
            )
        else:
            record.FIsRemote = False
        padded = data.ljust(64 if frame.is_fd else 8, b"\0")
        record.FDatas = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_I1,
            [v if v < 128 else v - 256 for v in padded],
        )
        if frame.is_fd:
            self.com.transmit_canfd_async(record)
        else:
            self.com.transmit_can_async(record)

    def start_replay(self, path: str, mappings: str):
        self.replay_index = self.log.add_online_replay_config(path)
        self.log.set_online_replay_config(
            self.replay_index,
            "ANDA_source_only",
            path,
            False,
            False,
            0,
            0,
            True,
            True,
            mappings,
        )
        actual = self.log.get_online_replay_config(self.replay_index)
        expected = ("ANDA_source_only", path, False, False, 0, 0, True, True, mappings)
        if tuple(actual) != expected:
            raise RuntimeError("回放配置读回不符；拒绝启动")
        state, _ = self.log.get_online_replay_status(self.replay_index)
        if state != 0:
            raise RuntimeError("新建回放不是未开始状态")
        self.log.start_online_replay(self.replay_index)

    def replay_status(self):
        return self.log.get_online_replay_status(self.replay_index)

    def wait(self, seconds: float):
        import pythoncom

        # 消息泵只用于 COM 响应；权威证据来自关闭的新 BLF。
        pythoncom.PumpWaitingMessages()
        time.sleep(seconds)

    def cleanup(self) -> list[dict]:
        errors = []
        if not self.app:
            return errors
        try:
            self.assert_owner()
        except Exception as exc:  # noqa: BLE001 - 所有权不明时保留清理失败
            return [{"operation": "ownership", "error": str(exc)}]
        actions = []
        if self.replay_index is not None:
            actions.append(
                ("stop_replay", lambda: self.log.stop_online_replay(self.replay_index))
            )
        if self.logging:
            actions.append(("stop_logging", self.com.stop_logging))
        if self._events is not None:
            actions.append(
                ("disable_events", lambda: self.com.enable_event_canfd(False))
            )
        actions.append(("disconnect", self.app.disconnect))
        for name, action in actions:
            try:
                action()
                if name == "stop_logging":
                    self.logging = False
                elif name == "stop_replay":
                    state, _ = self.log.get_online_replay_status(self.replay_index)
                    if state in (1, 2):
                        raise RuntimeError("回放仍活动，不能确认已停止")
                    self.log.del_online_replay_config(self.replay_index)
                    self.replay_index = None
                elif name == "disconnect":
                    ok, value = self.app.get_system_var_generic("Application.Connected")
                    if not ok or str(value).strip().casefold() not in {"0", "false"}:
                        raise RuntimeError("未确认连接已断开")
                    self.connected = False
                elif name == "disable_events":
                    self._events.close()
                    self._events = None
            except Exception as exc:  # noqa: BLE001 - 每个清理动作独立执行
                errors.append({"operation": name, "error": str(exc)[:1000]})
        return errors

    def close(self):
        errors = self.cleanup()
        self.db = self.log = self.form = self.com = self.app = None
        # COM 未提供退出方法；只向唯一自有进程的主窗口请求正常关闭。
        close_requested = False
        if self.owned_pids and processes() == self.owned_pids:
            import win32con
            import win32gui
            import win32process

            def close_window(handle, _):
                nonlocal close_requested
                _, pid = win32process.GetWindowThreadProcessId(handle)
                if (
                    pid in self.owned_pids
                    and win32gui.IsWindowVisible(handle)
                    and not win32gui.GetWindow(handle, win32con.GW_OWNER)
                ):
                    win32gui.PostMessage(handle, win32con.WM_CLOSE, 0, 0)
                    close_requested = True

            win32gui.EnumWindows(close_window, None)
            deadline = time.monotonic() + 5
            while self.owned_pids & processes() and time.monotonic() < deadline:

                def dismiss_owned_save(handle, _):
                    _, pid = win32process.GetWindowThreadProcessId(handle)
                    if (
                        pid not in self.owned_pids
                        or win32gui.GetClassName(handle) != "#32770"
                    ):
                        return
                    # 只确认独立工作工程的标准保存提示；不操作其他对话框。
                    if win32gui.GetWindowText(handle) in {
                        "工程已更改",
                        "Project changed",
                        "Project Changed",
                    }:
                        button = win32gui.GetDlgItem(handle, win32con.IDNO)
                        if button:
                            win32gui.PostMessage(button, win32con.BM_CLICK, 0, 0)

                win32gui.EnumWindows(dismiss_owned_save, None)
                self.wait(0.05)
        if self._initialized:
            import pythoncom

            pythoncom.CoUninitialize()
            self._initialized = False
        remaining = self.owned_pids & processes() if self.owned_pids else set()
        if remaining:
            errors.append(
                {
                    "operation": "close_process",
                    "error": "正常关闭尚未完成；未强制终止进程",
                }
            )
        return {
            "cleanup_errors": errors,
            "process_exit_requested": close_requested,
            "owned_process_ids": sorted(self.owned_pids),
            "remaining_process_ids": sorted(remaining),
        }
