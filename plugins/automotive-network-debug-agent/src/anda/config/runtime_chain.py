"""
文件用途：
- 将 ARXML 配置对象与一次性源码索引组织成可审计的 AUTOSAR 跨层调用链。
- 输出生产者/消费者、处理模式、缓冲、任务、callout、I-PDU Group 与调度控制事实。

该模块只组织已加载配置和源码中的确定性命中；缺失字段返回 UNKNOWN，不模拟运行时顺序。
"""

from __future__ import annotations

import re
from collections import defaultdict

from anda.common.errors import ConfigInputError, ConfigNotFoundError
from anda.config.arxml import normalize_identifier, reference_name
from anda.config.inspection import trace_signal_gateway
from anda.config.models import ArxmlIndex, ConfigObject
from anda.config.source import SourceIndex

_C_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LAYER_ORDER = [
    "CanIf",
    "PduR",
    "Com",
    "LinIf",
    "OS/RTE",
    "BswM",
    "ComM",
    "CDD/Application",
]
_SEMANTIC_SEARCHES = (
    ("MainFunction", ["Com", "LinIf", "OS/RTE"]),
    ("TriggerTransmit", ["PduR", "LinIf", "Com"]),
    ("Timeout", ["Com"]),
    ("Callout", ["Com", "PduR", "CDD/Application"]),
    ("IpduGroup", ["Com", "BswM", "ComM"]),
    ("Schedule", ["LinIf", "BswM", "OS/RTE"]),
    ("Priority", ["OS/RTE"]),
)


