"""
文件用途：
- 定义 Config Core 在内存中使用的最小关系模型。
- 模型只保存对象名、引用和证据位置，不长期保存整份 XML 或源码正文。

设计边界：
- 这些类型描述“工程里配置了什么”，不表达配置是否正确。
- 所有对 MCP 的输出都通过 ``as_dict`` 转成可审查的普通字典。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ConfigEvidence:
    """一个配置对象或源码符号的可定位证据。"""

    file: str
    line: int
    object_type: str
    object_name: str
    xml_path: str | None = None
    reference_path: str | None = None

    def as_dict(self) -> dict:
        """返回适合 MCP 序列化的紧凑证据。"""
        return {
            "file": self.file,
            "line": self.line,
            "object_type": self.object_type,
            "object_name": self.object_name,
            "xml_path": self.xml_path,
            "reference_path": self.reference_path,
        }


@dataclass(frozen=True, slots=True)
class ConfigValue:
    """ECUC 参数或引用；名称来自定义路径，值来自对应 VALUE/VALUE-REF。"""

    name: str
    definition_ref: str
    value: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "definition_ref": self.definition_ref,
            "value": self.value,
        }


@dataclass(slots=True)
class ConfigObject:
    """ARXML 中带 SHORT-NAME 的一个配置对象。"""

    name: str
    long_name: str | None
    kind: str
    reference_path: str
    definition_ref: str | None
    parameter_values: tuple[ConfigValue, ...]
    reference_values: tuple[ConfigValue, ...]
    evidence: ConfigEvidence

    @property
    def references(self) -> tuple[str, ...]:
        """兼容基础导航查询的紧凑引用值视图。"""
        return tuple(item.value for item in self.reference_values)


@dataclass(frozen=True, slots=True)
class FrameDefinition:
    """CAN-FRAME 与其承载 PDU 的确定性映射。"""

    name: str
    reference_path: str
    pdu_references: tuple[str, ...]
    length_bytes: int | None
    evidence: ConfigEvidence


@dataclass(frozen=True, slots=True)
class FrameTrigger:
    """CAN ID、物理网段与 CAN-FRAME 的绑定。"""

    name: str
    arbitration_id: int
    frame_reference: str
    network: str | None
    evidence: ConfigEvidence


@dataclass(frozen=True, slots=True)
class RouteEndpoint:
    """PduR Routing Path 中的 Source 或 Destination PDU。"""

    pdu_reference: str
    evidence: ConfigEvidence


@dataclass(frozen=True, slots=True)
class RoutePath:
    """一个 PduR Routing Path 及其端点。"""

    name: str
    source: RouteEndpoint | None
    destinations: tuple[RouteEndpoint, ...]
    evidence: ConfigEvidence


@dataclass(frozen=True, slots=True)
class MessageBinding:
    """完成跨对象关联后的 Message/CAN ID/PDU 入口。"""

    name: str
    arbitration_id: int
    network: str | None
    pdu_references: tuple[str, ...]
    evidence: tuple[ConfigEvidence, ...]
    direction: str | None = None
    length_bytes: int | None = None
    can_id_type: str | None = None
    is_extended: bool | None = None
    is_fd: bool | None = None


@dataclass(frozen=True, slots=True)
class SignalGateway:
    """ComGwMapping 中一个 Source Signal 到若干 Destination Signal 的关系。"""

    name: str
    source_reference: str | None
    destination_references: tuple[str, ...]
    evidence: ConfigEvidence


@dataclass(slots=True)
class ArxmlIndex:
    """所有 ARXML 文件合并后的轻量索引。"""

    objects: list[ConfigObject] = field(default_factory=list)
    frames: list[FrameDefinition] = field(default_factory=list)
    triggers: list[FrameTrigger] = field(default_factory=list)
    routes: list[RoutePath] = field(default_factory=list)
    pdu_networks: dict[str, set[str]] = field(default_factory=dict)
    messages: list[MessageBinding] = field(default_factory=list)
    signal_gateways: list[SignalGateway] = field(default_factory=list)
