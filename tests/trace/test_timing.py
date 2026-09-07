"""验证周期、异常间隔、单帧和空结果的客观时序统计。"""

import pytest

from anda.trace.timing import analyze_timestamps


def test_analyze_timestamps_regular_period():
    result = analyze_timestamps([0.0, 0.02, 0.04, 0.06])

    assert result["frame_count"] == 4
    assert result["interval_count"] == 3
    assert result["period_median_ms"] == pytest.approx(20.0)
    assert result["jitter_stddev_ms"] == pytest.approx(0.0, abs=1e-12)


def test_analyze_timestamps_irregular_period():
    result = analyze_timestamps([0.0, 0.01, 0.03, 0.06])

    assert result["period_min_ms"] == pytest.approx(10.0)
    assert result["period_max_ms"] == pytest.approx(30.0)
    assert result["max_gap_ms"] == pytest.approx(30.0)
    assert result["jitter_stddev_ms"] > 0


@pytest.mark.parametrize("timestamps, expected_count", [([], 0), ([1.0], 1)])
def test_analyze_timestamps_has_no_period_for_less_than_two_frames(
    timestamps, expected_count
):
    result = analyze_timestamps(timestamps)

    assert result["frame_count"] == expected_count
    assert result["interval_count"] == 0
    assert result["period_median_ms"] is None
    assert result["jitter_stddev_ms"] is None
