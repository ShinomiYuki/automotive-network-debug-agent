"""
文件用途：
- 使用 Python 标准库解析 AUTOSAR ARXML 的公共结构。
- 建立 CAN Frame、PDU、物理网段与 PduR Routing Path 的轻量关系索引。

支持边界：
- 通过 XML local-name 处理不同 AUTOSAR namespace URI，不绑定某个版本号。
- 首版覆盖 CAN-FRAME-TRIGGERING、CAN-FRAME/PDU 映射、PDU-TRIGGERING，
  以及 ECUC PduRRoutingPath 的 Source/Destination 容器。
- 不尝试解释厂商私有语义；无法确定的字段保留为空，不根据命名猜根因。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Iterator
from html import unescape
from pathlib import Path

from anda.common.errors import ConfigParseError
from anda.config.models import (
    ArxmlIndex,
    ConfigEvidence,
    ConfigObject,
    ConfigValue,
    FrameDefinition,
    FrameTrigger,
    MessageBinding,
    RouteEndpoint,
    RoutePath,
    SignalGateway,
)

_SHORT_NAME_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?SHORT-NAME>\s*([^<]+?)\s*</",
    re.IGNORECASE,
)
_MAX_REFERENCES_PER_OBJECT = 512
_MAX_PARAMETERS_PER_OBJECT = 128


def local_name(tag: str) -> str:
    """去掉 ElementTree namespace，返回 AUTOSAR 标签本地名。"""
    return tag.rsplit("}", 1)[-1].upper()


def reference_name(reference: str) -> str:
    """从 AUTOSAR 绝对引用中取得最后一个 SHORT-NAME。"""
    return reference.rstrip("/").rsplit("/", 1)[-1]


def normalize_identifier(value: str) -> str:
    """统一比较定义名，兼容连字符和大小写差异。"""
    return "".join(character for character in value.upper() if character.isalnum())


def parse_arxml_files(paths: list[Path]) -> ArxmlIndex:
    """解析一组 ARXML/XML 文件并完成跨文件关系关联。"""
    index = ArxmlIndex()
    for path in paths:
        _parse_one_file(path, index)
    _build_message_bindings(index)
    return index


def _parse_one_file(path: Path, index: ArxmlIndex) -> None:
    """解析单个文件；XML 语法错误转换为工程人员可理解的业务异常。"""
    try:
        line_map = _short_name_lines(path)
        root = ET.parse(path).getroot()
    except (OSError, UnicodeError) as error:
        raise ConfigParseError(f"无法读取 ARXML：{path}：{error}") from error
    except ET.ParseError as error:
        line, column = getattr(error, "position", (None, None))
        position = f"第 {line} 行、第 {column} 列" if line is not None else "未知位置"
        raise ConfigParseError(
            f"ARXML 格式损坏：{path}（{position}）：{error}"
        ) from error

    element_objects: list[tuple[ET.Element, ConfigObject, str | None]] = []

    for (
        element,
        name,
        kind,
        reference_path,
        xml_path,
        physical_channel,
    ) in _named_elements(root):
        lines = line_map.get(name)
        line = lines.popleft() if lines else 1
        # 即使目录对象不进入索引，也要先消费它的行号，避免同名业务对象错位。
        if kind in {"AR-PACKAGE", "AUTOSAR"}:
            continue

        evidence = ConfigEvidence(
            file=str(path),
            line=line,
            object_type=kind,
            object_name=name,
            xml_path=xml_path,
            reference_path=reference_path,
        )
        config_object = ConfigObject(
            name=name,
            long_name=_long_name(element),
            kind=kind,
            reference_path=reference_path,
            definition_ref=_direct_child_text(element, "DEFINITION-REF"),
            parameter_values=tuple(_object_parameter_values(element)),
            reference_values=tuple(_object_reference_values(element)),
            evidence=evidence,
        )
        index.objects.append(config_object)
        element_objects.append((element, config_object, physical_channel))

    object_by_element = {
        element: config_object for element, config_object, _ in element_objects
    }
    for element, config_object, physical_channel in element_objects:
        if config_object.kind == "CAN-FRAME":
            pdu_refs = _references_for_tags(element, {"PDU-REF", "I-PDU-REF"})
            index.frames.append(
                FrameDefinition(
                    name=config_object.name,
                    reference_path=config_object.reference_path,
                    pdu_references=tuple(dict.fromkeys(pdu_refs)),
                    length_bytes=_optional_int(
                        _first_descendant_text(element, "FRAME-LENGTH")
                    ),
                    evidence=config_object.evidence,
                )
            )
        elif config_object.kind == "CAN-FRAME-TRIGGERING":
            _append_frame_trigger(element, config_object, physical_channel, index)
        elif config_object.kind == "PDU-TRIGGERING":
            _append_pdu_networks(element, physical_channel, index)

        route = _extract_route(element, config_object, object_by_element)
        if route is not None:
            index.routes.append(route)
        gateway = _extract_signal_gateway(element, config_object)
        if gateway is not None:
            index.signal_gateways.append(gateway)

    _append_canif_messages(element_objects, index)


def _append_frame_trigger(
    element: ET.Element,
    config_object: ConfigObject,
    physical_channel: str | None,
    index: ArxmlIndex,
) -> None:
    identifier_text = _first_descendant_text(element, "IDENTIFIER")
    frame_reference = _first_descendant_text(element, "FRAME-REF")
    if identifier_text is None or frame_reference is None:
        return
    try:
        arbitration_id = int(identifier_text.strip(), 0)
    except ValueError as error:
        raise ConfigParseError(
            f"CAN-FRAME-TRIGGERING {config_object.name} 的 IDENTIFIER 无效："
            f"{identifier_text}（{config_object.evidence.file}:"
            f"{config_object.evidence.line}）"
        ) from error
    if not 0 <= arbitration_id <= 0x1FFFFFFF:
        raise ConfigParseError(
            f"CAN-FRAME-TRIGGERING {config_object.name} 的 IDENTIFIER 超出 CAN 范围："
            f"{identifier_text}"
        )
    index.triggers.append(
        FrameTrigger(
            name=config_object.name,
            arbitration_id=arbitration_id,
            frame_reference=frame_reference,
            network=physical_channel,
            evidence=config_object.evidence,
        )
    )


def _append_pdu_networks(
    element: ET.Element,
    physical_channel: str | None,
    index: ArxmlIndex,
) -> None:
    if physical_channel is None:
        return
    for reference in _references_for_tags(element, {"I-PDU-REF", "PDU-REF"}):
        index.pdu_networks.setdefault(reference.casefold(), set()).add(physical_channel)
        index.pdu_networks.setdefault(reference_name(reference).casefold(), set()).add(
            physical_channel
        )


def _append_canif_messages(
    element_objects: list[tuple[ET.Element, ConfigObject, str | None]],
    index: ArxmlIndex,
) -> None:
    """解析 DaVinci ECUC CanIf 配置中的 CAN ID、PDU 与控制器网段关系。"""
    controller_networks: dict[str, str] = {}
    for element, config_object, _ in element_objects:
        signature = normalize_identifier(config_object.definition_ref or "")
        if not signature.endswith("CANIFCTRLCFG"):
            continue
        # LONG-NAME 是配置中的显式名称，不从短名模式猜测网段。
        network = _first_descendant_text(element, "L-4") or config_object.name
        _add_reference_aliases(
            controller_networks, config_object.reference_path, network
        )

    hardware_networks: dict[str, str] = {}
    for element, config_object, _ in element_objects:
        signature = normalize_identifier(config_object.definition_ref or "")
        if signature.endswith("CANIFHRHCFG"):
            marker = "CanIfHrhCanCtrlIdRef"
        elif signature.endswith("CANIFHTHCFG"):
            marker = "CanIfHthCanCtrlIdRef"
        else:
            continue
        controller_reference = _ecuc_reference_value(element, marker)
        network = _lookup_reference_alias(controller_networks, controller_reference)
        if network is not None:
            _add_reference_aliases(
                hardware_networks, config_object.reference_path, network
            )

    # DaVinci 的 Tx PDU 通常不直接引用 HTH，而是先引用 CanIfBufferCfg。
    # 这里沿配置中的显式引用再走一跳，不根据对象名猜测网段。
    buffer_networks: dict[str, str] = {}
    for element, config_object, _ in element_objects:
        signature = normalize_identifier(config_object.definition_ref or "")
        if not signature.endswith("CANIFBUFFERCFG"):
            continue
        hardware_reference = _ecuc_reference_value(element, "CanIfBufferHthRef")
        network = _lookup_reference_alias(hardware_networks, hardware_reference)
        if network is not None:
            _add_reference_aliases(
                buffer_networks, config_object.reference_path, network
            )

    for element, config_object, _ in element_objects:
        signature = normalize_identifier(config_object.definition_ref or "")
        if signature.endswith("CANIFRXPDUCFG"):
            prefix = "CanIfRxPdu"
            hardware_marker = "CanIfRxPduHrhIdRef"
            buffer_marker = None
        elif signature.endswith("CANIFTXPDUCFG"):
            prefix = "CanIfTxPdu"
            hardware_marker = "CanIfTxPduHthIdRef"
            buffer_marker = "CanIfTxPduBufferRef"
        else:
            continue

        can_id_text = _ecuc_parameter_value(element, f"{prefix}CanId")
        can_id_type = _ecuc_parameter_value(element, f"{prefix}CanIdType")
        length_bytes = _optional_int(_ecuc_parameter_value(element, f"{prefix}Dlc"))
        pdu_reference = _ecuc_reference_value(element, f"{prefix}Ref")
        if can_id_text is None or pdu_reference is None:
            continue
        try:
            arbitration_id = int(can_id_text, 0)
        except ValueError as error:
            raise ConfigParseError(
                f"{config_object.name} 的 {prefix}CanId 无效：{can_id_text}"
                f"（{config_object.evidence.file}:{config_object.evidence.line}）"
            ) from error
        if not 0 <= arbitration_id <= 0x1FFFFFFF:
            raise ConfigParseError(
                f"{config_object.name} 的 {prefix}CanId 超出 CAN 范围：{can_id_text}"
            )

        hardware_reference = _ecuc_reference_value(element, hardware_marker)
        network = _lookup_reference_alias(hardware_networks, hardware_reference)
        if network is None and buffer_marker is not None:
            buffer_reference = _ecuc_reference_value(element, buffer_marker)
            network = _lookup_reference_alias(buffer_networks, buffer_reference)
        index.messages.append(
            MessageBinding(
                name=config_object.name,
                arbitration_id=arbitration_id,
                network=network,
                pdu_references=(pdu_reference,),
                evidence=(config_object.evidence,),
                direction="receive" if prefix == "CanIfRxPdu" else "send",
                length_bytes=length_bytes,
                can_id_type=can_id_type,
                is_extended=(
                    "EXTENDED" in can_id_type.upper()
                    if can_id_type is not None
                    else None
                ),
                is_fd=(
                    "FD" in can_id_type.upper() if can_id_type is not None else None
                ),
            )
        )
        if network is not None:
            index.pdu_networks.setdefault(pdu_reference.casefold(), set()).add(network)
            index.pdu_networks.setdefault(
                reference_name(pdu_reference).casefold(), set()
            ).add(network)


def _extract_route(
    element: ET.Element,
    config_object: ConfigObject,
    object_by_element: dict[ET.Element, ConfigObject],
) -> RoutePath | None:
    """从 ECUC PduR 容器或标准 PDU-TO-PDU-MAPPING 提取一条路由。"""
    definition_signature = normalize_identifier(config_object.definition_ref or "")
    if (
        not definition_signature.endswith("PDURROUTINGPATH")
        and config_object.kind != "PDU-TO-PDU-MAPPING"
    ):
        return None

    if config_object.kind == "PDU-TO-PDU-MAPPING":
        source_ref = _first_descendant_text(element, "SOURCE-PDU-REF")
        destination_refs = _descendant_texts(element, "TARGET-PDU-REF")
        source = (
            RouteEndpoint(source_ref, config_object.evidence) if source_ref else None
        )
        destinations = tuple(
            RouteEndpoint(reference, config_object.evidence)
            for reference in destination_refs
        )
        return RoutePath(
            config_object.name, source, destinations, config_object.evidence
        )

    source: RouteEndpoint | None = None
    destinations: list[RouteEndpoint] = []
    # ECUC Route 的端点通常是嵌套 ECUC-CONTAINER-VALUE。根据标准定义引用判断
    # Source/Destination，而不是仅依赖项目自定义 SHORT-NAME。
    for nested in element.iter():
        if nested is element or local_name(nested.tag) != "ECUC-CONTAINER-VALUE":
            continue
        nested_name = _direct_child_text(nested, "SHORT-NAME") or "<unnamed>"
        definition_ref = _direct_child_text(nested, "DEFINITION-REF") or ""
        endpoint_signature = normalize_identifier(f"{definition_ref} {nested_name}")
        nested_object = object_by_element.get(nested)
        endpoint_evidence = (
            nested_object.evidence
            if nested_object is not None
            else config_object.evidence
        )
        if "PDURSRCPDU" in endpoint_signature or "PDURSOURCEPDU" in endpoint_signature:
            reference = _ecuc_reference_value(nested, "PduRSrcPduRef")
            if reference is not None:
                source = RouteEndpoint(reference, endpoint_evidence)
        elif (
            "PDURDESTPDU" in endpoint_signature
            or "PDURDESTINATIONPDU" in endpoint_signature
        ):
            reference = _ecuc_reference_value(nested, "PduRDestPduRef")
            if reference is not None:
                destinations.append(RouteEndpoint(reference, endpoint_evidence))

    return RoutePath(
        name=config_object.name,
        source=source,
        destinations=tuple(destinations),
        evidence=config_object.evidence,
    )


def _extract_signal_gateway(
    element: ET.Element, config_object: ConfigObject
) -> SignalGateway | None:
    """提取 ComGwMapping 的 Source/Destination Signal 明确引用。"""
    definition = normalize_identifier(config_object.definition_ref or "")
    if not definition.endswith("COMGWMAPPING"):
        return None

    source_reference: str | None = None
    destinations: list[str] = []
    for nested in element.iter():
        if nested is element or local_name(nested.tag) != "ECUC-CONTAINER-VALUE":
            continue
        nested_definition = normalize_identifier(
            _direct_child_text(nested, "DEFINITION-REF") or ""
        )
        if nested_definition.endswith("COMGWSOURCE"):
            source_reference = _first_gateway_endpoint_reference(nested)
        elif nested_definition.endswith("COMGWDESTINATION"):
            reference = _first_gateway_endpoint_reference(nested)
            if reference is not None:
                destinations.append(reference)
    return SignalGateway(
        name=config_object.name,
        source_reference=source_reference,
        destination_references=tuple(dict.fromkeys(destinations)),
        evidence=config_object.evidence,
    )


def _build_message_bindings(index: ArxmlIndex) -> None:
    frame_by_reference: dict[str, list[FrameDefinition]] = defaultdict(list)
    for frame in index.frames:
        frame_by_reference[frame.reference_path.casefold()].append(frame)
        frame_by_reference[frame.name.casefold()].append(frame)

    for trigger in index.triggers:
        candidates = frame_by_reference.get(trigger.frame_reference.casefold(), [])
        if not candidates:
            candidates = frame_by_reference.get(
                reference_name(trigger.frame_reference).casefold(), []
            )
        # 同一个 Frame 可能同时通过全路径和短名进入索引，按证据位置去重。
        unique_frames = {
            (frame.evidence.file, frame.evidence.line): frame for frame in candidates
        }
        if not unique_frames:
            index.messages.append(
                MessageBinding(
                    name=reference_name(trigger.frame_reference),
                    arbitration_id=trigger.arbitration_id,
                    network=trigger.network,
                    pdu_references=(),
                    evidence=(trigger.evidence,),
                    direction=None,
                )
            )
            continue
        for frame in unique_frames.values():
            index.messages.append(
                MessageBinding(
                    name=frame.name,
                    arbitration_id=trigger.arbitration_id,
                    network=trigger.network,
                    pdu_references=frame.pdu_references,
                    evidence=(trigger.evidence, frame.evidence),
                    length_bytes=frame.length_bytes,
                    is_fd=(
                        True
                        if frame.length_bytes is not None and frame.length_bytes > 8
                        else None
                    ),
                )
            )


def _short_name_lines(path: Path) -> dict[str, deque[int]]:
    """流式记录 SHORT-NAME 行号，避免同时保留百 MB 级 XML 文本和语法树。"""
    result: dict[str, deque[int]] = defaultdict(deque)
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, start=1):
            for match in _SHORT_NAME_RE.finditer(line):
                result[unescape(match.group(1).strip())].append(line_number)
    return result


def _named_elements(
    root: ET.Element,
) -> Iterator[tuple[ET.Element, str, str, str, str, str | None]]:
    """单次深度遍历生成带 SHORT-NAME 对象的路径与物理网段上下文。"""

    def walk(
        element: ET.Element,
        named_ancestors: tuple[tuple[str, str], ...],
        physical_channel: str | None,
    ) -> Iterator[tuple[ET.Element, str, str, str, str, str | None]]:
        name = _direct_child_text(element, "SHORT-NAME")
        kind = local_name(element.tag)
        current_ancestors = named_ancestors
        current_channel = physical_channel
        if name:
            current_ancestors = (*named_ancestors, (kind, name))
            if kind == "CAN-PHYSICAL-CHANNEL":
                current_channel = name
            if kind not in {"AR-PACKAGE", "AUTOSAR"}:
                reference_path = "/" + "/".join(
                    ancestor_name for _, ancestor_name in current_ancestors
                )
                xml_path = "/" + "/".join(
                    f"{ancestor_kind}[{ancestor_name}]"
                    for ancestor_kind, ancestor_name in current_ancestors
                )
                yield (
                    element,
                    name,
                    kind,
                    reference_path,
                    xml_path,
                    current_channel,
                )
        for child in element:
            yield from walk(child, current_ancestors, current_channel)

    yield from walk(root, (), None)


def _direct_child_text(element: ET.Element, expected_tag: str) -> str | None:
    for child in element:
        if local_name(child.tag) == expected_tag and child.text and child.text.strip():
            return child.text.strip()
    return None


def _long_name(element: ET.Element) -> str | None:
    for child in element:
        if local_name(child.tag) != "LONG-NAME":
            continue
        return _first_descendant_text(child, "L-4")
    return None


def _first_descendant_text(element: ET.Element, expected_tag: str) -> str | None:
    for descendant in element.iter():
        if (
            local_name(descendant.tag) == expected_tag
            and descendant.text
            and descendant.text.strip()
        ):
            return descendant.text.strip()
    return None


def _descendant_texts(element: ET.Element, expected_tag: str) -> list[str]:
    return [
        descendant.text.strip()
        for descendant in element.iter()
        if local_name(descendant.tag) == expected_tag
        and descendant.text
        and descendant.text.strip()
    ]


def _references_for_tags(element: ET.Element, tags: set[str]) -> list[str]:
    return [
        descendant.text.strip()
        for descendant in element.iter()
        if local_name(descendant.tag) in tags
        and descendant.text
        and descendant.text.strip()
    ]


def _own_scope_iter(element: ET.Element) -> Iterator[ET.Element]:
    """遍历当前对象字段，但不进入另一个带 SHORT-NAME 的子对象。"""
    yield element
    for child in element:
        if child is not element and _direct_child_text(child, "SHORT-NAME"):
            continue
        yield from _own_scope_iter(child)


def _object_parameter_values(element: ET.Element) -> list[ConfigValue]:
    result: list[ConfigValue] = []
    for candidate in _own_scope_iter(element):
        if not local_name(candidate.tag).endswith("PARAM-VALUE"):
            continue
        definition = _direct_child_text(candidate, "DEFINITION-REF")
        value = _direct_child_text(candidate, "VALUE")
        if definition is None or value is None:
            continue
        result.append(
            ConfigValue(
                name=reference_name(definition),
                definition_ref=definition,
                value=value,
            )
        )
        if len(result) >= _MAX_PARAMETERS_PER_OBJECT:
            break
    return result


def _object_reference_values(element: ET.Element) -> list[ConfigValue]:
    result: list[ConfigValue] = []
    for candidate in _own_scope_iter(element):
        tag = local_name(candidate.tag)
        if tag == "ECUC-REFERENCE-VALUE":
            definition = _direct_child_text(candidate, "DEFINITION-REF")
            value = _direct_child_text(candidate, "VALUE-REF")
            if definition is not None and value is not None:
                result.append(
                    ConfigValue(
                        name=reference_name(definition),
                        definition_ref=definition,
                        value=value,
                    )
                )
        elif (
            tag.endswith("-REF")
            and tag not in {"DEFINITION-REF", "VALUE-REF"}
            and candidate.text
            and candidate.text.strip()
        ):
            result.append(
                ConfigValue(
                    name=tag,
                    definition_ref=tag,
                    value=candidate.text.strip(),
                )
            )
        if len(result) >= _MAX_REFERENCES_PER_OBJECT:
            break
    return result


def _ecuc_parameter_value(element: ET.Element, definition_marker: str) -> str | None:
    """按 DEFINITION-REF 精确选取 ECUC 参数，避免误取同容器其他 VALUE。"""
    expected = normalize_identifier(definition_marker)
    for candidate in element.iter():
        if not local_name(candidate.tag).endswith("PARAM-VALUE"):
            continue
        definition = _direct_child_text(candidate, "DEFINITION-REF")
        value = _direct_child_text(candidate, "VALUE")
        if definition and value and normalize_identifier(definition).endswith(expected):
            return value
    return None


def _ecuc_reference_value(element: ET.Element, definition_marker: str) -> str | None:
    """按 DEFINITION-REF 精确选取 ECUC 引用，不依赖 XML 中引用排列顺序。"""
    expected = normalize_identifier(definition_marker)
    for candidate in element.iter():
        if local_name(candidate.tag) != "ECUC-REFERENCE-VALUE":
            continue
        definition = _direct_child_text(candidate, "DEFINITION-REF")
        value = _direct_child_text(candidate, "VALUE-REF")
        if definition and value and normalize_identifier(definition).endswith(expected):
            return value
    return None


def _add_reference_aliases(mapping: dict[str, str], reference: str, value: str) -> None:
    mapping[reference.casefold()] = value
    mapping[reference_name(reference).casefold()] = value


def _lookup_reference_alias(
    mapping: dict[str, str], reference: str | None
) -> str | None:
    if reference is None:
        return None
    return mapping.get(reference.casefold()) or mapping.get(
        reference_name(reference).casefold()
    )


def _first_gateway_endpoint_reference(element: ET.Element) -> str | None:
    for candidate in element.iter():
        if local_name(candidate.tag) != "ECUC-REFERENCE-VALUE":
            continue
        definition = _direct_child_text(candidate, "DEFINITION-REF") or ""
        value = _direct_child_text(candidate, "VALUE-REF")
        signature = normalize_identifier(definition)
        if value and "COMGW" in signature and signature.endswith("REF"):
            return value
    return None


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None
