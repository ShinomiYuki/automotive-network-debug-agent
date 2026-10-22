"""通过合成 BLF 和独立 DBC 验证路由证据边界。"""

from copy import deepcopy

import can
import pytest

from anda.tsmaster.analysis import analyze_routes

CONTEXT = {
    "executed": True,
    "capture_complete": True,
    "direction_verified": True,
    "isolated": True,
    "clock_verified": True,
    "data_frames_only_verified": True,
    "recorded_channels": [1, 2],
}


def endpoint(channel, identifier, **extra):
    return {
        "channel": channel,
        "arbitration_id": identifier,
        "is_extended_id": False,
        "is_fd": False,
        **extra,
    }


def case(**extra):
    return {
        "name": "synthetic route",
        "source": endpoint(1, 0x100),
        "target": endpoint(2, 0x200),
        "window_seconds": 0.05,
        "baseline_seconds": 0.1,
        **extra,
    }


def frame(time, channel, identifier, *, rx=True, data=b"\x01", **extra):
    return can.Message(
        timestamp=1_700_000_000 + time,
        channel=channel - 1,
        arbitration_id=identifier,
        is_rx=rx,
        data=data,
        is_extended_id=False,
        **extra,
    )


def write_blf(tmp_path, frames):
    path = tmp_path / "measurement.blf"
    with can.BLFWriter(str(path)) as writer:
        for item in frames:
            writer(item)
    return str(path)


def result(path, route=None, context=None, bindings=None):
    return analyze_routes(path, [route or case()], context or CONTEXT, bindings)[
        "results"
    ][0]


def test_complete_blf_uses_late_source_and_target_beyond_sample_limit(tmp_path):
    frames = [frame(index * 0.001, 1, 0x999) for index in range(300)]
    frames += [frame(0.4, 1, 0x100, rx=False), frame(0.42, 2, 0x200, data=b"\x98")]
    path = write_blf(tmp_path, frames)
    analysis = analyze_routes(path, [case()], CONTEXT)
    route = analysis["results"][0]
    assert analysis["frames_scanned"] == 302
    assert route["status"] == "observed"
    assert route["evidence"][0]["target"]["frame_index"] == 302
    assert route["evidence"][0]["source"]["direction"] == "Tx"
    assert route["evidence"][0]["target"]["direction"] == "Rx"
    assert route["diagnostic_transaction_checked"] is False


@pytest.mark.parametrize("variant", ["tx", "fd", "late", "error", "data"])
def test_wrong_target_evidence_never_passes(tmp_path, variant):
    source = frame(0, 1, 0x100, rx=False)
    target = frame(
        0.1 if variant == "late" else 0.01,
        2,
        0x200,
        rx=variant != "tx",
        is_fd=variant == "fd",
        is_error_frame=variant == "error",
    )
    route = case()
    if variant == "data":
        route["target"]["data_hex"] = "55"
    assert (
        result(write_blf(tmp_path, [source, target]), route)["status"] == "not_observed"
    )


@pytest.mark.parametrize(
    "missing",
    [
        "capture_complete",
        "direction_verified",
        "isolated",
        "clock_verified",
        "data_frames_only_verified",
        "recorded_channels",
    ],
)
def test_incomplete_context_is_insufficient(tmp_path, missing):
    path = write_blf(tmp_path, [frame(0, 1, 0x100, rx=False), frame(0.01, 2, 0x200)])
    context = dict(CONTEXT)
    context.pop(missing)
    assert result(path, context=context)["status"] == "insufficient_evidence"


def test_no_source_and_not_executed_are_distinct(tmp_path):
    path = write_blf(tmp_path, [frame(0.01, 2, 0x200)])
    assert result(path)["status"] == "insufficient_evidence"
    assert (
        result(path, context={**CONTEXT, "executed": False})["status"] == "not_executed"
    )


def test_baseline_relative_to_actual_first_source(tmp_path):
    path = write_blf(
        tmp_path,
        [
            frame(0, 1, 0x777),
            frame(1, 2, 0x200),
            frame(1.05, 1, 0x100, rx=False),
            frame(1.06, 2, 0x200),
        ],
    )
    route = result(path)
    assert route["baseline_count"] == 1
    assert route["status"] == "insufficient_evidence"


def test_old_target_outside_baseline_does_not_block(tmp_path):
    path = write_blf(
        tmp_path,
        [
            frame(0, 2, 0x200),
            frame(1, 1, 0x100, rx=False),
            frame(1.01, 2, 0x200),
        ],
    )
    assert result(path)["status"] == "observed"


def test_counts_are_full_but_evidence_is_bounded(tmp_path):
    frames = []
    for index in range(250):
        frames += [
            frame(index * 0.1, 1, 0x100, rx=False),
            frame(index * 0.1 + 0.01, 2, 0x200),
        ]
    route = result(write_blf(tmp_path, frames))
    assert route["source_count"] == route["matched_count"] == 250
    assert len(route["evidence"]) == 10


