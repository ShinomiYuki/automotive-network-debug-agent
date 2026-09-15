"""可脱离 Codex 运行的 CANoe Dynamic 能力采集器。"""

from __future__ import annotations

import argparse
import json
import platform
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anda.dynamic.pycanoe import PyCanoeBackend, _exception_details
from anda.dynamic.worker import BackendWorker


def _record(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"status": "supported", "value": call()}
    except Exception as exc:  # noqa: BLE001 - 每个探测项必须独立落盘
        return {"status": "unsupported", "error": _exception_details(exc)}


def _write_synthetic_blf(path: Path) -> None:
    import can

    started = time.time()
    with can.BLFWriter(str(path)) as writer:
        for index in range(20):
            writer.on_message_received(
                can.Message(
                    timestamp=started + index * 0.1,
                    arbitration_id=0x123,
                    data=[index, 1, 2, 3, 4, 5, 6, 7],
                    channel=0,
                    is_rx=True,
                )
            )


def collect_capabilities(output_path: Path, temp_root: Path | None = None) -> dict:
    """运行有限离线探测并始终写出 JSON 报告。"""
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if temp_root is not None:
        temp_root = temp_root.expanduser().resolve()
        temp_root.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "safety": {
            "mode": "offline_only",
            "raw_frame_sent": False,
            "diagnostic_request_sent": False,
            "replay_started": False,
            "original_configuration_opened": False,
        },
        "static_environment": PyCanoeBackend.probe_environment(),
        "checks": {},
    }

    worker: BackendWorker | None = None
    with tempfile.TemporaryDirectory(
        prefix="anda-canoe-probe-",
        dir=str(temp_root) if temp_root else None,
        ignore_cleanup_errors=True,
    ) as directory:
        working = Path(directory)
        report["temporary_directory"] = str(working)
        blf = working / "probe.blf"
        report["checks"]["synthetic_blf"] = _record(
            lambda: (_write_synthetic_blf(blf), {"created": True})[1]
        )
        try:
            worker = BackendWorker(PyCanoeBackend)
            report["checks"]["create_temporary_configuration"] = _record(
                lambda: worker.call(
                    "initialize",
                    {
                        "mode": "offline",
                        "configuration_origin": "temporary_configuration",
                        "configuration_path": None,
                        "blf_path": str(blf),
                        "databases": [],
                        "temp_directory": str(working),
                        "visible": False,
                        "raw_frame_capl_function": "ANDA_TransmitCanFrame",
                    },
                    timeout=90,
                )
            )
            if report["checks"]["create_temporary_configuration"]["status"] == "supported":
                report["checks"]["status"] = _record(
                    lambda: worker.call("get_status")
                )
                report["checks"]["safe_capability_inventory"] = _record(
                    lambda: worker.call("inspect_safe_capabilities")
                )
                report["checks"]["compile_capl"] = _record(
                    lambda: worker.call("compile_capl", 40)
                )
                report["checks"]["write_window"] = _record(
                    lambda: worker.call("read_write_window", 80, 12_000)
                )
                report["checks"]["bus_statistics_can1"] = _record(
                    lambda: worker.call("get_bus_statistics", 1)
                )
                report["checks"]["offline_animation_start"] = _record(
                    lambda: worker.call("control_offline_animation", "start", 100)
                )
                if report["checks"]["offline_animation_start"]["status"] == "supported":
                    report["checks"]["measurement_started_event"] = _record(
                        lambda: worker.call(
                            "wait_runtime_event",
                            "measurement_started",
                            5,
                            timeout=10,
                        )
                    )
                    report["checks"]["offline_animation_pause"] = _record(
                        lambda: worker.call("control_offline_animation", "pause", 100)
                    )
                    report["checks"]["offline_animation_step"] = _record(
                        lambda: worker.call("control_offline_animation", "step", 100)
                    )
                    report["checks"]["offline_animation_resume"] = _record(
                        lambda: worker.call("control_offline_animation", "resume", 100)
                    )
                    report["checks"]["offline_animation_reset"] = _record(
                        lambda: worker.call("control_offline_animation", "reset", 100)
                    )
                    report["checks"]["offline_animation_stop"] = _record(
                        lambda: worker.call("control_offline_animation", "stop", 100)
                    )
        except Exception as exc:  # noqa: BLE001 - 顶层失败仍必须生成报告
            report["fatal_error"] = _exception_details(exc)
        finally:
            if worker is not None:
                report["checks"]["cleanup"] = _record(
                    lambda: worker.call("close", timeout=60)
                )
                report["worker_stopped"] = worker.shutdown(timeout=15)

    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _default_output() -> Path:
    import os

    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return (
        base
        / "AutomotiveNetworkDebugAgent"
        / "diagnostics"
        / "canoe-capabilities.json"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="离线采集 CANoe Dynamic 能力，不发送帧或诊断请求"
    )
    parser.add_argument("--output", type=Path, default=_default_output())
    parser.add_argument("--temp-root", type=Path)
    arguments = parser.parse_args()
    report = collect_capabilities(arguments.output, arguments.temp_root)
    print(str(arguments.output.expanduser().resolve()))
    return 0 if "fatal_error" not in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
