"""
文件用途：
- 在统一 ARXML 对象索引与源码符号索引上提供通信属性、Signal Gateway 和 I-PDU Group 查询。
- 返回配置中直接存在的参数、引用和源码位置，不模拟 BswM/ComM/Com 运行时状态。

设计原则：
- Tool 面向真实调查动作，不为每个 AUTOSAR 参数创建独立接口。
- 完整 AUTOSAR 引用优先；两个不同完整路径即使短名相同也不会自动关联。
- 所有列表都有固定上限，并明确返回总数与截断状态。
"""

from __future__ import annotations

from collections.abc import Iterable

from anda.common.errors import (
    ConfigConflictError,
    ConfigInputError,
    ConfigNotFoundError,
)
from anda.config.arxml import normalize_identifier, reference_name
from anda.config.models import ArxmlIndex, ConfigObject, ConfigValue, MessageBinding
from anda.config.routing import resolve_message
from anda.config.source import SourceIndex

MAX_COMMUNICATION_OBJECTS = 20
MAX_SIGNALS_PER_IPDU = 50
MAX_SIGNAL_GATEWAYS = 20
MAX_GROUP_MEMBERS = 100
MAX_GROUP_CONTROLS = 50


def inspect_communication(
    index: ArxmlIndex,
    source_index: SourceIndex,
    arbitration_id: int | None,
    message_name: str | None,
    pdu_name: str | None,
    network: str | None,
) -> dict:
    """查看 Message/PDU 在 CanIf、Com 和源码中的确定性通信属性。"""
    has_message_selector = arbitration_id is not None or bool(
        message_name and message_name.strip()
    )
    has_pdu_selector = bool(pdu_name and pdu_name.strip())
    if has_message_selector == has_pdu_selector:
        raise ConfigInputError(
            "请提供 CAN ID/Message 或 PDU 其中一种查询入口，不要同时提供"
        )

    message: MessageBinding | None = None
    if has_message_selector:
        message = resolve_message(index, arbitration_id, message_name, network)
        selected_pdu_references = list(dict.fromkeys(message.pdu_references))
    else:
        selected_pdu_references = _resolve_pdu_references(index, pdu_name or "")

    linked_pdu_references = _expand_pdu_references(index, selected_pdu_references)
    communication_objects = [
        _communication_object(index, source_index, reference, relation, route_names)
        for reference, relation, route_names in linked_pdu_references[
            :MAX_COMMUNICATION_OBJECTS
        ]
    ]
    return {
        "message": _message_dict(message) if message is not None else None,
        "query": {
            "arbitration_id": arbitration_id,
            "message_name": message_name,
            "pdu_name": pdu_name,
            "network": network,
        },
        "selected_pdu_references": selected_pdu_references,
        "communication_object_count": len(linked_pdu_references),
        "communication_objects_returned": len(communication_objects),
        "communication_objects": communication_objects,
        "result_truncated": len(linked_pdu_references) > len(communication_objects),
    }


def trace_signal_gateway(
    index: ArxmlIndex,
    source_index: SourceIndex,
    signal_name: str,
    limit: int = 20,
) -> dict:
    """追踪 Com Signal、所在 I-PDU 与 ComGwMapping Source/Destination。"""
    query = signal_name.strip()
    if not query:
        raise ConfigInputError("signal_name 不能为空")
    if limit < 1:
        raise ConfigInputError("limit 必须大于等于 1")
    applied_limit = min(limit, MAX_SIGNAL_GATEWAYS)
    normalized = query.casefold()
    signal_objects = [
        item
        for item in index.objects
        if _definition_ends(item, "ComSignal")
        and (
            normalized in item.name.casefold()
            or bool(item.long_name and normalized in item.long_name.casefold())
        )
    ]
    signal_references = {item.reference_path.casefold() for item in signal_objects}
    gateways = [
        gateway
        for gateway in index.signal_gateways
        if normalized in gateway.name.casefold()
        or _reference_matches_query(gateway.source_reference, normalized)
        or any(
            _reference_matches_query(reference, normalized)
            for reference in gateway.destination_references
        )
        or bool(
            signal_references
            & {
                reference.casefold()
                for reference in (
                    gateway.source_reference,
                    *gateway.destination_references,
                )
                if reference
            }
        )
    ]
    if not signal_objects and not gateways:
        raise ConfigNotFoundError(f"工程配置中未找到 Signal：{query}")

    returned_gateways = gateways[:applied_limit]
    gateway_results = []
    for gateway in returned_gateways:
        source = _signal_endpoint(index, source_index, gateway.source_reference)
        destinations = [
            _signal_endpoint(index, source_index, reference)
            for reference in gateway.destination_references
        ]
        gateway_results.append(
            {
                "mapping": gateway.name,
                "source": source,
                "destinations": destinations,
                "destination_missing": not gateway.destination_references,
                "evidence": gateway.evidence.as_dict(),
                "source_locations": source_index.locations_for_symbols(
                    [gateway.name], limit=10
                ),
            }
        )

    return {
        "signal_name": query,
        "signal_definitions": [
            _object_summary(item) for item in signal_objects[:MAX_SIGNALS_PER_IPDU]
        ],
        "gateway_found": bool(gateways),
        "gateway_count": len(gateways),
        "gateways_returned": len(gateway_results),
        "gateways": gateway_results,
        "limit_applied": applied_limit,
        "result_truncated": len(gateways) > len(gateway_results),
    }


