"""Dynamic Core 的稳定数据模型与副作用语义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class SideEffectLevel(StrEnum):
    """Dynamic Tool 的副作用等级。"""

    READ_ONLY = "READ_ONLY"
    LOCAL_MUTATION = "LOCAL_MUTATION"
    RUNTIME_MUTATION = "RUNTIME_MUTATION"
    BUS_ACTIVE = "BUS_ACTIVE"


class DynamicMode(StrEnum):
    AUTO = "auto"
    OFFLINE = "offline"
    ONLINE = "online"


class ConfigurationOrigin(StrEnum):
    TEMPORARY_CONFIGURATION = "temporary_configuration"
    SHADOW_COPY = "shadow_copy"


class RuntimeCapability(StrEnum):
    OBSERVATION = "observation"
    OFFLINE_ANIMATION = "offline_animation"
    MEASUREMENT = "measurement"
    SIGNAL_WRITE = "signal_write"
    VARIABLE_WRITE = "variable_write"
    CAPL_CALL = "capl_call"
    REPLAY = "replay"
    FRAME_INJECTION = "frame_injection"
    DIAGNOSTIC = "diagnostic"
    TEST = "test"
    LOGGING = "logging"


DEFAULT_RUNTIME_CAPABILITIES = frozenset(
    {
        RuntimeCapability.OBSERVATION,
        RuntimeCapability.OFFLINE_ANIMATION,
        RuntimeCapability.MEASUREMENT,
    }
)


@dataclass(frozen=True, slots=True)
class DatabaseSpec:
    path: Path
    bus: str
    channel: int
    network_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "bus": self.bus,
            "channel": self.channel,
            "network_name": self.network_name,
        }


@dataclass(slots=True)
class DynamicSession:
    dynamic_session_id: str
    mode: DynamicMode
    configuration_origin: ConfigurationOrigin
    runtime_capabilities: frozenset[RuntimeCapability]
    worker: Any
    temp_directory: Path
    original_configuration: Path | None = None
    original_configuration_digest: str | None = None
    shadow_configuration: Path | None = None
    blf_path: Path | None = None
    database_specs: tuple[DatabaseSpec, ...] = ()
    trace_id: str | None = None
    backend_status: dict[str, Any] = field(default_factory=dict)
    active_operations: set[str] = field(default_factory=set)
    closed: bool = False

    def policy_dict(self) -> dict[str, Any]:
        return {
            "allowed": sorted(item.value for item in self.runtime_capabilities),
            "defaulted": self.runtime_capabilities == DEFAULT_RUNTIME_CAPABILITIES,
        }
