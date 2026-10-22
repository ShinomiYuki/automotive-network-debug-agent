"""把审核后的源流量写入独立 BLF，再交给同一 COM 工程在线回放。"""

from pathlib import Path

import can

from anda.tsmaster.models import Bench, Frame, Replay


def prepare_replay(plan: Replay, bench: Bench, output: Path) -> dict:
    if output.exists() or Path(plan.path).resolve() == output.resolve():
        raise ValueError("刺激输出必须是独立的新 BLF")
    try:
        return _write_replay(plan, bench, output)
    except BaseException:
        # 仅移除本次尚未审核完成的刺激产物，避免后续误用部分文件。
        output.unlink(missing_ok=True)
        raise


def _write_replay(plan: Replay, bench: Bench, output: Path) -> dict:
    selections = {item.input_channel: item for item in plan.selections}
    for selection in plan.selections:
        channel = bench.channel(selection.logical_channel)
        if channel.role != "source" or not set(selection.arbitration_ids) <= set(
            channel.allowed_ids
        ):
            raise ValueError("回放仅允许批准的源通道与 ID")
    mappings = [0] * 32
    count = 0
    origin = None
    first_selected = last_selected = None
    with can.BLFReader(plan.path) as reader, can.BLFWriter(str(output)) as writer:
        for message in reader:
            if origin is None:
                origin = message.timestamp
            relative = message.timestamp - origin
            selection = selections.get(message.channel + 1)
            if (
                not selection
                or not plan.start_seconds <= relative <= plan.end_seconds
                or message.arbitration_id not in selection.arbitration_ids
                or ("Rx" if message.is_rx else "Tx") not in selection.directions
            ):
                continue
            if message.is_error_frame or message.is_remote_frame:
                raise ValueError("一期刺激仅支持 CAN/CAN FD 数据帧")
            frame = Frame(
                channel=selection.logical_channel,
                arbitration_id=message.arbitration_id,
                is_extended_id=message.is_extended_id,
                is_fd=message.is_fd,
                bitrate_switch=message.bitrate_switch,
                data_hex=bytes(message.data).hex(),
                count=1,
                interval_ms=1,
            )
            frame.authorize(bench)
            count += 1
            if count > plan.max_frames:
                raise ValueError("选中刺激帧超过 max_frames，拒绝截断后发送")
            if first_selected is None:
                first_selected = message.timestamp
            last_selected = message.timestamp
            mappings[selection.input_channel - 1] = selection.logical_channel
            writer(message)
    if not count:
        raise ValueError("选择没有产生可发送刺激")
    return {
        "input_blf": str(Path(plan.path).resolve()),
        "stimulus_blf": str(output),
        "frame_count": count,
        "duration_seconds": last_selected - first_selected,
        "mappings": ",".join(map(str, mappings)),
        "selection": plan.model_dump(),
        "send_tx": True,
        "send_rx": True,
        "repetitive": False,
        "auto_start": False,
    }
