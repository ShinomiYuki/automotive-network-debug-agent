"""
文件用途：
- 在已建立的 ARXML 索引上解析 Message → PDU → Routing Path → Destination。
- 只做引用相等关系连接，不根据 ECU、网段命名或业务预期判断配置正确性。
"""

from pathlib import Path

from anda.common.errors import (
    ConfigConflictError,
    ConfigInputError,
    ConfigNotFoundError,
)
from anda.config.arxml import reference_name
from anda.config.models import ArxmlIndex, ConfigObject, MessageBinding, RoutePath
from anda.config.source import SourceIndex

MAX_ROUTE_PATHS = 50
MAX_ROUTE_DESTINATIONS = 100


def trace_message_route(
    index: ArxmlIndex,
    source_index: SourceIndex,
    arbitration_id: int | None,
    message_name: str | None,
    source_network: str | None,
    destination_network: str | None,
) -> dict:
    """解析唯一 Message 入口，并返回可审查的路由链。"""
    message = resolve_message(index, arbitration_id, message_name, source_network)
    routes = _matching_routes(index.routes, message.pdu_references)
    route_results = []
    all_destinations = []
    generated_symbols = [message.name]

    for route in routes[:MAX_ROUTE_PATHS]:
        generated_symbols.append(route.name)
        selected_is_source = bool(
            route.source
            and any(
                _same_reference(route.source.pdu_reference, reference)
                for reference in message.pdu_references
            )
        )
        selected_is_destination = any(
            _same_reference(endpoint.pdu_reference, reference)
            for endpoint in route.destinations
            for reference in message.pdu_references
        )
        source_result = (
            _endpoint_dict(index, route.source.pdu_reference, route.source.evidence)
            if route.source
            else None
        )
        if source_result is not None:
            generated_symbols.append(source_result["pdu"])
        destinations = []
        for endpoint in route.destinations:
            destination = _endpoint_dict(
                index, endpoint.pdu_reference, endpoint.evidence
            )
            generated_symbols.append(destination["pdu"])
            destination["matches_requested_network"] = (
                None
                if destination_network is None
                else _contains_casefold(destination["networks"], destination_network)
            )
            destinations.append(destination)
            all_destinations.append(destination)
            if len(all_destinations) >= MAX_ROUTE_DESTINATIONS:
                break
        route_results.append(
            {
                "name": route.name,
                "selected_pdu_role": (
                    "both"
                    if selected_is_source and selected_is_destination
                    else "source"
                    if selected_is_source
                    else "destination"
                ),
                "source": source_result,
                "destinations": destinations,
                "destination_missing": not route.destinations,
                "evidence": route.evidence.as_dict(),
            }
        )
        if len(all_destinations) >= MAX_ROUTE_DESTINATIONS:
            break

    requested_destination_found = (
        None
        if destination_network is None
        else any(
            _contains_casefold(destination["networks"], destination_network)
            for destination in all_destinations
        )
    )
    return {
        "message": {
            "name": message.name,
            "arbitration_id": message.arbitration_id,
            "arbitration_id_hex": f"0x{message.arbitration_id:X}",
            "network": message.network,
            "pdu_references": list(message.pdu_references),
            "direction": message.direction,
            "length_bytes": message.length_bytes,
            "can_id_type": message.can_id_type,
            "is_extended": message.is_extended,
            "is_fd": message.is_fd,
            "evidence": [item.as_dict() for item in message.evidence],
        },
        "filters": {
            "source_network": source_network,
            "destination_network": destination_network,
        },
        "route_found": bool(routes),
        "routing_path_count": len(routes),
        "routing_paths_returned": len(route_results),
        "routing_paths": route_results,
        "requested_destination_found": requested_destination_found,
        "result_truncated": (
            len(routes) > len(route_results)
            or sum(len(route.destinations) for route in routes) > len(all_destinations)
        ),
        "generated_locations": source_index.locations_for_symbols(
            list(dict.fromkeys(generated_symbols)), limit=20
        ),
    }


