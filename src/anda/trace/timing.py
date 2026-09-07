"""
文件用途：
- 提供客观的报文时序统计，不在缺少期望周期时推断丢帧。
"""

from math import sqrt
from statistics import fmean, median


def analyze_timestamps(timestamps: list[float]) -> dict:
    if len(timestamps) < 2:
        return {
            "frame_count": len(timestamps),
            "interval_count": 0,
            "period_median_ms": None,
            "period_mean_ms": None,
            "period_min_ms": None,
            "period_max_ms": None,
            "max_gap_ms": None,
            "jitter_stddev_ms": None,
            "jitter_max_abs_deviation_ms": None,
        }

    diffs_ms = [
        (timestamps[i] - timestamps[i - 1]) * 1000.0
        for i in range(1, len(timestamps))
    ]

    median_ms = median(diffs_ms)
    mean_ms = fmean(diffs_ms)
    variance = fmean((period - mean_ms) ** 2 for period in diffs_ms)
    return {
        "frame_count": len(timestamps),
        "interval_count": len(diffs_ms),
        "period_median_ms": median_ms,
        "period_mean_ms": mean_ms,
        "period_min_ms": min(diffs_ms),
        "period_max_ms": max(diffs_ms),
        "max_gap_ms": max(diffs_ms),
        "jitter_stddev_ms": sqrt(variance),
        "jitter_max_abs_deviation_ms": max(
            abs(period - median_ms) for period in diffs_ms
        ),
    }
