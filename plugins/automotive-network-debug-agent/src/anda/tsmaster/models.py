"""显式台架参数与有限刺激，不从日志或设备名称猜测配置。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Channel(InputModel):
    network: str = Field(min_length=1, max_length=100)
    logical_channel: int = Field(ge=1, le=32)
    role: Literal["source", "target"]
    device_type: int = Field(ge=1)
    device_subtype: int
    device_index: int = Field(ge=0)
    device_channel: int = Field(ge=0)
    device_name: str = Field(min_length=1, max_length=200)
    arbitration_kbps: float = Field(gt=0)
    data_kbps: float = Field(gt=0)
    controller_type: Literal[0, 1, 2]
    termination: bool
    allowed_ids: list[int] = Field(default_factory=list, max_length=2048)
    response_ids: list[int] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def check_ids(self):
        if any(
            not 0 <= value <= 0x1FFFFFFF
            for value in self.allowed_ids + self.response_ids
        ):
            raise ValueError("CAN 标识符超出范围")
        if self.role == "target" and self.allowed_ids:
            raise ValueError("目标口仅可通过 response_ids 显式批准诊断响应例外")
        return self


class DatabaseBinding(InputModel):
    path: str
    network: str
    logical_channel: int = Field(ge=1, le=32)


class Bench(InputModel):
    channels: list[Channel] = Field(min_length=2, max_length=32)
    databases: list[DatabaseBinding] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def check_bindings(self):
        logical = [item.logical_channel for item in self.channels]
        physical = [
            (c.device_type, c.device_subtype, c.device_index, c.device_channel)
            for c in self.channels
        ]
        networks = [item.network for item in self.channels]
        if (
            len(set(logical)) != len(logical)
            or len(set(physical)) != len(physical)
            or len(set(networks)) != len(networks)
        ):
            raise ValueError("网络、逻辑通道与物理端口必须各自唯一")
        if {c.role for c in self.channels} != {"source", "target"}:
            raise ValueError("必须明确源口和目标口")
        for binding in self.databases:
            if not any(
                c.logical_channel == binding.logical_channel
                and c.network == binding.network
                for c in self.channels
            ):
                raise ValueError("DBC 绑定必须与明确的网络和逻辑通道相符")
            if (
                not Path(binding.path).is_file()
                or Path(binding.path).suffix.lower() != ".dbc"
            ):
                raise ValueError(f"DBC 文件不存在或扩展名错误: {binding.path}")
        return self

    def channel(self, number: int) -> Channel:
        for item in self.channels:
            if item.logical_channel == number:
                return item
        raise ValueError(f"未配置逻辑通道 {number}")


class Frame(InputModel):
    channel: int = Field(ge=1, le=32)
    arbitration_id: int = Field(ge=0, le=0x1FFFFFFF)
    is_extended_id: bool
    is_fd: bool
    bitrate_switch: bool
    data_hex: str = Field(max_length=192)
    count: int = Field(ge=1, le=10000)
    interval_ms: float = Field(ge=1, le=10000)
    purpose: Literal["stimulus", "diagnostic_response"] = "stimulus"

    @model_validator(mode="after")
    def check_frame(self):
        data = bytes.fromhex(self.data_hex)
        if not self.is_extended_id and self.arbitration_id > 0x7FF:
            raise ValueError("标准帧 ID 不能超过 0x7FF")
        if len(data) not in FD_LENGTHS or (not self.is_fd and len(data) > 8):
            raise ValueError("数据长度不符合 CAN/CAN FD DLC")
        if self.bitrate_switch and not self.is_fd:
            raise ValueError("经典 CAN 不支持 BRS")
        return self

    def authorize(self, bench: Bench):
        channel = bench.channel(self.channel)
        allowed = (
            channel.allowed_ids if self.purpose == "stimulus" else channel.response_ids
        )
        if self.arbitration_id not in allowed or (
            self.purpose == "stimulus" and channel.role != "source"
        ):
            raise ValueError("通道、ID 或诊断响应例外未被批准")
        if self.is_fd and channel.controller_type == 0:
            raise ValueError("经典 CAN 通道不能发送 FD")


class ReplaySelection(InputModel):
    input_channel: int = Field(ge=1, le=32)
    logical_channel: int = Field(ge=1, le=32)
    arbitration_ids: list[int] = Field(min_length=1, max_length=2048)
    directions: list[Literal["Rx", "Tx"]] = Field(min_length=1, max_length=2)


class Replay(InputModel):
    path: str
    selections: list[ReplaySelection] = Field(min_length=1, max_length=32)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0, le=3600)
    max_frames: int = Field(ge=1, le=100000)

    @model_validator(mode="after")
    def check_window(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("回放结束必须晚于开始")
        channels = [item.input_channel for item in self.selections]
        if len(set(channels)) != len(channels):
            raise ValueError("每个原日志通道只能指定一个源映射")
        return self