def resolve_message(
    index: ArxmlIndex,
    arbitration_id: int | None,
    message_name: str | None,
    source_network: str | None,
) -> MessageBinding:
    if arbitration_id is None and not (message_name and message_name.strip()):
        raise ConfigInputError("arbitration_id 和 message_name 至少提供一个")
    if arbitration_id is not None and not 0 <= arbitration_id <= 0x1FFFFFFF:
        raise ConfigInputError("arbitration_id 必须在 0 到 0x1FFFFFFF 之间")

    candidates = index.messages
    if arbitration_id is not None:
        candidates = [
            item for item in candidates if item.arbitration_id == arbitration_id
        ]
    if message_name and message_name.strip():
        normalized_name = message_name.strip().casefold()
        candidates = [
            item for item in candidates if item.name.casefold() == normalized_name
        ]
    if source_network and source_network.strip():
        normalized_network = source_network.strip().casefold()
        candidates = [
            item
            for item in candidates
            if item.network is not None
            and item.network.casefold() == normalized_network
        ]

    # 同一触发关系可能由重复包含的 ARXML 产生完全相同记录；先按关键事实去重，
    # 但保留同 CAN ID 不同网段/Message 的真实冲突。
    unique = {
        (
            item.name.casefold(),
            item.arbitration_id,
            (item.network or "").casefold(),
            tuple(reference.casefold() for reference in item.pdu_references),
        ): item
        for item in candidates
    }
    candidates = list(unique.values())
    if not candidates:
        selector = (
            f"CAN ID 0x{arbitration_id:X}"
            if arbitration_id is not None
            else f"Message {message_name}"
        )
        if source_network:
            selector += f"、源网段 {source_network}"
        raise ConfigNotFoundError(f"工程配置中未找到 {selector}")
    if len(candidates) > 1:
        summary = ", ".join(
            f"{item.name}@{item.network or '未知网段'}(0x{item.arbitration_id:X})"
            f" PDU={','.join(reference_name(value) for value in item.pdu_references) or '未知'}"
            f" [{Path(item.evidence[0].file).name}:{item.evidence[0].line}]"
            for item in sorted(
                candidates,
                key=lambda candidate: (candidate.name, candidate.network or ""),
            )[:10]
        )
        raise ConfigConflictError(
            f"Message 查询存在多个候选，请补充名称或源网段：{summary}"
        )
    return candidates[0]


def _matching_routes(
    routes: list[RoutePath], pdu_references: tuple[str, ...]
) -> list[RoutePath]:
    return [
        route
        for route in routes
        if (
            route.source is not None
            and any(
                _same_reference(route.source.pdu_reference, pdu_reference)
                for pdu_reference in pdu_references
            )
        )
        or any(
            _same_reference(endpoint.pdu_reference, pdu_reference)
            for endpoint in route.destinations
            for pdu_reference in pdu_references
        )
    ]


def _same_reference(first: str, second: str) -> bool:
    if first.casefold() == second.casefold():
        return True
    # 两边都是完整 AUTOSAR 引用时，不允许仅凭相同 SHORT-NAME 跨包关联。
    if first.startswith("/") and second.startswith("/"):
        return False
    return reference_name(first).casefold() == reference_name(second).casefold()


def _endpoint_dict(index: ArxmlIndex, reference: str, evidence) -> dict:
    objects = _objects_for_reference(index, reference)
    networks = _networks_for_reference(index, reference)
    return {
        "pdu": reference_name(reference),
        "reference": reference,
        "networks": networks,
        "evidence": [
            evidence.as_dict(),
            *[item.evidence.as_dict() for item in objects[:5]],
        ],
    }


def _objects_for_reference(index: ArxmlIndex, reference: str) -> list[ConfigObject]:
    normalized = reference.casefold()
    short_name = reference_name(reference).casefold()
    exact = [
        item for item in index.objects if item.reference_path.casefold() == normalized
    ]
    if exact:
        return exact
    return [item for item in index.objects if item.name.casefold() == short_name]


def _networks_for_reference(index: ArxmlIndex, reference: str) -> list[str]:
    result = set(index.pdu_networks.get(reference.casefold(), ()))
    if not result:
        result.update(index.pdu_networks.get(reference_name(reference).casefold(), ()))
    return sorted(result, key=str.casefold)


def _contains_casefold(values: list[str], expected: str) -> bool:
    normalized = expected.strip().casefold()
    return any(value.casefold() == normalized for value in values)