def trace_autosar_runtime_chain(
    index: ArxmlIndex,
    source_index: SourceIndex,
    signal_name: str,
    additional_identifiers: list[str] | None = None,
) -> dict:
    """返回跨模块静态证据图谱；不把静态引用描述成已发生的运行时调用。"""
    signal = signal_name.strip()
    if not signal:
        raise ConfigInputError("signal_name 不能为空")
    requested_identifiers = [signal, *(additional_identifiers or [])]
    if len(requested_identifiers) > 50:
        raise ConfigInputError("signal_name 与 additional_identifiers 合计不能超过 50 个")

    try:
        gateway = trace_signal_gateway(index, source_index, signal, limit=20)
    except ConfigNotFoundError:
        gateway = {
            "signal_name": signal,
            "signal_definitions": [],
            "gateway_found": False,
            "gateway_count": 0,
            "gateways": [],
        }

    identifiers = _collect_c_identifiers(requested_identifiers, gateway)
    semantic_symbols = []
    for query, modules in _SEMANTIC_SEARCHES:
        result = source_index.search_symbols(query, limit=10, modules=modules)
        semantic_symbols.extend(item["symbol"] for item in result["matches"])
    identifiers = list(dict.fromkeys([*identifiers, *semantic_symbols]))[:50]

    source_evidence = (
        source_index.search_occurrences(
            identifiers,
            modules=_LAYER_ORDER,
            include_generated=True,
            include_source=True,
            limit_per_identifier=10,
            max_chars_per_match=1000,
        )
        if identifiers
        else _empty_source_evidence(source_index)
    )
    source_matches = [
        match
        for result in source_evidence["results"]
        for match in result["matches"]
    ]

    relevant_objects = _relevant_config_objects(index, requested_identifiers, gateway)
    config_facts = _config_facts(relevant_objects)
    source_by_module: dict[str, list[dict]] = defaultdict(list)
    for match in source_matches:
        source_by_module[match["module"]].append(match)
    config_by_module: dict[str, list[dict]] = defaultdict(list)
    for fact in config_facts:
        config_by_module[fact["module"]].append(fact)

    steps = []
    for layer in _LAYER_ORDER:
        layer_source = source_by_module.get(layer, [])[:20]
        layer_config = config_by_module.get(layer, [])[:20]
        if not layer_source and not layer_config:
            continue
        steps.append(
            {
                "layer": layer,
                "static_relation_only": True,
                "config_facts": layer_config,
                "source_facts": layer_source,
                "evidence_count": len(layer_config) + len(layer_source),
            }
        )

    producer_consumer = _producer_consumer(gateway)
    buffering = _semantic_attribute(config_facts, source_matches, ("BUFFERED", "UNBUFFERED"))
    processing = _semantic_attribute(config_facts, source_matches, ("DEFERRED", "IMMEDIATE"))
    callouts = _matching_semantics(config_facts, source_matches, ("CALLOUT", "HOOK"))
    tasks = _matching_semantics(
        config_facts,
        source_matches,
        ("TASK", "MAINFUNCTION", "PRIORITY", "PERIOD", "TICK"),
    )
    ipdu_groups = _gateway_group_references(gateway) + _matching_semantics(
        config_facts, source_matches, ("IPDUGROUP", "PDU_GROUP")
    )
    schedule_controllers = _matching_semantics(
        config_facts, source_matches, ("SCHEDULE", "LINSM", "BSWM")
    )

    cdd_evidence = (
        source_index.search_occurrences(
            _collect_c_identifiers(requested_identifiers, gateway),
            modules=["CDD/Application"],
            include_generated=False,
            include_source=True,
            limit_per_identifier=10,
            max_chars_per_match=1000,
        )
        if _collect_c_identifiers(requested_identifiers, gateway)
        else _empty_source_evidence(source_index)
    )
    cdd_hit_count = sum(
        item["total_count"] for item in cdd_evidence.get("results", [])
    )
    counter_evidence = [
        {
            "candidate": "CDD_or_application_direct_symbol_reference",
            "result": "FOUND" if cdd_hit_count else "NOT_FOUND_IN_SCOPE",
            "search": cdd_evidence,
            "boundary": (
                "未命中只适用于已索引的非 generated C/C++ 文件，不能证明不存在间接调用。"
            ),
        },
        {
            "candidate": "configured_callout_insertion",
            "result": "FOUND" if callouts else "NOT_FOUND_IN_LOADED_CONFIG_AND_INDEX",
            "evidence": callouts,
            "boundary": "静态未命中不证明运行时没有函数指针或外部集成代码。",
        },
        {
            "candidate": "PduR_or_Com_buffer_queue",
            "result": buffering["value"],
            "evidence": buffering["evidence"],
            "boundary": "仅报告配置/源码中显式 BUFFERED 或 UNBUFFERED 标记。",
        },
    ]

    unknowns = []
    if buffering["value"] == "UNKNOWN":
        unknowns.append("buffered/unbuffered 未在当前相关配置或源码证据中明确出现")
    if processing["value"] == "UNKNOWN":
        unknowns.append("deferred/immediate 未在当前相关配置或源码证据中明确出现")
    if not tasks:
        unknowns.append("任务周期和优先级未被当前索引中的显式符号/参数证明")
    if not callouts:
        unknowns.append("callout/CDD 插入点未被当前加载范围直接证明")
    if not schedule_controllers:
        unknowns.append("LIN schedule 控制者未被当前加载范围直接证明")

    return {
        "signal_name": signal,
        "query_plan": source_index.plan_search("can_to_lin_timeout", identifiers or [signal]),
        "producer_consumer": producer_consumer,
        "buffering": buffering,
        "signal_processing": processing,
        "tasks": tasks[:50],
        "callout_and_cdd_insertion_points": callouts[:50],
        "ipdu_groups": _deduplicate_dicts(ipdu_groups)[:50],
        "schedule_controllers": schedule_controllers[:50],
        "steps": steps,
        "counter_evidence": counter_evidence,
        "unknowns": unknowns,
        "runtime_order_inferred": False,
        "root_cause_inferred": False,
        "evidence_rule": "每项事实必须包含已加载 ARXML 或已索引源码的文件与行号。",
    }


def _collect_c_identifiers(values: list[str], payload: object) -> list[str]:
    result = []
    for value in values:
        result.extend(_C_IDENTIFIER.findall(value))

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, value in item.items():
                if key in {
                    "signal",
                    "mapping",
                    "name",
                    "pdu_reference",
                    "reference",
                    "reference_path",
                } and isinstance(value, str):
                    result.extend(_C_IDENTIFIER.findall(reference_name(value)))
                else:
                    visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)

    visit(payload)
    return list(dict.fromkeys(result))[:50]


def _relevant_config_objects(
    index: ArxmlIndex, identifiers: list[str], gateway: dict
) -> list[ConfigObject]:
    terms = {value.casefold() for value in identifiers if value.strip()}
    terms.update(value.casefold() for value in _collect_c_identifiers([], gateway))
    result = []
    for item in index.objects:
        haystacks = [
            item.name,
            item.reference_path,
            item.definition_ref or "",
            *(value.value for value in item.parameter_values),
            *(value.value for value in item.reference_values),
        ]
        normalized_haystacks = [value.casefold() for value in haystacks]
        if any(term in value for term in terms for value in normalized_haystacks):
            result.append(item)
    return result[:500]


