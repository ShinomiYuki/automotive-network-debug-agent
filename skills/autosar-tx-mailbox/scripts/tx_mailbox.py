"""Configure dedicated transmit objects and BswM confirmation conditions locally."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import cantools
from arxml_io import XmlDocument as ArxmlDocument
from arxml_io import definition_ref, short_name
from lxml import etree

VERSION = "1.0.1"
PERIOD_MS = 10
SUPPORTED_SEND_TYPES = {"cyclic", "periodic"}


class ConfigurationError(ValueError):
    """An input cannot be mapped to one unambiguous configuration object."""


def require(condition, message):
    if not condition:
        raise ConfigurationError(message)


def one(items, description):
    items = list(items)
    require(len(items) == 1, f"{description}: expected one object, found {len(items)}")
    return items[0]


def text_file(path):
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ConfigurationError(f"Unsupported text encoding: {path}")


def write_json_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def load_dbc(path):
    # cantools resolves the DBC enumeration and its default value; index zero is
    # not assumed to mean periodic transmission.
    return cantools.database.load_string(text_file(path), database_format="dbc")


def get(doc, node, key, default=None):
    return doc.value(node, key, default=default)


def ref(doc, node, key):
    return doc.value(node, key)


def refs(doc, node, key):
    return doc.values(node, key)


def objects(doc, kind, within=None):
    return [
        node for node in doc.containers.values()
        if definition_ref(node).rsplit("/", 1)[-1] == kind
        and (within is None or node == within or within in node.iterancestors())
    ]


def parameter(doc, node, key, value, value_type):
    doc.set_value(node, key, str(value), kind=value_type)


def reference(doc, node, key, target, symbolic=False):
    doc.set_value(node, key, target, kind="symbolic-ref" if symbolic else "ref")


def add_argument(doc, expression, target):
    if target in refs(doc, expression, "BswMArgumentRef"):
        return
    doc.append_value(expression, "BswMArgumentRef", target, kind="choice-ref")


def find_source(root, pattern, explicit=None):
    if explicit:
        return Path(explicit).resolve(strict=True)
    return one(root.rglob(pattern), f"Source file {pattern}; specify its exact path if ambiguous")


def controller_symbols(config_file):
    return dict(re.findall(
        r"^\s*#define\s+(CanIfConf_CanIfCtrlCfg_\w+)\s+(\d+)[uU]?\s*(?:/\*.*)?$",
        text_file(config_file), re.MULTILINE,
    ))


def network_label(controller_name):
    match = re.search(r"_([A-Za-z][A-Za-z0-9]*?)(?:Messagelis)?_[0-9a-fA-F]{8}$", controller_name)
    return match[1] if match else None


def discover(args):
    root = Path(args.project_root).resolve(strict=True)
    arxml = Path(args.arxml).resolve(strict=True)
    config_file = find_source(root, "CanIf_Cfg.h", args.canif_cfg)
    header = find_source(root, "CanIf.h", args.canif_header)
    doc = ArxmlDocument.load(arxml)
    symbols = controller_symbols(config_file)
    dbc_paths = sorted(root.rglob("*.dbc"))
    rules = objects(doc, "BswMRule")
    networks = []
    unresolved = []
    for controller in objects(doc, "CanIfCtrlCfg"):
        name = short_name(controller)
        label = network_label(name)
        if not label:
            unresolved.append(f"Controller needs an explicit network label: {doc.path(controller)}")
            continue
        symbol = "CanIfConf_CanIfCtrlCfg_" + name
        candidates = [path for path in dbc_paths if re.search(
            rf"(?:^|[_\s]){re.escape(label)}(?=[_\s]+Message\b|$)", path.stem, re.IGNORECASE,
        )]
        tx_rules = [node for node in rules if re.search(
            rf"_{re.escape(label)}(?:Messagelis)?_[0-9a-fA-F]{{8}}_TX$", short_name(node),
        )]
        nodes = []
        if len(candidates) == 1:
            database = load_dbc(candidates[0])
            nodes = sorted({sender for message in database.messages for sender in message.senders if sender != "Vector__XXX"})
        if symbol not in symbols or len(candidates) != 1 or len(tx_rules) != 1 or len(nodes) != 1:
            unresolved.append(f"{label}: review DBC, transmitter, controller symbol and TX Rule mapping")
        networks.append({
            "network": label,
            "dbc": str(candidates[0]) if len(candidates) == 1 else None,
            "dbc_candidates": [str(path) for path in candidates] if len(candidates) != 1 else [],
            "tx_nodes": nodes if len(nodes) == 1 else [],
            "tx_node_candidates": nodes if len(nodes) != 1 else [],
            "controller_path": doc.path(controller),
            "controller_symbol": symbol if symbol in symbols else None,
            "tx_rule_path": doc.path(tx_rules[0]) if len(tx_rules) == 1 else None,
        })
    manifest = {
        "schema_version": 1, "project_root": str(root), "arxml": str(arxml),
        "canif_cfg": str(config_file), "canif_header": str(header),
        "networks": networks, "unresolved": unresolved,
    }
    write_json_new(args.out, manifest)
    return {"manifest": str(Path(args.out).resolve()), "networks": len(networks), "unresolved": unresolved}


def issue_records(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        records = json.loads(text_file(path))
        require(isinstance(records, list) and records, "Issue JSON must be a nonempty array")
        result = {}
        for record in records:
            network = str(record["network"])
            ids = record.get("ids", [record.get("id")])
            require(ids and all(value is not None for value in ids), "Issue record has no message ID")
            result.setdefault(network, set()).update(int(value, 0) if isinstance(value, str) else int(value) for value in ids)
        return result
    content = text_file(path)
    starts = list(re.finditer(r"测试网段\s*[:：]\s*([A-Za-z0-9_-]+)", content))
    require(starts, "Issue list has no 测试网段 field; use Markdown/text or structured JSON")
    result = {}
    for index, match in enumerate(starts):
        record = content[match.end():starts[index + 1].start() if index + 1 < len(starts) else len(content)]
        require("自发" in record and re.search(r"周期.*(?:超|规格)", record), "Issue is not a spontaneous-transmit period issue")
        ids = {int(value, 16) for value in re.findall(r"\bid\s*[:=：]\s*0x([0-9a-fA-F]+)\b", record, re.IGNORECASE)}
        require(ids, f"Issue {match[1]} has no explicit hexadecimal message ID")
        result.setdefault(match[1], set()).update(ids)
    return result


def numbered_name(doc, kind, prefix, within):
    names = {short_name(node) for node in objects(doc, kind, within)}
    numbers = [int(match[1]) for name in names if (match := re.fullmatch(re.escape(prefix) + r"_(\d+)", name))]
    return f"{prefix}_{max(numbers, default=-1) + 1:03d}"


def mailbox_names(hardware_name, network, message_id, extended):
    match = re.fullmatch(rf"(.+_{re.escape(network)})(?:Messagelis)?(?:_[0-9a-fA-F]{{8}})?_Tx", hardware_name)
    require(match, f"Cannot derive example-style mailbox name from {hardware_name} for network {network}")
    identifier = f"{message_id:08X}" if extended else f"{message_id:03X}"
    return f"{match[1]}_{identifier}_Tx", f"CanIfBufferCfg_{network}_{identifier}"


def dedicated(doc, pdu):
    buffer = doc.at(ref(doc, pdu, "CanIfTxPduBufferRef"))
    hth = doc.at(ref(doc, buffer, "CanIfBufferHthRef"))
    hardware = doc.at(ref(doc, hth, "CanIfHthIdSymRef"))
    users = [node for node in objects(doc, "CanIfTxPduCfg") if ref(doc, node, "CanIfTxPduBufferRef") == doc.path(buffer)]
    hth_buffers = [node for node in objects(doc, "CanIfBufferCfg") if ref(doc, node, "CanIfBufferHthRef") == doc.path(hth)]
    hardware_hths = [node for node in objects(doc, "CanIfHthCfg") if ref(doc, node, "CanIfHthIdSymRef") == doc.path(hardware)]
    good = (
        len(users) == len(hth_buffers) == len(hardware_hths) == 1
        and get(doc, buffer, "CanIfTxBufferHandlingType") == "NONE"
        and get(doc, buffer, "CanIfBufferSize") == "0"
        and get(doc, buffer, "CanIfTxBufferMappedTxPdus") == "1"
        and get(doc, buffer, "CanIfTxBufferMaxPduLength") == get(doc, pdu, "CanIfTxPduDlc")
        and get(doc, hardware, "CanHandleType") == "FULL"
        and get(doc, hardware, "CanObjectType") == "TRANSMIT"
        and get(doc, hardware, "CanObjectHwSize") == "1"
        and get(doc, hardware, "CanObjectHwFifo") == "false"
        and get(doc, hardware, "CanObjectMultiplexedTransmission") == "false"
    )
    return good, buffer, hth, hardware


def resolve_network(doc, entry, symbols):
    label = entry["network"]
    require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", label), f"Invalid network label: {label}")
    require(entry.get("dbc") and entry.get("tx_nodes"), f"{label}: DBC or ECU transmit node is unresolved")
    require(entry.get("controller_path") and entry.get("tx_rule_path"), f"{label}: controller or TX Rule is unresolved")
    controller = doc.at(entry["controller_path"])
    require(definition_ref(controller).endswith("/CanIfCtrlCfg"), f"{label}: expected CanIfCtrlCfg")
    expected_symbol = "CanIfConf_CanIfCtrlCfg_" + short_name(controller)
    require(entry.get("controller_symbol") == expected_symbol and expected_symbol in symbols, f"{label}: controller macro is not defined by the supplied CanIf_Cfg.h")
    require(int(symbols[expected_symbol]) == int(get(doc, controller, "CanIfCtrlId")), f"{label}: source and ARXML controller IDs disagree")
    rule = doc.at(entry["tx_rule_path"])
    require(definition_ref(rule).endswith("/BswMRule"), f"{label}: expected BswMRule")
    require(ref(doc, rule, "BswMRuleTrueActionList") and ref(doc, rule, "BswMRuleFalseActionList"), f"{label}: TX Rule must have enable and disable actions")
    for key in ("BswMRuleTrueActionList", "BswMRuleFalseActionList"):
        require(definition_ref(doc.at(ref(doc, rule, key))).endswith("/BswMActionList"), f"{label}: TX Rule action is not an ActionList")
    return controller, rule


def make_plan(manifest, issues=None, all_networks=False):
    require(manifest.get("schema_version") == 1, "Unsupported manifest version")
    root = Path(manifest["project_root"]).resolve(strict=True)
    require(root.is_dir(), "Project source root is required")
    config_path = Path(manifest["canif_cfg"]).resolve(strict=True)
    header_path = Path(manifest["canif_header"]).resolve(strict=True)
    require(config_path.is_relative_to(root) and header_path.is_relative_to(root), "CanIf source files must be inside the provided project")
    require(re.search(r"CanIf_GetTxConfirmationState\s*\(\s*uint8\s+ControllerId\s*\)", text_file(header_path)), "CanIf.h does not declare the supported controller query interface")
    doc = ArxmlDocument.load(manifest["arxml"])
    symbols = controller_symbols(config_path)
    require(bool(issues) != bool(all_networks), "Choose issue-list mode or --all-networks")
    entries = manifest["networks"]
    labels = [entry["network"] for entry in entries]
    require(len(labels) == len(set(labels)), "Manifest has duplicate network mappings")
    if issues:
        require(set(issues) <= set(labels), f"Unknown issue networks: {sorted(set(issues) - set(labels))}")
        entries = [entry for entry in entries if entry["network"] in issues]
    changes = []
    reviewed = []
    pdu_nodes = objects(doc, "CanIfTxPduCfg")
    hths = objects(doc, "CanIfHthCfg")
    for entry in entries:
        label = entry["network"]
        require(entry.get("dbc"), f"{label}: DBC mapping is unresolved")
        database = load_dbc(entry["dbc"])
        if not any(message.cycle_time == PERIOD_MS for message in database.messages):
            require(not issues or not issues.get(label), f"{label}: no 10 ms messages exist in DBC")
            reviewed.append({"network": label, "eligible_ids": [], "dbc": entry["dbc"], "tx_nodes": entry["tx_nodes"]})
            continue
        controller, rule = resolve_network(doc, entry, symbols)
        require(set(entry["tx_nodes"]) <= {node.name for node in database.nodes}, f"{label}: transmit node is absent from DBC")
        eligible = [message for message in database.messages if (
            message.cycle_time == PERIOD_MS
            and set(message.senders) & set(entry["tx_nodes"])
            and str(message.send_type).lower() in SUPPORTED_SEND_TYPES
        )]
        if issues:
            missing = issues[label] - {message.frame_id for message in eligible}
            require(not missing, f"{label}: issue IDs are not proven cyclic 10 ms local TX frames: {[hex(value) for value in sorted(missing)]}")
        eligible.sort(key=lambda message: (message.frame_id, message.is_extended_frame))
        reviewed.append({"network": label, "eligible_ids": [f"0x{message.frame_id:X}" for message in eligible], "dbc": entry["dbc"], "tx_nodes": entry["tx_nodes"]})
        allowed_hths = {doc.path(node) for node in hths if ref(doc, node, "CanIfHthCanCtrlIdRef") == doc.path(controller)}
        for message in eligible:
            candidates = []
            for node in pdu_nodes:
                if int(get(doc, node, "CanIfTxPduCanId", "-1"), 0) != message.frame_id:
                    continue
                id_type = get(doc, node, "CanIfTxPduCanIdType", "")
                if message.is_extended_frame != id_type.startswith("EXTENDED"):
                    continue
                buffer = doc.at(ref(doc, node, "CanIfTxPduBufferRef"))
                if ref(doc, buffer, "CanIfBufferHthRef") in allowed_hths:
                    candidates.append(node)
            pdu = one(candidates, f"{label} 0x{message.frame_id:X} CanIf TX mapping")
            require(short_name(pdu).startswith(message.name + "_"), f"{label}: DBC message name and CanIf PDU do not agree for 0x{message.frame_id:X}")
            require(int(get(doc, pdu, "CanIfTxPduDlc")) == message.length, f"{label}: DBC and CanIf lengths differ for {message.name}")
            good, buffer, hth, hardware = dedicated(doc, pdu)
            require(ref(doc, hardware, "CanControllerRef") == ref(doc, controller, "CanIfCtrlCanCtrlRef"), f"{label}: CanIf and Can controller references disagree")
            require(get(doc, hardware, "CanObjectType") == "TRANSMIT", f"{label}: hardware object is not TRANSMIT")
            changes.append({
                "network": label, "id": message.frame_id, "id_hex": f"0x{message.frame_id:X}",
                "extended": message.is_extended_frame, "message": message.name, "period_ms": PERIOD_MS,
                "length": message.length, "pdu_path": doc.path(pdu),
                "old_buffer": doc.path(buffer), "old_hth": doc.path(hth), "old_hardware": doc.path(hardware),
                "controller_path": doc.path(controller), "controller_symbol": entry["controller_symbol"],
                "tx_rule_path": doc.path(rule), "already_dedicated": good,
                "dbc": entry["dbc"], "tx_nodes": entry["tx_nodes"], "send_type": message.send_type,
                "selection_reason": "reported_issue" if issues and message.frame_id in issues[label] else "same_network_10ms" if issues else "all_networks_10ms",
            })
    require(changes, "No eligible cyclic 10 ms local TX message was found; no configuration generated")
    return doc, {"version": VERSION, "scope": "all_networks" if all_networks else "issue_networks", "reviewed_networks": reviewed, "messages": changes}


def create_mailbox(doc, change, next_id):
    if change["already_dedicated"]:
        return
    pdu = doc.at(change["pdu_path"])
    old_buffer = doc.at(change["old_buffer"])
    old_hth = doc.at(change["old_hth"])
    old_hardware = doc.at(change["old_hardware"])
    hardware_name, buffer_name = mailbox_names(short_name(old_hardware), change['network'], change['id'], change['extended'])
    hth_name = hardware_name
    for parent, name in ((old_hardware.getparent(), hardware_name), (old_hth.getparent(), hth_name), (old_buffer.getparent(), buffer_name)):
        require(not any(short_name(child) == name for child in parent), f"Object name already exists with an incompatible configuration: {name}")
    hardware = doc.clone_container(old_hardware, old_hardware.getparent().getparent(), hardware_name)
    for key, value, kind in (
        ("CanHandleType", "FULL", "enum"), ("CanObjectHwSize", "1", "integer"),
        ("CanObjectHwFifo", "false", "boolean"), ("CanObjectMultiplexedTransmission", "false", "boolean"),
        ("CanMaxDataLen", "64", "integer"), ("CanObjectId", str(next_id), "integer"),
    ):
        parameter(doc, hardware, key, value, kind)
    hth = doc.clone_container(old_hth, old_hth.getparent().getparent(), hth_name)
    reference(doc, hth, "CanIfHthIdSymRef", doc.path(hardware), symbolic=True)
    buffer = doc.clone_container(old_buffer, old_buffer.getparent().getparent(), buffer_name)
    for key, value, kind in (
        ("CanIfBufferSize", "0", "integer"), ("CanIfTxBufferHandlingType", "NONE", "enum"),
        ("CanIfTxBufferMappedTxPdus", "1", "integer"), ("CanIfTxBufferMaxPduLength", str(change["length"]), "integer"),
    ):
        parameter(doc, buffer, key, value, kind)
    reference(doc, buffer, "CanIfBufferHthRef", doc.path(hth), symbolic=True)
    reference(doc, pdu, "CanIfTxPduBufferRef", doc.path(buffer), symbolic=True)
    change["new_buffer"] = doc.path(buffer)
    change["new_hth"] = doc.path(hth)
    change["new_hardware"] = doc.path(hardware)


def child_container(doc, parent, name, choice=False):
    return doc.add_container(parent, name, definition_ref(parent) + "/" + name, choice=choice)


def apply_bswm(doc, network, symbol, rule_path):
    rule = doc.at(rule_path)
    arbitration = rule.getparent().getparent()
    require(definition_ref(arbitration).endswith("/BswMArbitration"), "TX Rule is not under BswMArbitration")
    port_name = f"{network}CanNmTxConfirmationState"
    expression = f"CanIf_GetTxConfirmationState({symbol})"
    ports = [node for node in objects(doc, "BswMModeRequestPort", arbitration) if short_name(node) == port_name]
    if ports:
        port = one(ports, port_name)
        source = one(objects(doc, "BswMUserConditionRequest", port), f"{port_name} source")
        require(get(doc, source, "BswMUserCondition") == expression, f"{port_name} already exists with another source")
        require(get(doc, port, "BswMRequestProcessing") == "BSWM_DEFERRED", f"{port_name} processing differs")
        require(get(doc, port, "BswMArbitrateOnInit") == "false", f"{port_name} initialization processing differs")
        initial = one(objects(doc, "BswMBswModeInitValue", port), f"{port_name} initial value")
        require(get(doc, initial, "BswMBswModeInitValueMode") == "0", f"{port_name} initial value differs")
    else:
        port = doc.add_container(arbitration, port_name, definition_ref(arbitration) + "/BswMModeRequestPort")
        parameter(doc, port, "BswMArbitrateOnInit", "false", "boolean")
        parameter(doc, port, "BswMRequestProcessing", "BSWM_DEFERRED", "enum")
        source = child_container(doc, child_container(doc, port, "BswMModeRequestSource", True), "BswMUserConditionRequest")
        parameter(doc, source, "BswMUserCondition", expression, "string")
        initial = child_container(doc, child_container(doc, port, "BswMModeInitValue", True), "BswMBswModeInitValue")
        parameter(doc, initial, "BswMBswModeInitValueMode", "0", "string")
    matching_conditions = []
    for condition in objects(doc, "BswMModeCondition", arbitration):
        if ref(doc, condition, "BswMConditionMode") != doc.path(port):
            continue
        modes = objects(doc, "BswMBswMode", condition)
        if get(doc, condition, "BswMConditionType") == "BSWM_EQUALS" and len(modes) == 1 and get(doc, modes[0], "BswMBswRequestedMode") == "CANIF_TX_RX_NOTIFICATION":
            matching_conditions.append(condition)
    if matching_conditions:
        condition = one(matching_conditions, "Existing confirmation condition")
    else:
        condition_name = numbered_name(doc, "BswMModeCondition", "BswMModeCondition", arbitration)
        condition = doc.add_container(arbitration, condition_name, definition_ref(arbitration) + "/BswMModeCondition")
        parameter(doc, condition, "BswMConditionType", "BSWM_EQUALS", "enum")
        reference(doc, condition, "BswMConditionMode", doc.path(port))
        value = child_container(doc, child_container(doc, condition, "BswMConditionValue", True), "BswMBswMode")
        parameter(doc, value, "BswMBswRequestedMode", "CANIF_TX_RX_NOTIFICATION", "string")
    original = doc.at(ref(doc, rule, "BswMRuleExpressionRef"))
    require(definition_ref(original).endswith("/BswMLogicalExpression"), "Rule expression is not a logical expression")
    # Clone the existing root expression so a shared expression never changes
    # the meaning of another Rule. Its original branches remain intact.
    users = [node for node in objects(doc, "BswMRule") if ref(doc, node, "BswMRuleExpressionRef") == doc.path(original)]
    nested_users = [node for node in objects(doc, "BswMLogicalExpression") if doc.path(original) in refs(doc, node, "BswMArgumentRef")]
    if get(doc, original, "BswMLogicalOperator") == "BSWM_AND" and len(users) == 1 and not nested_users:
        gated = original
        add_argument(doc, gated, doc.path(condition))
    elif doc.path(condition) in refs(doc, original, "BswMArgumentRef") and get(doc, original, "BswMLogicalOperator") == "BSWM_AND":
        gated = original
    else:
        gated_name = numbered_name(doc, "BswMLogicalExpression", short_name(original), arbitration)
        if get(doc, original, "BswMLogicalOperator") == "BSWM_AND":
            gated = doc.clone_container(original, original.getparent().getparent(), gated_name)
        else:
            gated = doc.add_container(arbitration, gated_name, definition_ref(arbitration) + "/BswMLogicalExpression")
            parameter(doc, gated, "BswMLogicalOperator", "BSWM_AND", "enum")
            reference(doc, gated, "BswMArgumentRef", doc.path(original))
            gated.find(f"{doc.q('REFERENCE-VALUES')}/*/{doc.q('DEFINITION-REF')}").set("DEST", "ECUC-CHOICE-REFERENCE-DEF")
        add_argument(doc, gated, doc.path(condition))
        reference(doc, rule, "BswMRuleExpressionRef", doc.path(gated))
    return {"network": network, "port": doc.path(port), "user_condition": expression, "condition": doc.path(condition), "rule": rule_path, "expression": doc.path(gated)}


def snapshot_pdu(doc, node):
    return {key: get(doc, node, key) for key in (
        "CanIfTxPduCanId", "CanIfTxPduCanIdType", "CanIfTxPduDlc", "CanIfTxPduId",
        "CanIfTxPduUserTxConfirmationName", "CanIfTxPduUserTxConfirmationUL",
    )} | {"CanIfTxPduRef": ref(doc, node, "CanIfTxPduRef")}


def validate_result(doc, report, before):
    require(get(doc, one(objects(doc, "CanIfPublicCfg"), "CanIfPublicCfg"), "CanIfPublicTxConfirmPollingSupport") == "true", "Polling API support is disabled")
    for message in report["messages"]:
        node = doc.at(message["pdu_path"])
        require(snapshot_pdu(doc, node) == before[message["pdu_path"]], "A preserved PDU property changed")
        good, _, hth, hardware = dedicated(doc, node)
        require(good, f"Dedicated mailbox validation failed: {message['network']} {message['id_hex']}")
        require(ref(doc, hth, "CanIfHthCanCtrlIdRef") == message["controller_path"], "Wrong CanIf controller")
        controller = doc.at(message["controller_path"])
        require(ref(doc, hardware, "CanControllerRef") == ref(doc, controller, "CanIfCtrlCanCtrlRef"), "Wrong Can controller")
    for gate in report["bswm"]:
        expression = doc.at(ref(doc, doc.at(gate["rule"]), "BswMRuleExpressionRef"))
        require(get(doc, expression, "BswMLogicalOperator") == "BSWM_AND" and gate["condition"] in refs(doc, expression, "BswMArgumentRef"), "Confirmation condition does not gate TX Rule")
    for module in (doc.module("Can"),):
        ids = [get(doc, node, "CanObjectId") for node in objects(doc, "CanHardwareObject", module)]
        require(None not in ids and len(ids) == len(set(ids)), "CanObjectId is missing or duplicated")


def markdown_report(report):
    planned = report.get("validation") == "PLAN_ONLY"
    status = "仅配置计划，尚未生成 ARXML。" if planned else "已生成新 ARXML，并通过本次 XML 和配置约束校验；尚未进行 DaVinci 导入验证。"
    lines = ["# 独立发送邮箱配置结果", "", status, "", f"范围：{report['scope']}。筛选条件：DBC 中本 ECU 发送、Cyclic/Periodic、周期 10 ms。", "", "| 网段 | 报文 ID | 报文 | 纳入原因 | 独立邮箱 |", "|---|---|---|---|---|"]
    for item in report["messages"]:
        reason = {"reported_issue": "问题记录", "same_network_10ms": "同网段其他 10 ms TX", "all_networks_10ms": "全工程 10 ms TX"}[item["selection_reason"]]
        action = '复用已有' if item['already_dedicated'] else '新增'
        lines.append(f"| {item['network']} | {item['id_hex']} | {item['message']} | {reason} | {'计划' if planned else ''}{action} |")
    lines += ["", "## 网段扫描范围", ""]
    for entry in report["reviewed_networks"]:
        lines.append(f"- {entry['network']}：{', '.join(entry['eligible_ids']) or '没有符合条件的报文，不修改'}")
    lines += ["", "## BswM 查询来源", ""]
    for gate in report.get("bswm", []):
        lines.append(f"- {gate['network']}：`{gate['user_condition']}`")
    boundary = "计划只校验输入映射和筛选条件；输出重解析及生成后的配置约束校验将在 apply 阶段执行。" if planned else "已执行输出 XML 重解析、发送引用链及本次参数约束校验。"
    lines += ["", "## 验证边界", "", boundary + "需要在目标 DaVinci 版本中导入并执行 Validate/Generate，之后实测周期与唤醒首帧。控制器发送确认不单独识别网管 PDU。", ""]
    return "\n".join(lines)


def run_configuration(args):
    manifest = json.loads(text_file(args.manifest))
    issues = issue_records(args.issues) if args.issues else None
    doc, report = make_plan(manifest, issues, args.all_networks)
    output_dir = Path(args.output_dir).resolve()
    require(not output_dir.exists(), "Output directory already exists; use a new directory")
    if args.command == "apply":
        before = {item["pdu_path"]: snapshot_pdu(doc, doc.at(item["pdu_path"])) for item in report["messages"]}
        hardware = objects(doc, "CanHardwareObject")
        next_id = max(int(get(doc, node, "CanObjectId")) for node in hardware) + 1
        for item in report["messages"]:
            create_mailbox(doc, item, next_id)
            if not item["already_dedicated"]:
                next_id += 1
        for path in sorted({item["old_buffer"] for item in report["messages"] if not item["already_dedicated"]}):
            count = sum(ref(doc, node, "CanIfTxPduBufferRef") == path for node in objects(doc, "CanIfTxPduCfg"))
            parameter(doc, doc.at(path), "CanIfTxBufferMappedTxPdus", str(count), "integer")
        public = one(objects(doc, "CanIfPublicCfg"), "CanIfPublicCfg")
        parameter(doc, public, "CanIfPublicTxConfirmPollingSupport", "true", "boolean")
        report["bswm"] = []
        seen = set()
        for item in report["messages"]:
            if item["network"] not in seen:
                report["bswm"].append(apply_bswm(doc, item["network"], item["controller_symbol"], item["tx_rule_path"]))
                seen.add(item["network"])
        validate_result(doc, report, before)
        output_dir.mkdir(parents=True)
        output = output_dir / "configured.arxml"
        doc.save_new(output, validator=lambda candidate: validate_result(candidate, report, before))
        report["arxml"] = str(output)
        report["validation"] = "XML_AND_CONFIGURATION_CHECKS_PASSED; DAVINCI_IMPORT_NOT_TESTED"
    else:
        output_dir.mkdir(parents=True)
        report["validation"] = "PLAN_ONLY"
        planned_networks = {item["network"]: item for item in report["messages"]}
        report["bswm"] = [{"network": network, "user_condition": f"CanIf_GetTxConfirmationState({item['controller_symbol']})", "rule": item["tx_rule_path"]} for network, item in planned_networks.items()]
    write_json_new(output_dir / "report.json", report)
    (output_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    return {"output_directory": str(output_dir), "messages": len(report["messages"]), "networks": report["reviewed_networks"], "validation": report["validation"]}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("discover", help="Read source/ARXML/DBC and propose explicit network mappings")
    scan.add_argument("--project-root", required=True)
    scan.add_argument("--arxml", required=True)
    scan.add_argument("--canif-cfg")
    scan.add_argument("--canif-header")
    scan.add_argument("--out", required=True)
    for name in ("plan", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", required=True)
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument("--issues")
        scope.add_argument("--all-networks", action="store_true")
        command.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = discover(args) if args.command == "discover" else run_configuration(args)
        print(json.dumps({"success": True, **result}, ensure_ascii=False, indent=2))
        return 0
    except (ConfigurationError, ValueError, KeyError, OSError, etree.XMLSyntaxError, cantools.database.errors.Error) as error:
        print(json.dumps({"success": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