def test_explicit_diagnostic_request_and_real_reverse_response(tmp_path):
    path = write_blf(
        tmp_path,
        [
            frame(0, 1, 0x100, rx=False),
            frame(0.01, 2, 0x200),
            frame(0.02, 2, 0x708),
            frame(0.03, 1, 0x709),
        ],
    )
    reverse = case(
        kind="diagnostic_frame",
        source=endpoint(2, 0x708, direction="Rx"),
        target=endpoint(1, 0x709),
    )
    assert result(path, reverse)["status"] == "insufficient_evidence"
    analysis = analyze_routes(
        path,
        [case(kind="diagnostic_frame"), reverse],
        {**CONTEXT, "response_origin_verified": True},
    )
    assert [item["status"] for item in analysis["results"]] == ["observed", "observed"]
    assert all(
        not item["diagnostic_transaction_checked"] for item in analysis["results"]
    )


def signal_case(tmp_path):
    paths = []
    for filename, identifier, signal in (
        ("source.dbc", 256, "Input"),
        ("target.dbc", 512, "Output"),
    ):
        path = tmp_path / filename
        path.write_text(
            f'VERSION ""\nNS_ :\nBS_:\nBU_: ECU\nBO_ {identifier} Data: 1 ECU\n SG_ {signal} : 0|8@1+ (1,0) [0|255] "" ECU\n',
            encoding="ascii",
        )
        paths.append(str(path))
    route = case(
        kind="signal",
        source=endpoint(1, 0x100, network="source"),
        target=endpoint(2, 0x200, network="target"),
    )
    route["signal"] = {
        "source_dbc": paths[0],
        "target_dbc": paths[1],
        "source_signal": "Input",
        "target_signal": "Output",
        "factor": 2,
        "offset": 1,
        "tolerance": 0,
    }
    bindings = [
        {"path": paths[0], "network": "source", "channel": 1},
        {"path": paths[1], "network": "target", "channel": 2},
    ]
    return route, bindings


def test_signal_decodes_actual_received_target_with_explicit_transform(tmp_path):
    route, bindings = signal_case(tmp_path)
    path = write_blf(
        tmp_path,
        [
            frame(0, 1, 0x100, rx=False, data=b"\x03"),
            frame(0.01, 2, 0x200, data=b"\x07"),
        ],
    )
    checked = result(path, route, bindings=bindings)
    assert checked["status"] == "observed"
    assert checked["evidence"][0]["source"]["signal_value"] == 3
    assert checked["evidence"][0]["target"]["signal_value"] == 7


def test_signal_wrong_value_fails_and_duplicate_binding_is_rejected(tmp_path):
    route, bindings = signal_case(tmp_path)
    path = write_blf(
        tmp_path,
        [
            frame(0, 1, 0x100, rx=False, data=b"\x03"),
            frame(0.01, 2, 0x200, data=b"\x08"),
        ],
    )
    assert result(path, route, bindings=bindings)["status"] == "not_observed"
    with pytest.raises(ValueError, match="唯一绑定"):
        result(path, route, bindings=bindings + [deepcopy(bindings[0])])


def test_signal_compares_with_latest_source_value_in_window(tmp_path):
    route, bindings = signal_case(tmp_path)
    path = write_blf(
        tmp_path,
        [
            frame(0, 1, 0x100, rx=False, data=b"\x03"),
            frame(0.01, 1, 0x100, rx=False, data=b"\x05"),
            frame(0.02, 2, 0x200, data=b"\x07"),
        ],
    )
    checked = result(path, route, bindings=bindings)
    assert checked["status"] == "not_observed"
    assert len(checked["source_evidence"]) == 2
    assert checked["target_evidence"][0]["data_hex"] == "07"


def test_signal_decode_error_returns_insufficient_evidence(tmp_path):
    route, bindings = signal_case(tmp_path)
    path = write_blf(
        tmp_path,
        [
            frame(0, 1, 0x100, rx=False, data=b""),
            frame(0.01, 2, 0x200, data=b"\x07"),
        ],
    )
    checked = result(path, route, bindings=bindings)
    assert checked["status"] == "insufficient_evidence"
    assert checked["decode_errors"] == 1


@pytest.mark.parametrize(
    "change", ["same_channel", "unknown_kind", "zero_channel", "missing_type"]
)
def test_rejects_incomplete_or_unsupported_case(tmp_path, change):
    route = case()
    if change == "same_channel":
        route["target"]["channel"] = 1
    elif change == "unknown_kind":
        route["kind"] = "uds_transaction"
    elif change == "zero_channel":
        route["source"]["channel"] = 0
    else:
        del route["source"]["is_fd"]
    with pytest.raises(ValueError):
        result(str(tmp_path / "unused.blf"), route)