def _config_facts(objects: list[ConfigObject]) -> list[dict]:
    facts = []
    for item in objects:
        module = _module_from_config(item)
        for relation, values in (
            ("parameter", item.parameter_values),
            ("reference", item.reference_values),
        ):
            for value in values:
                facts.append(
                    {
                        "module": module,
                        "object": item.name,
                        "object_type": item.kind,
                        "relation": relation,
                        "field": value.name,
                        "definition_ref": value.definition_ref,
                        "value": value.value,
                        "evidence": item.evidence.as_dict(),
                    }
                )
    return facts[:2000]


def _module_from_config(item: ConfigObject) -> str:
    module_names = {
        "CANIF": "CanIf",
        "PDUR": "PduR",
        "LINIF": "LinIf",
        "BSWM": "BswM",
        "COMM": "ComM",
        "COM": "Com",
        "RTE": "OS/RTE",
        "OS": "OS/RTE",
        "CDD": "CDD/Application",
    }
    definition_segments = [
        normalize_identifier(segment)
        for segment in re.split(r"[/\\]", item.definition_ref or "")
        if segment
    ]
    for segment in definition_segments:
        module = module_names.get(segment)
        if module is not None:
            return module

    # SHORT-NAME/kind 只在明确以模块名开头时作为补充；不在
    # AUTOSAR、CROSS 等普通单词内做 OS/RTE 子串命中。
    signature = normalize_identifier(f"{item.name} {item.kind}")
    for token in ("CANIF", "PDUR", "LINIF", "BSWM", "COMM", "COM", "RTE", "CDD"):
        if signature.startswith(token):
            return module_names[token]
    if re.search(
        r"(?:^|[_\-/])OS(?:[_\-/]|$)",
        f"{item.name} {item.kind}",
        re.IGNORECASE,
    ):
        return "OS/RTE"
    return "Other"


def _producer_consumer(gateway: dict) -> list[dict]:
    result = []
    for mapping in gateway.get("gateways", []):
        source = mapping.get("source")
        if source:
            result.append(
                {
                    "role": "producer",
                    "mapping": mapping.get("mapping"),
                    "signal": source.get("signal"),
                    "reference": source.get("reference"),
                    "ipdus": source.get("ipdus", []),
                    "evidence": source.get("evidence") or mapping.get("evidence"),
                }
            )
        for destination in mapping.get("destinations", []):
            if not destination:
                continue
            result.append(
                {
                    "role": "consumer",
                    "mapping": mapping.get("mapping"),
                    "signal": destination.get("signal"),
                    "reference": destination.get("reference"),
                    "ipdus": destination.get("ipdus", []),
                    "evidence": destination.get("evidence") or mapping.get("evidence"),
                }
            )
    return result


def _semantic_attribute(
    config_facts: list[dict], source_matches: list[dict], values: tuple[str, ...]
) -> dict:
    evidence = _matching_semantics(config_facts, source_matches, values)
    found = []
    for item in evidence:
        text = str(item).upper()
        found.extend(value for value in values if _token_present(text, value))
    unique = list(dict.fromkeys(found))
    return {
        "value": unique[0] if len(unique) == 1 else "CONFLICT" if unique else "UNKNOWN",
        "evidence": evidence[:50],
    }


def _matching_semantics(
    config_facts: list[dict], source_matches: list[dict], tokens: tuple[str, ...]
) -> list[dict]:
    result = []
    for item in [*config_facts, *source_matches]:
        text = str(item).upper()
        if any(_token_present(text, token) for token in tokens):
            result.append(item)
    return _deduplicate_dicts(result)


def _gateway_group_references(gateway: dict) -> list[dict]:
    result = []
    for endpoint in gateway.get("gateways", []):
        for side in [endpoint.get("source"), *endpoint.get("destinations", [])]:
            if not side:
                continue
            for ipdu in side.get("ipdus", []):
                for reference in ipdu.get("group_references", []):
                    result.append(
                        {
                            "module": "Com",
                            "ipdu": ipdu.get("name"),
                            "group_reference": reference,
                            "evidence": ipdu.get("evidence"),
                        }
                    )
    return result


def _deduplicate_dicts(items: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in items:
        key = repr(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _empty_source_evidence(source_index: SourceIndex) -> dict:
    return {
        "identifiers": [],
        "results": [],
        "not_found": [],
        "search_scope": {
            "root": str(source_index.root),
            "matching_mode": "exact_identifier_from_sqlite_index",
        },
    }


def _token_present(text: str, token: str) -> bool:
    # 枚举值需要完整边界，避免把 UNBUFFERED 同时识别成 BUFFERED；函数或
    # 配置标识符中的语义词（如 Com_MainFunctionRx）则允许作为子串出现。
    if token in {"BUFFERED", "UNBUFFERED", "DEFERRED", "IMMEDIATE"}:
        return bool(
            re.search(rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])", text)
        )
    return token in text
