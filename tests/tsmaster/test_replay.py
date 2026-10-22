"""验证源刺激隔离、回放编号及失败时不保留可发送的部分文件。"""

from pathlib import Path

import can
import pytest

from anda.tsmaster.models import Bench, Frame, Replay
from anda.tsmaster.replay import prepare_replay


def bench():
    common = {
        "device_type": 3,
        "device_subtype": 8,
        "device_index": 0,
        "device_name": "Synthetic device",
        "arbitration_kbps": 500,
        "data_kbps": 2000,
        "controller_type": 1,
        "termination": False,
    }
    return Bench.model_validate(
        {
            "channels": [
                {
                    **common,
                    "network": "source",
                    "logical_channel": 3,
                    "role": "source",
                    "device_channel": 0,
                    "allowed_ids": [0x100, 0x12345],
                },
                {
                    **common,
                    "network": "target",
                    "logical_channel": 6,
                    "role": "target",
                    "device_channel": 1,
                    "response_ids": [0x708],
                },
            ]
        }
    )


def message(offset, channel, identifier=0x100, **values):
    options = {
        "timestamp": 1_700_000_000 + offset,
        "channel": channel - 1,
        "arbitration_id": identifier,
        "data": b"\x01",
        "is_rx": True,
        "is_extended_id": False,
    }
    options.update(values)
    return can.Message(**options)


def write_input(tmp_path, frames):
    path = tmp_path / "original.blf"
    with can.BLFWriter(str(path)) as writer:
        for frame in frames:
            writer(frame)
    return path


def plan(path, **extra):
    return Replay.model_validate(
        {
            "path": str(path),
            "selections": [
                {
                    "input_channel": 2,
                    "logical_channel": 3,
                    "arbitration_ids": [0x100],
                    "directions": ["Rx"],
                }
            ],
            "start_seconds": 0,
            "end_seconds": 1,
            "max_frames": 100,
            **extra,
        }
    )


def read_output(path):
    with can.BLFReader(str(path)) as reader:
        return list(reader)


def test_only_approved_source_rx_is_retained_and_target_history_is_excluded(tmp_path):
    path = write_input(
        tmp_path,
        [
            message(0, 2),
            message(0.01, 2, 0x101),
            message(0.02, 5, 0x100),
            message(0.03, 2, is_rx=False),
            message(0.04, 2, data=b"\x02"),
        ],
    )
    output = tmp_path / "stimulus.blf"
    result = prepare_replay(plan(path), bench(), output)
    frames = read_output(output)
    assert result["frame_count"] == 2
    assert len(frames) == 2
    assert all(
        frame.channel == 1 and frame.arbitration_id == 0x100 and frame.is_rx
        for frame in frames
    )
    assert [bytes(frame.data) for frame in frames] == [b"\x01", b"\x02"]
    mappings = list(map(int, result["mappings"].split(",")))
    assert len(mappings) == 32
    assert mappings[1] == 3
    assert all(value == 0 for index, value in enumerate(mappings) if index != 1)
    assert result["send_rx"] is True
    assert result["auto_start"] is result["repetitive"] is False
    assert Path(result["input_blf"]) != Path(result["stimulus_blf"])


@pytest.mark.parametrize(
    "selection",
    [
        {
            "input_channel": 2,
            "logical_channel": 6,
            "arbitration_ids": [0x708],
            "directions": ["Rx"],
        },
        {
            "input_channel": 2,
            "logical_channel": 3,
            "arbitration_ids": [0x101],
            "directions": ["Rx"],
        },
    ],
)
def test_unapproved_role_or_id_is_rejected_before_output(tmp_path, selection):
    path = write_input(tmp_path, [message(0, 2)])
    output = tmp_path / "rejected.blf"
    with pytest.raises(ValueError, match="批准的源通道"):
        prepare_replay(plan(path, selections=[selection]), bench(), output)
    assert not output.exists()


def test_extended_fd_brs_and_data_are_preserved(tmp_path):
    path = write_input(
        tmp_path,
        [
            message(
                0,
                2,
                0x12345,
                is_extended_id=True,
                is_fd=True,
                bitrate_switch=True,
                data=bytes(range(64)),
            )
        ],
    )
    output = tmp_path / "fd.blf"
    selection = {
        "input_channel": 2,
        "logical_channel": 3,
        "arbitration_ids": [0x12345],
        "directions": ["Rx"],
    }
    prepare_replay(plan(path, selections=[selection]), bench(), output)
    frame = read_output(output)[0]
    assert frame.arbitration_id == 0x12345
    assert frame.is_extended_id is frame.is_fd is frame.bitrate_switch is True
    assert bytes(frame.data) == bytes(range(64))


@pytest.mark.parametrize("invalid", ["remote", "fd_length", "error"])
def test_invalid_selected_frame_is_rejected_without_partial_stimulus(tmp_path, invalid):
    extra = (
        {"is_remote_frame": True, "data": b""}
        if invalid == "remote"
        else (
            {"is_fd": True, "data": bytes(9)}
            if invalid == "fd_length"
            else {"is_error_frame": True}
        )
    )
    path = write_input(tmp_path, [message(0, 2), message(0.01, 2, **extra)])
    output = tmp_path / "invalid.blf"
    with pytest.raises(ValueError):
        prepare_replay(plan(path), bench(), output)
    assert not output.exists()


def test_max_frames_never_returns_or_leaves_truncated_stimulus(tmp_path):
    path = write_input(tmp_path, [message(index * 0.01, 2) for index in range(3)])
    output = tmp_path / "limited.blf"
    with pytest.raises(ValueError, match="max_frames"):
        prepare_replay(plan(path, max_frames=2), bench(), output)
    assert not output.exists()


def test_empty_selection_is_not_a_sendable_file(tmp_path):
    path = write_input(tmp_path, [message(0, 5)])
    output = tmp_path / "empty.blf"
    with pytest.raises(ValueError, match="没有产生"):
        prepare_replay(plan(path), bench(), output)
    assert not output.exists()


def test_existing_output_and_input_overwrite_are_refused(tmp_path):
    path = write_input(tmp_path, [message(0, 2)])
    output = tmp_path / "existing.blf"
    output.write_bytes(b"owned by user")
    with pytest.raises(ValueError, match="独立的新 BLF"):
        prepare_replay(plan(path), bench(), output)
    assert output.read_bytes() == b"owned by user"
    with pytest.raises(ValueError, match="独立的新 BLF"):
        prepare_replay(plan(path), bench(), path)


@pytest.mark.parametrize(
    "values",
    [
        {"is_fd": True, "data_hex": "00" * 9},
        {"is_fd": False, "bitrate_switch": True},
        {"arbitration_id": 0x12345, "is_extended_id": False},
    ],
)
def test_frame_model_rejects_invalid_lengths_brs_and_identifier(values):
    data = {
        "channel": 3,
        "arbitration_id": 0x100,
        "is_extended_id": False,
        "is_fd": False,
        "bitrate_switch": False,
        "data_hex": "01",
        "count": 1,
        "interval_ms": 1,
        **values,
    }
    with pytest.raises(ValueError):
        Frame.model_validate(data)