def inspect_ipdu_group(
    index: ArxmlIndex,
    source_index: SourceIndex,
    group_name: str,
) -> dict:
    """查看 I-PDU Group 成员及 BswM/ComM 对该 Group 的直接引用。"""
    query = group_name.strip()
    if not query:
        raise ConfigInputError("group_name 不能为空")
    groups = [
        item
        for item in index.objects
        if _definition_ends(item, "ComIPduGroup") and _object_name_matches(item, query)
    ]
    if not groups:
        raise ConfigNotFoundError(f"工程配置中未找到 I-PDU Group：{query}")
    if len(groups) > 1:
        candidates = ", ".join(item.reference_path for item in groups[:10])
        raise ConfigConflictError(
            f"I-PDU Group 存在多个候选，请使用完整引用路径：{candidates}"
        )
    group = groups[0]
    members = [
        item
        for item in index.objects
        if _definition_ends(item, "ComIPdu")
        and any(
            value.name.casefold() == "comipdugroupref"
            and _same_reference(value.value, group.reference_path)
            for value in item.reference_values
        )
    ]
    controls = [
        item
        for item in index.objects
        if _is_bswm_or_comm(item)
        and any(
            _same_reference(value.value, group.reference_path)
            for value in item.reference_values
        )
    ]
    member_results = [_ipdu_summary(item) for item in members[:MAX_GROUP_MEMBERS]]
    control_results = [
        {
            **_object_summary(item),
            "module": _module_name(item),
            "relation": "direct_group_reference",
        }
        for item in controls[:MAX_GROUP_CONTROLS]
    ]
    symbol_names = [
        group.name,
        *(item.name for item in members[:MAX_GROUP_MEMBERS]),
        *(item.name for item in controls[:MAX_GROUP_CONTROLS]),
    ]
    return {
        "group": _object_summary(group),
        "member_count": len(members),
        "members_returned": len(member_results),
        "members": member_results,
        "direct_control_reference_count": len(controls),
        "direct_control_references_returned": len(control_results),
        "direct_control_references": control_results,
        "source_locations": source_index.locations_for_symbols(
            list(dict.fromkeys(symbol_names)), limit=20
        ),
        "result_truncated": (
            len(members) > len(member_results) or len(controls) > len(control_results)
        ),
        "runtime_state_inferred": False,
    }


def _communication_object(
    index: ArxmlIndex,
    source_index: SourceIndex,
    pdu_reference: str,
    relation: str,
    route_names: list[str],
) -> dict:
    canif_objects = [
        item
        for item in index.objects
        if (
            _definition_ends(item, "CanIfRxPduCfg")
            or _definition_ends(item, "CanIfTxPduCfg")
        )
        and any(
            value.name.casefold() in {"canifrxpduref", "caniftxpduref"}
            and _same_reference(value.value, pdu_reference)
            for value in item.reference_values
        )
    ]
    com_ipdus = [
        item
        for item in index.objects
        if _definition_ends(item, "ComIPdu")
        and any(
            value.name.casefold() == "compduidref"
            and _same_reference(value.value, pdu_reference)
            for value in item.reference_values
        )
    ]
    networks = sorted(
        {
            message.network
            for message in index.messages
            if message.network
            and any(
                _same_reference(reference, pdu_reference)
                for reference in message.pdu_references
            )
        },
        key=str.casefold,
    )
    names = [
        reference_name(pdu_reference),
        *(item.name for item in canif_objects),
        *(item.name for item in com_ipdus),
    ]
    return {
        "pdu": reference_name(pdu_reference),
        "pdu_reference": pdu_reference,
        "relation_to_query": relation,
        "routing_paths": route_names,
        "networks": networks,
        "canif": [_canif_summary(item, index) for item in canif_objects[:20]],
        "com_ipdus": [_com_ipdu_summary(item, index) for item in com_ipdus[:20]],
        "source_locations": source_index.locations_for_symbols(
            list(dict.fromkeys(names)), limit=20
        ),
        "result_truncated": len(canif_objects) > 20 or len(com_ipdus) > 20,
    }


