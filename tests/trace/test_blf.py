"""验证真实 BLF 的经典 CAN、CAN FD、Channel、时间戳与 payload 读取。"""

import pytest

from anda.common.errors import TraceInputError
from anda.trace.blf import iter_blf


def test_iter_blf_preserves_core_frame_fields(trace_files):
    blf_path, _ = trace_files
    frames = list(iter_blf(blf_path))

    first = frames[0]
    assert first.timestamp == pytest.approx(1_700_000_000.0)
    assert first.channel == 1
    assert first.arbitration_id == 0x100
    assert first.dlc == 8
    assert first.data == bytes.fromhex("40 1F 00 00 00 00 00 00")
    assert first.is_fd is False


def test_iter_blf_reads_can_fd_properties(trace_files):
    blf_path, _ = trace_files
    frame = next(item for item in iter_blf(blf_path) if item.arbitration_id == 0x300)

    assert frame.channel == 1
    assert frame.dlc == 16
    assert frame.data == bytes(range(16))
    assert frame.is_fd is True
    assert frame.bitrate_switch is True


def test_iter_blf_exposes_vector_one_based_channels(trace_files):
    blf_path, _ = trace_files
    channels = {frame.channel for frame in iter_blf(blf_path)}
    assert channels == {1, 2}


def test_iter_blf_rejects_missing_file(tmp_path):
    with pytest.raises(TraceInputError, match="BLF 文件不存在"):
        list(iter_blf(tmp_path / "missing.blf"))


def test_iter_blf_wraps_corrupt_file_error(tmp_path):
    path = tmp_path / "corrupt.blf"
    path.write_bytes(b"not a BLF")

    with pytest.raises(TraceInputError, match="BLF 读取失败: corrupt.blf"):
        list(iter_blf(path))
