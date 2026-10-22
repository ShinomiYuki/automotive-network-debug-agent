"""检查关闭后的完整测量 BLF，并返回有限的原始帧证据。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any

from anda.trace.blf import iter_blf
from anda.trace.dbc import DatabaseMessage, NetworkDatabase
from anda.trace.models import RawFrame

_EVIDENCE_LIMIT = 10
_REQUIRED_CONTEXT = (
    "capture_complete",
    "direction_verified",
    "isolated",
    "clock_verified",
    "data_frames_only_verified",
)


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} 必须为有限数值")
    result = float(value)
    if not isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{label} 必须为{'正' if positive else '非负'}有限数值")
    return result


def _endpoint(value: dict, *, source: bool) -> dict:
    result = dict(value)
    for key in ("channel", "arbitration_id", "is_extended_id", "is_fd"):
        if key not in result:
            raise ValueError(f"路由端点缺少 {key}")
    if type(result["channel"]) is not int or result["channel"] < 1:
        raise ValueError("BLF channel 必须为一基正整数")
    maximum = 0x1FFFFFFF if result["is_extended_id"] else 0x7FF
    if type(result["arbitration_id"]) is not int or not (
        0 <= result["arbitration_id"] <= maximum
    ):
        raise ValueError("arbitration_id 不符合帧类型")
    for key in ("is_extended_id", "is_fd"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    if "bitrate_switch" in result and type(result["bitrate_switch"]) is not bool:
        raise ValueError("bitrate_switch 必须为布尔值")
    direction = result.setdefault("direction", "Tx" if source else "Rx")
    if direction not in (("Tx", "Rx") if source else ("Rx",)):
        raise ValueError("源方向只允许 Tx/Rx，目标方向只允许 Rx")
    if "data_hex" in result:
        result["data"] = bytes.fromhex(result["data_hex"])
    return result


def _matches(frame: RawFrame, endpoint: dict, *, payload: bool = True) -> bool:
    return (
        frame.bus_type == "can"
        and not frame.is_error_frame
        and frame.channel == endpoint["channel"]
        and frame.arbitration_id == endpoint["arbitration_id"]
        and frame.is_extended_id == endpoint["is_extended_id"]
        and frame.is_fd == endpoint["is_fd"]
        and frame.is_rx is (endpoint["direction"] == "Rx")
        and (
            "bitrate_switch" not in endpoint
            or frame.bitrate_switch == endpoint["bitrate_switch"]
        )
        and (not payload or "data" not in endpoint or frame.data == endpoint["data"])
    )


def _evidence(index: int, frame: RawFrame, value: float | None = None) -> dict:
    result = {
        "frame_index": index,
        "timestamp": frame.timestamp,
        "channel": frame.channel,
        "arbitration_id": frame.arbitration_id,
        "direction": "Rx" if frame.is_rx else "Tx",
        "is_extended_id": frame.is_extended_id,
        "is_fd": frame.is_fd,
        "bitrate_switch": frame.bitrate_switch,
        "length": len(frame.data),
        "data_hex": frame.data.hex(" "),
    }
    if value is not None:
        result["signal_value"] = value
    return result


@dataclass
class _Route:
    case: dict
    source: dict
    target: dict
    window: float
    baseline: float
    minimum: int
    source_message: DatabaseMessage | None = None
    target_message: DatabaseMessage | None = None
    pending: deque = field(default_factory=deque)
    baseline_targets: deque = field(default_factory=deque)
    first_source: float | None = None
    source_count: int = 0
    target_count: int = 0
    matched_count: int = 0
    baseline_count: int = 0
    decode_errors: int = 0
    evidence: list = field(default_factory=list)
    source_evidence: list = field(default_factory=list)
    target_evidence: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def value(self, frame: RawFrame, source: bool) -> float | None:
        message = self.source_message if source else self.target_message
        if message is None:
            return None
        side = "source" if source else "target"
        signal = self.case["signal"][f"{side}_signal"]
        decoded = message.decode(frame.data)
        if signal not in decoded:
            raise ValueError(f"当前多路复用帧不包含信号 {signal}")
        value = float(decoded[signal])
        if not isfinite(value):
            raise ValueError("信号值不是有限数值")
        return value

    def feed(self, index: int, frame: RawFrame) -> None:
        while (
            self.pending
            and frame.timestamp > self.pending[0][1].timestamp + self.window
        ):
            self.pending.popleft()
        while (
            self.baseline_targets
            and self.baseline_targets[0] < frame.timestamp - self.baseline
        ):
            self.baseline_targets.popleft()
        if _matches(frame, self.source):
            if self.first_source is None:
                self.first_source = frame.timestamp
                self.baseline_count = len(self.baseline_targets)
            self.source_count += 1
            if len(self.source_evidence) < _EVIDENCE_LIMIT:
                self.source_evidence.append(_evidence(index, frame))
            try:
                value = self.value(frame, True)
            except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                self.decode_errors += 1
                if not self.reasons:
                    self.reasons.append(f"源信号解码失败：{exc}")
                return
            self.pending.append((index, frame, value))
        if not _matches(frame, self.target, payload=False):
            return
        self.target_count += 1
        if len(self.target_evidence) < _EVIDENCE_LIMIT:
            self.target_evidence.append(_evidence(index, frame))
        if self.first_source is None:
            self.baseline_targets.append(frame.timestamp)
            return
        if not _matches(frame, self.target):
            return
        try:
            target_value = self.value(frame, False)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            self.decode_errors += 1
            if not self.reasons:
                self.reasons.append(f"目标信号解码失败：{exc}")
            return
        candidates = (
            [self.pending[-1]]
            if self.source_message is not None and self.pending
            else reversed(self.pending)
        )
        for source_index, source_frame, source_value in candidates:
            if frame.timestamp < source_frame.timestamp:
                continue
            if self.source_message is not None:
                signal = self.case["signal"]
                expected = source_value * signal["factor"] + signal["offset"]
                if abs(target_value - expected) > signal["tolerance"]:
                    continue
            self.matched_count += 1
            if len(self.evidence) < _EVIDENCE_LIMIT:
                self.evidence.append(
                    {
                        "source": _evidence(source_index, source_frame, source_value),
                        "target": _evidence(index, frame, target_value),
                        "observed_delay_seconds": frame.timestamp
                        - source_frame.timestamp,
                    }
                )
            break


def _route(case: dict, bindings: list[dict]) -> _Route:
    kind = case.get("kind", "frame")
    if kind not in ("frame", "signal", "diagnostic_frame"):
        raise ValueError(
            "kind 只支持 frame/signal/diagnostic_frame；不执行完整诊断事务"
        )
    source = _endpoint(case["source"], source=True)
    target = _endpoint(case["target"], source=False)
    if source["channel"] == target["channel"]:
        raise ValueError("源目标必须属于不同的记录通道")
    window = _number(case["window_seconds"], "window_seconds", positive=True)
    baseline = _number(case.get("baseline_seconds", 0), "baseline_seconds")
    minimum = case.get("minimum_matches", 1)
    if type(minimum) is not int or minimum < 1:
        raise ValueError("minimum_matches 必须为正整数")
    result = _Route(case, source, target, window, baseline, minimum)
    if kind != "signal":
        return result
    signal = case["signal"]
    for key in ("factor", "offset", "tolerance"):
        if (
            key not in signal
            or type(signal[key]) not in (int, float)
            or not isfinite(signal[key])
        ):
            raise ValueError(f"信号规则必须明确有限的 {key}")
    if signal["tolerance"] < 0:
        raise ValueError("tolerance 不能为负数")
    for side, endpoint in (("source", source), ("target", target)):
        path = Path(signal[f"{side}_dbc"]).resolve()
        if path.suffix.lower() != ".dbc":
            raise ValueError("信号路由只接受明确的 DBC 文件")
        candidates = [
            binding
            for binding in bindings
            if Path(binding["path"]).resolve() == path
            and binding["channel"] == endpoint["channel"]
            and binding.get("network") == endpoint.get("network")
            and binding.get("network")
        ]
        if len(candidates) != 1:
            raise ValueError(f"{side} DBC 必须唯一绑定到明确网络和记录通道")
        message = NetworkDatabase(path).resolve_signal(
            endpoint["arbitration_id"],
            signal[f"{side}_signal"],
            bus_type="can",
        )
        if message.decoder.is_extended_frame != endpoint["is_extended_id"]:
            raise ValueError(f"{side} DBC 帧类型与用例不符")
        setattr(result, f"{side}_message", message)
    return result


def validate_cases(
    cases: list[dict], database_bindings: list[dict] | None = None
) -> None:
    """在连接硬件之前核对路由和数据库选择。"""
    if not cases or len(cases) > 100:
        raise ValueError("cases 必须包含 1 至 100 条明确路由")
    for case in cases:
        _route(case, database_bindings or [])


def analyze_routes(
    blf_path: str,
    cases: list[dict],
    evidence_context: dict,
    database_bindings: list[dict] | None = None,
) -> dict:
    """全量扫描测量日志；基础结论仅表示窗口内存在性，不表示逐帧转发率。"""
    if not cases or len(cases) > 100:
        raise ValueError("cases 必须包含 1 至 100 条明确路由")
    routes = [_route(case, database_bindings or []) for case in cases]
    total = 0
    previous = None
    unordered = False
    for total, frame in enumerate(iter_blf(blf_path), start=1):
        if previous is not None and frame.timestamp < previous:
            unordered = True
        previous = frame.timestamp
        for route in routes:
            route.feed(total, frame)
    results = []
    for route in routes:
        reasons = list(route.reasons)
        if evidence_context.get("executed") is not True:
            status = "not_executed"
            reasons.append("未确认本次刺激已执行")
        else:
            reasons.extend(
                f"未确认 {key}"
                for key in _REQUIRED_CONTEXT
                if evidence_context.get(key) is not True
            )
            channels = evidence_context.get("recorded_channels", [])
            if any(
                endpoint["channel"] not in channels
                for endpoint in (route.source, route.target)
            ):
                reasons.append("未确认源目标通道均完整记录")
            if (
                route.source["direction"] == "Rx"
                and evidence_context.get("response_origin_verified") is not True
            ):
                reasons.append("未确认真实响应端来源")
            if not route.source_count:
                reasons.append("日志缺少本次源刺激证据")
            if unordered:
                reasons.append("日志时间未按顺序排列，不能可靠关联")
            if route.baseline_count:
                reasons.append("刺激前基线已有目标同 ID 报文，来源存在歧义")
            if route.decode_errors:
                reasons.append("存在信号解码失败，无法确认完整检查")
            status = (
                "insufficient_evidence"
                if reasons
                else "observed"
                if route.matched_count >= route.minimum
                else "not_observed"
            )
        results.append(
            {
                "name": route.case.get("name", ""),
                "kind": route.case.get("kind", "frame"),
                "status": status,
                "scope": "frame_presence"
                if route.source_message is None
                else "signal_value_presence",
                "window_seconds": route.window,
                "source": route.case["source"],
                "target": route.case["target"],
                "source_count": route.source_count,
                "target_count": route.target_count,
                "matched_count": route.matched_count,
                "baseline_count": route.baseline_count,
                "decode_errors": route.decode_errors,
                "reasons": reasons,
                "evidence": route.evidence,
                "source_evidence": route.source_evidence,
                "target_evidence": route.target_evidence,
                "evidence_limit": _EVIDENCE_LIMIT,
                "diagnostic_transaction_checked": False,
            }
        )
    return {
        "measurement_blf": str(Path(blf_path).resolve()),
        "frames_scanned": total,
        "analysis_complete": True,
        "results": results,
    }