def _expand_pdu_references(
    index: ArxmlIndex, selected_references: list[str]
) -> list[tuple[str, str, list[str]]]:
    """沿一跳 PduR 路由补充 CanIf 与 Com 之间的相关 PDU，不递归猜测。"""
    result: dict[str, tuple[str, str, list[str]]] = {
        reference.casefold(): (reference, "selected", [])
        for reference in selected_references
    }
    for route in index.routes:
        if route.source and any(
            _same_reference(route.source.pdu_reference, selected)
            for selected in selected_references
        ):
            for endpoint in route.destinations:
                key = endpoint.pdu_reference.casefold()
                existing = result.get(key)
                if existing is None:
                    result[key] = (
                        endpoint.pdu_reference,
                        "routing_destination",
                        [route.name],
                    )
                else:
                    existing[2].append(route.name)
        if (
            any(
                _same_reference(endpoint.pdu_reference, selected)
                for endpoint in route.destinations
                for selected in selected_references
            )
            and route.source
        ):
            key = route.source.pdu_reference.casefold()
            existing = result.get(key)
            if existing is None:
                result[key] = (
                    route.source.pdu_reference,
                    "routing_source",
                    [route.name],
                )
            else:
                existing[2].append(route.name)
    return list(result.values())


def _canif_summary(item: ConfigObject, index: ArxmlIndex) -> dict:
    is_receive = _definition_ends(item, "CanIfRxPduCfg")
    prefix = "CanIfRxPdu" if is_receive else "CanIfTxPdu"
    can_id = _first_parameter(item, f"{prefix}CanId")
    can_id_type = _first_parameter(item, f"{prefix}CanIdType")
    length = _first_parameter(item, f"{prefix}Dlc")
    pdu_reference = _first_reference(item, f"{prefix}Ref")
    networks = sorted(
        {
            message.network
            for message in index.messages
            if message.network
            and pdu_reference
            and any(
                _same_reference(reference, pdu_reference)
                for reference in message.pdu_references
            )
        },
        key=str.casefold,
    )
    return {
        "object": item.name,
        "direction": "receive" if is_receive else "send",
        "arbitration_id": _parse_int(can_id),
        "arbitration_id_hex": (
            f"0x{_parse_int(can_id):X}" if _parse_int(can_id) is not None else None
        ),
        "length_bytes": _parse_int(length),
        "can_id_type": can_id_type,
        "is_extended": (
            "EXTENDED" in can_id_type.upper() if can_id_type is not None else None
        ),
        "is_fd": "FD" in can_id_type.upper() if can_id_type is not None else None,
        "networks": networks,
        "parameters": _value_dicts(item.parameter_values),
        "evidence": item.evidence.as_dict(),
    }


def _com_ipdu_summary(item: ConfigObject, index: ArxmlIndex) -> dict:
    signal_references = [
        value.value
        for value in item.reference_values
        if value.name.casefold() == "comipdusignalref"
    ]
    signals = []
    for reference in signal_references[:MAX_SIGNALS_PER_IPDU]:
        signal = _object_by_reference(index, reference)
        signals.append(
            {
                "signal": reference_name(reference),
                "reference": reference,
                "properties": (
                    _value_dicts(signal.parameter_values) if signal is not None else []
                ),
                "evidence": signal.evidence.as_dict() if signal is not None else None,
            }
        )
    descendants = [
        candidate
        for candidate in index.objects
        if candidate.reference_path.startswith(f"{item.reference_path}/")
        and candidate.parameter_values
        and (
            "COMTX" in normalize_identifier(candidate.definition_ref or "")
            or "COMRX" in normalize_identifier(candidate.definition_ref or "")
        )
    ]
    return {
        **_ipdu_summary(item),
        "signal_count": len(signal_references),
        "signals_returned": len(signals),
        "signals": signals,
        "timing_and_mode_configs": [
            _object_summary(candidate) for candidate in descendants[:20]
        ],
        "result_truncated": (
            len(signal_references) > len(signals) or len(descendants) > 20
        ),
    }


def _signal_endpoint(
    index: ArxmlIndex, source_index: SourceIndex, reference: str | None
) -> dict | None:
    if reference is None:
        return None
    signal = _object_by_reference(index, reference)
    ipdus = [
        item
        for item in index.objects
        if _definition_ends(item, "ComIPdu")
        and any(
            value.name.casefold() == "comipdusignalref"
            and _same_reference(value.value, reference)
            for value in item.reference_values
        )
    ]
    names = [reference_name(reference), *(item.name for item in ipdus[:20])]
    return {
        "signal": reference_name(reference),
        "reference": reference,
        "properties": _value_dicts(signal.parameter_values) if signal else [],
        "evidence": signal.evidence.as_dict() if signal else None,
        "ipdus": [_ipdu_summary(item) for item in ipdus[:20]],
        "source_locations": source_index.locations_for_symbols(names, limit=10),
    }


