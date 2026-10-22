"""有限执行、异常清理及关闭日志后的自动检查。"""

from __future__ import annotations

import atexit
import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

from anda.tsmaster.analysis import analyze_routes
from anda.tsmaster.backend import ComBackend, health
from anda.tsmaster.models import Bench, Frame, Replay
from anda.tsmaster.replay import prepare_replay


class TSMasterSessions:
    """每个服务进程只允许一个拥有的工程，COM 始终留在专用线程。"""

    def __init__(self, backend_factory=ComBackend):
        self._factory = backend_factory
        self._sessions = {}
        self._lock = Lock()
        atexit.register(self.shutdown)

    @staticmethod
    def get_health():
        return health()

    def create(
        self,
        workspace: str,
        project_name: str,
        bench: dict,
        application_authorized: bool = False,
        template: str | None = None,
        template_transmitters_reviewed: bool = False,
    ) -> dict:
        if not application_authorized:
            raise ValueError(
                "需明确授权启动独立 TSMaster 测试应用；此授权不允许总线发送"
            )
        config = Bench.model_validate(bench)
        if (
            not project_name
            or Path(project_name).name != project_name
            or Path(project_name).suffix.lower() != ".t7z"
        ):
            raise ValueError("project_name 必须是单一 .T7z 文件名")
        if template and (
            not template_transmitters_reviewed or not Path(template).is_file()
        ):
            raise ValueError(
                "加载模板需要确认无自动 RBS、周期发送、回放及 SimpleGateway"
            )
        root = Path(workspace).resolve()
        if not root.is_dir():
            raise ValueError("workspace 必须是已存在的本地目录")
        with self._lock:
            if any(s["state"] != "closed" for s in self._sessions.values()):
                raise RuntimeError("已有会话；先关闭并确认自有实例状态")
            session_id = uuid4().hex
            folder = root / ("tsmaster-" + session_id)
            folder.mkdir()
            session = {
                "session_id": session_id,
                "workspace": str(folder),
                "project_path": str(folder / project_name),
                "state": "opening",
                "bench": config,
                "backend": None,
                "cancel": Event(),
                "executor": ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="anda-tsmaster-sta"
                ),
                "result": None,
                "error": None,
            }
            self._sessions[session_id] = session
            session["future"] = session["executor"].submit(
                self._open, session, template
            )
        try:
            session["future"].result(timeout=30)
        except TimeoutError:
            session["cancel"].set()
            session["state"] = "opening_timeout"
        except Exception as exc:  # noqa: BLE001 - Future 的错误以状态交付
            session["error"] = str(exc)[:2000]
        return self.status(session_id)

    def _open(self, session, template):
        backend = self._factory()
        session["backend"] = backend
        try:
            session["environment"] = backend.open()
            if session["cancel"].is_set():
                raise RuntimeError("会话创建已取消")
            session["project"] = backend.prepare_project(
                session["project_path"], template
            )
            session["configuration"] = backend.configure(session["bench"])
            backend.save(session["project_path"])
            session["project"]["configuration_reload"] = backend.reload(
                session["project_path"]
            )
            if session["cancel"].is_set():
                raise RuntimeError("会话创建已取消；清理创建中的工程")
            session["state"] = "ready"
        except Exception:
            session["close_result"] = backend.close()
            session["state"] = "failed"
            raise

    def _get(self, session_id):
        if session_id not in self._sessions:
            raise ValueError("未知的 TSMaster session_id")
        return self._sessions[session_id]

    def status(self, session_id: str) -> dict:
        session = self._get(session_id)
        keys = (
            "session_id",
            "workspace",
            "project_path",
            "state",
            "environment",
            "project",
            "configuration",
            "run_directory",
            "result",
            "error",
            "close_result",
        )
        result = {key: session[key] for key in keys if key in session}
        if result.get("result"):
            report = result["result"]
            result["result"] = {
                key: report.get(key)
                for key in (
                    "execution",
                    "report_path",
                    "measurement_blf",
                    "error",
                    "cleanup_errors",
                )
            }
            result["result"]["case_statuses"] = [
                {"name": item["name"], "status": item["status"]}
                for item in report.get("analysis", {}).get("results", [])
            ]
        result["operation_pending"] = not session["future"].done()
        result["replay_progress"] = session.get("replay_progress")
        backend = session["backend"]
        if backend is not None and hasattr(backend, "feedback"):
            result["feedback"] = {
                "frames": list(backend.feedback)[-20:],
                "total_seen": backend.feedback_count,
                "authoritative": False,
                "retained_limit": 200,
            }
        if session["future"].done() and session["future"].exception():
            result["error"] = str(session["future"].exception())[:2000]
        return result

    def result(self, session_id: str, offset: int = 0, limit: int = 10):
        if offset < 0 or not 1 <= limit <= 10:
            raise ValueError("offset 不得为负，limit 必须为 1..10")
        session = self._get(session_id)
        if session["result"] is None:
            return {"state": session["state"], "result_ready": False}
        report = session["result"]
        analysis = report.get("analysis", {})
        routes = analysis.get("results", [])
        return {
            "report_path": report["report_path"],
            "execution": report["execution"],
            "measurement_blf": report["measurement_blf"],
            "error": report["error"],
            "cleanup_errors": report["cleanup_errors"],
            "total_cases": len(routes),
            "frames_scanned": analysis.get("frames_scanned"),
            "analysis_status": analysis.get("status"),
            "analysis_reason": analysis.get("reason"),
            "results": routes[offset : offset + limit],
        }

    def run(
        self,
        session_id: str,
        cases: list[dict],
        frames: list[dict] | None,
        replay: dict | None,
        baseline_seconds: float,
        response_seconds: float,
        timeout_seconds: float,
        evidence_context: dict,
        bus_authorized: bool = False,
    ) -> dict:
        session = self._get(session_id)
        if not bus_authorized:
            raise ValueError("需要本次台架连接及批准刺激的明确授权")
        if (
            not 0 <= baseline_seconds <= 10
            or not 0 < response_seconds <= 30
            or not 0 < timeout_seconds <= 120
        ):
            raise ValueError("基线 0..10 秒，响应 0..30 秒，总时限 0..120 秒")
        if not 1 <= len(cases) <= 64 or len(json.dumps(cases)) > 128000:
            raise ValueError("每次允许 1..64 个明确用例且输入不得超过 128000 字符")
        if bool(frames) == bool(replay):
            raise ValueError("必须且只能选择有限帧发送或源 BLF 回放")
        parsed = [Frame.model_validate(f) for f in (frames or [])]
        if len(parsed) > 64 or sum(f.count for f in parsed) > 10000:
            raise ValueError("刺激最多 64 项及总计 10000 帧")
        for frame in parsed:
            frame.authorize(session["bench"])
        duration = sum((f.count - 1) * f.interval_ms / 1000 for f in parsed)
        if duration + baseline_seconds + response_seconds >= timeout_seconds:
            raise ValueError("发送及观察时间超过本次总时限")
        plan = Replay.model_validate(replay) if replay else None
        # 先验证所有用例，包括 DBC 歧义；空日志返回未执行，不产生硬件动作。
        from anda.tsmaster.analysis import validate_cases

        validate_cases(cases, self._bindings(session))
        for case in cases:
            for endpoint in (case["source"], case["target"]):
                session["bench"].channel(endpoint["channel"])
            if case["window_seconds"] > response_seconds:
                raise ValueError("尾部响应时间必须覆盖所有用例窗口")
            if case.get("baseline_seconds", 0) > baseline_seconds:
                raise ValueError("执行基线时间短于用例要求")
        with self._lock:
            if session["state"] != "ready" or not session["future"].done():
                raise RuntimeError("会话尚未就绪或仍有操作")
            run_folder = Path(session["workspace"]) / ("run-" + uuid4().hex)
            run_folder.mkdir()
            session["run_directory"] = str(run_folder)
            session["result"] = None
            session["replay_progress"] = None
            session["cancel"].clear()
            session["state"] = "running"
            session["future"] = session["executor"].submit(
                self._execute,
                session,
                cases,
                parsed,
                plan,
                baseline_seconds,
                response_seconds,
                timeout_seconds,
                evidence_context,
                run_folder,
            )
        return self.status(session_id)

    @staticmethod
    def _bindings(session):
        return [
            {"path": b.path, "network": b.network, "channel": b.logical_channel}
            for b in session["bench"].databases
        ]

    def _execute(
        self,
        session,
        cases,
        frames,
        replay,
        baseline,
        response,
        timeout,
        context,
        folder,
    ):
        backend = session["backend"]
        started = time.monotonic()
        deadline = started + timeout
        executed = False
        completed = False
        error = None
        replay_info = None
        output = folder / "measurement.blf"

        def check():
            if session["cancel"].is_set():
                raise InterruptedError("本次执行已取消")
            if time.monotonic() >= deadline:
                raise TimeoutError("本次执行超过总时限")

        def wait(seconds):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                check()
                backend.wait(min(0.02, max(0, until - time.monotonic())))

        try:
            if replay:
                replay_info = prepare_replay(
                    replay, session["bench"], folder / "source-stimulus.blf"
                )
                if replay_info["duration_seconds"] + baseline + response >= timeout:
                    raise ValueError("回放及响应时间超过总时限")
            check()
            backend.connect()
            backend.enable_feedback()
            backend.start_logging(str(output))
            wait(baseline)
            if replay:
                executed = True
                backend.start_replay(
                    replay_info["stimulus_blf"], replay_info["mappings"]
                )
                while True:
                    check()
                    state, progress = backend.replay_status()
                    session["replay_progress"] = {"state": state, "percent": progress}
                    if state == 3:
                        break
                    if state not in (0, 1):
                        raise RuntimeError(f"回放异常状态 {state}")
                    backend.wait(0.02)
            else:
                for frame in frames:
                    for index in range(frame.count):
                        check()
                        executed = True
                        backend.send(frame)
                        if index + 1 < frame.count:
                            wait(frame.interval_ms / 1000)
            wait(response)
            completed = True
        except Exception as exc:  # noqa: BLE001 - 执行错误保留报告并继续清理
            error = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        finally:
            try:
                cleanup_errors = backend.cleanup()
            except Exception as exc:  # noqa: BLE001 - 清理错误不得丢失执行记录
                cleanup_errors = [{"operation": "cleanup", "error": str(exc)[:2000]}]
        verified = dict(context)
        verified["executed"] = executed
        verified["capture_complete"] = bool(
            context.get("capture_complete") is True and completed and not cleanup_errors
        )
        result = {
            "execution": "completed"
            if completed and not cleanup_errors
            else "interrupted",
            "measurement_blf": str(output),
            "replay": replay_info,
            "cases": cases,
            "bench": session["bench"].model_dump(),
            "environment": session.get("environment"),
            "configuration": session.get("configuration"),
            "send_plan": [frame.model_dump() for frame in frames],
            "error": error,
            "cleanup_errors": cleanup_errors,
            "elapsed_seconds": time.monotonic() - started,
            "evidence_context": verified,
            "full_diagnostic_transaction": "not_executed",
        }
        try:
            if output.is_file() and not backend.logging:
                result["analysis"] = analyze_routes(
                    str(output), cases, verified, self._bindings(session)
                )
            else:
                result["analysis"] = {
                    "status": "insufficient_evidence" if executed else "not_executed",
                    "reason": "本次测量 BLF 缺失或尚未确认关闭",
                }
        except Exception as exc:  # noqa: BLE001 - 无法分析时只报告证据不足
            result["analysis"] = {
                "status": "insufficient_evidence",
                "reason": str(exc)[:2000],
            }
        result["report_path"] = str(folder / "result.json")
        try:
            (folder / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            result["report_write_error"] = str(exc)[:1000]
        session["result"] = result
        if session["state"] != "closing":
            session["state"] = "failed" if cleanup_errors else "ready"
        return result

    def stop(self, session_id: str):
        session = self._get(session_id)
        session["cancel"].set()
        return {
            "session_id": session_id,
            "stop_requested": True,
            "note": "停止标志已提交；须继续查询状态确认回放、记录及连接已清理",
        }

    def close(self, session_id: str):
        session = self._get(session_id)
        session["cancel"].set()
        with self._lock:
            if session["state"] in {"closed", "closing"}:
                return self.status(session_id)
            session["state"] = "closing"

            def finish():
                try:
                    session["close_result"] = (
                        session["backend"].close() if session["backend"] else {}
                    )
                    session["state"] = (
                        "close_failed"
                        if session["close_result"].get("cleanup_errors")
                        else "closed"
                    )
                except Exception as exc:  # noqa: BLE001 - 关闭失败保留可信状态
                    session["error"] = str(exc)[:2000]
                    session["state"] = "close_failed"

            session["future"] = session["executor"].submit(finish)
        try:
            session["future"].result(timeout=5)
        except TimeoutError:
            return self.status(session_id)
        if session["state"] == "closed":
            session["executor"].shutdown(wait=False)
        return self.status(session_id)

    def shutdown(self):
        for session in list(self._sessions.values()):
            if session["state"] != "closed":
                try:
                    self.close(session["session_id"])
                except RuntimeError:
                    # 解释器退出阶段执行器可能已停止；不能声称 COM 已关闭。
                    session["state"] = "close_failed"
