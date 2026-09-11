"""
文件用途：
- 在同一 Trace Session 内关联源报文停止事件与多个目标 Channel 的 timeout 值切换。
- 返回可复查的总线事实、分 Channel 统计和有限异常原始帧，不推断软件根因。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from math import isclose
from statistics import median

from anda.common.errors import TraceDatabaseError, TraceInputError
from anda.trace.dbc import DatabaseMessage, NetworkDatabase
from anda.trace.store import CompactFrameStore
from anda.trace.timing import analyze_timestamps

MAX_TIMEOUT_EVENTS = 200
MAX_ABNORMAL_RAW_FRAMES = 50


@dataclass(frozen=True, slots=True)
class _SignalSample:
    timestamp: float
    value: object
    position: int


def analyze_routed_signal_timeout(
    *,
    store: CompactFrameStore,
    database: NetworkDatabase | None,
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
    """关联停止事件；只有日志和数据库可直接证明的事实进入结果。"""
    if configured_timeout_ms <= 0:
        raise TraceInputError("configured_timeout_ms 必须大于 0")
    if source_channel < 1 or not target_channels or any(item < 1 for item in target_channels):
        raise TraceInputError("source_channel 和 target_channels 必须使用 1-based 编号")
    if len(target_channels) > 16 or len(set(target_channels)) != len(target_channels):
        raise TraceInputError("target_channels 必须包含 1 到 16 个不重复 Channel")
    if not signal_name.strip():
        raise TraceInputError("signal_name 不能为空")
    source_bus_type = _bus_type(source_bus_type, "source_bus_type")
    target_bus_type = _bus_type(target_bus_type, "target_bus_type")
    source_frame_id, source_message_name = _resolve_frame_selector(
        database,
        source_message,
        source_bus_type,
        source_database_file,
    )
    if database is None:
        raise TraceDatabaseError("关联 timeout 值需要加载目标 DBC/ARXML/LDF 数据库")

    target_messages: dict[int, DatabaseMessage] = {}
    target_frame_ids: dict[int, int] = {}
    target_database_files = target_database_files or {}
    for channel in target_channels:
        database_file = target_database_files.get(str(channel))
        frame_id, _ = _resolve_frame_selector(
            database, target_frame, target_bus_type, database_file
        )
        message = database.resolve_signal(
            frame_id,
            signal_name,
            bus_type=target_bus_type,
            database_file=database_file,
        )
        target_messages[channel] = message
        target_frame_ids[channel] = message.frame_id

    source_positions = list(
        store.matching_indices(source_frame_id, source_channel, bus_type=source_bus_type)
    )
    if not source_positions:
        raise TraceInputError(
            f"日志中未找到 {source_bus_type.upper()}{source_channel} "
            f"Frame 0x{source_frame_id:X}"
        )
    source_events = _find_source_stop_events(
        store, source_positions, configured_timeout_ms
    )

    samples_by_channel: dict[int, list[_SignalSample]] = {}
    timestamps_by_channel: dict[int, list[float]] = {}
    timing_by_channel: dict[int, dict] = {}
    decode_errors: dict[int, int] = {}
    for channel, message in target_messages.items():
        samples, error_count = _decode_target_samples(
            store, message, target_frame_ids[channel], channel, signal_name
        )
        samples_by_channel[channel] = samples
        timestamps = [sample.timestamp for sample in samples]
        timestamps_by_channel[channel] = timestamps
        decode_errors[channel] = error_count
        timing_by_channel[channel] = analyze_timestamps(timestamps)

    completed_delays: dict[int, list[float]] = {
        channel: [] for channel in target_channels
    }
    missed_counts: dict[int, int] = {channel: 0 for channel in target_channels}
    abnormal_raw_frames = []
    abnormal_event_count = 0
    events = []
    for event_index, (source_position, window_end) in enumerate(source_events, start=1):
        last_source_timestamp = store.timestamp_at(source_position)
        deadline = last_source_timestamp + configured_timeout_ms / 1000.0
        channel_results = []
        for channel in target_channels:
            result = _match_target_event(
                store,
                samples_by_channel[channel],
                timestamps_by_channel[channel],
                deadline,
                window_end,
                timeout_value,
                last_source_timestamp,
            )
            delay = result["source_to_timeout_ms"]
            period = timing_by_channel[channel]["period_median_ms"]
            expected_max = (
                configured_timeout_ms + period if period is not None else None
            )
            result.update(
                {
                    "analysis_channel": channel,
                    "database_file": target_messages[channel].path.name,
                    "message_name": target_messages[channel].name,
                    "lin_period_median_ms": period,
                    "expected_max_ms_from_config_plus_period": expected_max,
                    "before_configured_timeout": (
                        delay is not None and delay < configured_timeout_ms - 0.1
                    ),
                    "exceeds_expected_max": (
                        delay is not None
                        and expected_max is not None
                        and delay > expected_max + 0.1
                    ),
                }
            )
            result["is_timing_abnormal"] = (
                result["before_configured_timeout"]
                or result["exceeds_expected_max"]
            )
            if delay is not None:
                completed_delays[channel].append(delay)
            missed_counts[channel] += result["missed_bus_frame_count"]
            if result["is_timing_abnormal"]:
                abnormal_event_count += 1
                if len(abnormal_raw_frames) < MAX_ABNORMAL_RAW_FRAMES:
                    abnormal_raw_frames.append(
                        {
                            "event_index": event_index,
                            "analysis_channel": channel,
                            "source_frame": store.frame_dict(source_position),
                            "first_target_timeout_frame": result[
                                "first_target_timeout_frame"
                            ],
                            "last_target_old_before_timeout": result[
                                "last_target_old_before_timeout"
                            ],
                        }
                    )
            channel_results.append(result)
        if len(events) < MAX_TIMEOUT_EVENTS:
            events.append(
                {
                    "event_index": event_index,
                    "last_source_frame": store.frame_dict(source_position),
                    "deadline_timestamp": deadline,
                    "source_gap_window_end": window_end,
                    "targets": channel_results,
                }
            )

    per_channel = []
    for channel in target_channels:
        delays = completed_delays[channel]
        per_channel.append(
            {
                "analysis_channel": channel,
                "target_frame_id": target_frame_ids[channel],
                "target_frame_id_hex": f"0x{target_frame_ids[channel]:X}",
                "message_name": target_messages[channel].name,
                "database_file": target_messages[channel].path.name,
                "decoded_frame_count": len(samples_by_channel[channel]),
                "decode_error_count": decode_errors[channel],
                "lin_timing": timing_by_channel[channel],
                "completed_event_count": len(delays),
                "missed_bus_frame_count": missed_counts[channel],
                "source_to_timeout_ms": _distribution(delays),
            }
        )

    return {
        "source": {
            "bus_type": source_bus_type,
            "analysis_channel": source_channel,
            "message_selector": source_message,
            "message_name": source_message_name,
            "frame_id": source_frame_id,
            "frame_id_hex": f"0x{source_frame_id:X}",
            "frame_count": len(source_positions),
        },
        "target": {
            "bus_type": target_bus_type,
            "analysis_channels": target_channels,
            "frame_selector": target_frame,
            "frame_ids_by_channel": {
                str(channel): target_frame_ids[channel] for channel in target_channels
            },
            "frame_ids_hex_by_channel": {
                str(channel): f"0x{target_frame_ids[channel]:X}"
                for channel in target_channels
            },
            "signal_name": signal_name,
            "timeout_value": timeout_value,
        },
        "configured_timeout_ms": configured_timeout_ms,
        "detected_source_stop_event_count": len(source_events),
        "returned_event_count": len(events),
        "events_truncated": len(source_events) > len(events),
        "events": events,
        "per_channel_statistics": per_channel,
        "abnormal_event_count": abnormal_event_count,
        "abnormal_raw_frames": abnormal_raw_frames,
        "abnormal_raw_frames_truncated": abnormal_event_count
        > len(abnormal_raw_frames),
        "root_cause_inferred": False,
        "interpretation_boundary": (
            "结果只关联 BLF 中记录器时间轴上的源帧停止、目标帧取值和总线周期；"
            "不证明 ECU 内部 timeout、COM 路由或 LinIf 取数的真实执行时刻。"
        ),
    }


def _resolve_frame_selector(
    database: NetworkDatabase | None,
    selector: str,
    bus_type: str,
    database_file: str | None,
) -> tuple[int, str | None]:
    value = selector.strip()
    try:
        if value.casefold().startswith("0x") or value.isdecimal():
            frame_id = int(value, 0)
            maximum = 0x3F if bus_type == "lin" else 0x1FFFFFFF
            if not 0 <= frame_id <= maximum:
                raise TraceInputError(f"{bus_type.upper()} Frame ID 超出范围：{selector}")
            return frame_id, None
    except ValueError:
        pass
    if database is None:
        raise TraceDatabaseError(f"按报文名 {selector} 查询需要加载数据库")
    message = database.resolve_message(
        selector, bus_type=bus_type, database_file=database_file
    )
    return message.frame_id, message.name


def _find_source_stop_events(
    store: CompactFrameStore, source_positions: list[int], timeout_ms: float
) -> list[tuple[int, float]]:
    result = []
    for index, position in enumerate(source_positions):
        timestamp = store.timestamp_at(position)
        window_end = (
            store.timestamp_at(source_positions[index + 1])
            if index + 1 < len(source_positions)
            else store.end_timestamp
        )
        if window_end is None:
            continue
        if (window_end - timestamp) * 1000 >= timeout_ms:
            result.append((position, window_end))
    return result


def _decode_target_samples(
    store: CompactFrameStore,
    message: DatabaseMessage,
    frame_id: int,
    channel: int,
    signal_name: str,
) -> tuple[list[_SignalSample], int]:
    result = []
    error_count = 0
    for position in store.matching_indices(
        frame_id, channel, bus_type=message.bus_type
    ):
        try:
            decoded = message.decode(store.payload_at(position))
        except TraceDatabaseError:
            error_count += 1
            continue
        result.append(
            _SignalSample(
                store.timestamp_at(position), decoded[signal_name], position
            )
        )
    return result, error_count


def _match_target_event(
    store: CompactFrameStore,
    decoded_samples: list[_SignalSample],
    timestamps: list[float],
    deadline: float,
    window_end: float,
    timeout_value: object,
    last_source_timestamp: float,
) -> dict:
    start = bisect_left(timestamps, last_source_timestamp)
    first_old_after_source = None
    first_old_after_deadline = None
    previous = decoded_samples[start - 1] if start > 0 else None
    previous_is_old = bool(
        previous is not None and not _values_equal(previous.value, timeout_value)
    )
    last_old = (
        {
            **store.frame_dict(previous.position),
            "signal_value": previous.value,
        }
        if previous_is_old and previous is not None
        else None
    )
    first_timeout = None
    missed = 0
    for sample in decoded_samples[start:]:
        if sample.timestamp >= window_end:
            break
        scalar = sample.value
        frame = {**store.frame_dict(sample.position), "signal_value": scalar}
        if _values_equal(scalar, timeout_value) and (
            previous_is_old or first_old_after_source is not None
        ):
            first_timeout = frame
            break
        if not _values_equal(scalar, timeout_value):
            if first_old_after_source is None:
                first_old_after_source = frame
            last_old = frame
            if sample.timestamp >= deadline:
                if first_old_after_deadline is None:
                    first_old_after_deadline = frame
                missed += 1
    timeout_timestamp = first_timeout["timestamp"] if first_timeout else None
    return {
        "status": "timeout_value_observed" if first_timeout else "timeout_value_not_observed",
        "first_target_old_after_source_stop": first_old_after_source,
        "first_target_old_after_deadline": first_old_after_deadline,
        "last_target_old_before_timeout": last_old,
        "first_target_timeout_frame": first_timeout,
        "source_to_timeout_ms": (
            (timeout_timestamp - last_source_timestamp) * 1000
            if timeout_timestamp is not None
            else None
        ),
        "deadline_to_timeout_ms": (
            (timeout_timestamp - deadline) * 1000
            if timeout_timestamp is not None
            else None
        ),
        "missed_bus_frame_count": missed,
    }


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": median(ordered),
        "p95": _percentile(ordered, 0.95),
        "max": ordered[-1],
    }


def _percentile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _values_equal(actual: object, expected: object) -> bool:
    if (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ):
        return isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-9)
    return actual == expected


def _bus_type(value: str, field_name: str) -> str:
    normalized = value.strip().casefold()
    if normalized not in {"can", "lin"}:
        raise TraceInputError(f"{field_name} 仅支持 can 或 lin")
    return normalized