def _ipdu_summary(item: ConfigObject) -> dict:
    return {
        "name": item.name,
        "long_name": item.long_name,
        "direction": _first_parameter(item, "ComIPduDirection"),
        "pdu_reference": _first_reference(item, "ComPduIdRef"),
        "group_references": [
            value.value
            for value in item.reference_values
            if value.name.casefold() == "comipdugroupref"
        ],
        "parameters": _value_dicts(item.parameter_values),
        "evidence": item.evidence.as_dict(),
    }


def _resolve_pdu_references(index: ArxmlIndex, query: str) -> list[str]:
    normalized = query.strip()
    if not normalized:
        raise ConfigInputError("pdu_name 不能为空")
    references: set[str] = set()
    for message in index.messages:
        references.update(
            reference
            for reference in message.pdu_references
            if _reference_matches_input(reference, normalized)
        )
    for route in index.routes:
        if route.source and _reference_matches_input(
            route.source.pdu_reference, normalized
        ):
            references.add(route.source.pdu_reference)
        references.update(
            endpoint.pdu_reference
            for endpoint in route.destinations
            if _reference_matches_input(endpoint.pdu_reference, normalized)
        )
    for item in index.objects:
        references.update(
            value.value
            for value in item.reference_values
            if value.name.casefold()
            in {"canifrxpduref", "caniftxpduref", "compduidref"}
            and _reference_matches_input(value.value, normalized)
        )
    if not references:
        raise ConfigNotFoundError(f"工程配置中未找到 PDU：{normalized}")
    if len(references) > 1:
        candidates = ", ".join(sorted(references)[:10])
        raise ConfigConflictError(f"PDU 存在多个完整引用，请补充完整路径：{candidates}")
    return list(references)


def _message_dict(message: MessageBinding) -> dict:
    return {
        "name": message.name,
        "arbitration_id": message.arbitration_id,
        "arbitration_id_hex": f"0x{message.arbitration_id:X}",
        "network": message.network,
        "direction": message.direction,
        "length_bytes": message.length_bytes,
        "can_id_type": message.can_id_type,
        "is_extended": message.is_extended,
        "is_fd": message.is_fd,
        "pdu_references": list(message.pdu_references),
        "evidence": [item.as_dict() for item in message.evidence],
    }


def _object_summary(item: ConfigObject) -> dict:
    return {
        "name": item.name,
        "long_name": item.long_name,
        "object_type": item.kind,
        "definition_ref": item.definition_ref,
        "reference_path": item.reference_path,
        "parameters": _value_dicts(item.parameter_values),
        "references": _value_dicts(item.reference_values),
        "evidence": item.evidence.as_dict(),
    }


def _value_dicts(values: Iterable[ConfigValue]) -> list[dict]:
    return [value.as_dict() for value in values]


def _first_parameter(item: ConfigObject, name: str) -> str | None:
    normalized = name.casefold()
    return next(
        (
            value.value
            for value in item.parameter_values
            if value.name.casefold() == normalized
        ),
        None,
    )


def _first_reference(item: ConfigObject, name: str) -> str | None:
    normalized = name.casefold()
    return next(
        (
            value.value
            for value in item.reference_values
            if value.name.casefold() == normalized
        ),
        None,
    )


def _object_by_reference(index: ArxmlIndex, reference: str) -> ConfigObject | None:
    normalized = reference.casefold()
    return next(
        (
            item
            for item in index.objects
            if item.reference_path.casefold() == normalized
        ),
        None,
    )


def _definition_ends(item: ConfigObject, suffix: str) -> bool:
    return normalize_identifier(item.definition_ref or "").endswith(
        normalize_identifier(suffix)
    )


def _object_name_matches(item: ConfigObject, query: str) -> bool:
    normalized = query.casefold()
    if query.startswith("/"):
        return item.reference_path.casefold() == normalized
    return item.name.casefold() == normalized or bool(
        item.long_name and item.long_name.casefold() == normalized
    )


def _same_reference(first: str, second: str) -> bool:
    if first.casefold() == second.casefold():
        return True
    if first.startswith("/") and second.startswith("/"):
        return False
    return reference_name(first).casefold() == reference_name(second).casefold()


def _reference_matches_input(reference: str, query: str) -> bool:
    if query.startswith("/"):
        return reference.casefold() == query.casefold()
    return reference_name(reference).casefold() == query.casefold()


def _reference_matches_query(reference: str | None, query: str) -> bool:
    return bool(reference and query in reference_name(reference).casefold())


def _is_bswm_or_comm(item: ConfigObject) -> bool:
    definition = (item.definition_ref or "").casefold()
    return "/bswm/" in definition or "/comm/" in definition


def _module_name(item: ConfigObject) -> str:
    definition = (item.definition_ref or "").casefold()
    if "/bswm/" in definition:
        return "BswM"
    if "/comm/" in definition:
        return "ComM"
    return "unknown"


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None
