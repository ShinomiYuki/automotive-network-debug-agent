"""Exercise observable scope, configuration links and refusal behavior."""

import argparse
import json
import sys
from pathlib import Path

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from arxml_io import XmlDocument
from tx_mailbox import (
    ConfigurationError,
    issue_records,
    make_plan,
    markdown_report,
    ref,
    run_configuration,
)

NS = "http://autosar.org/schema/r4.0"


def element(parent, name, text=None, **attributes):
    node = etree.SubElement(parent, f"{{{NS}}}{name}", **attributes)
    node.text = text
    return node


def fixture_project(tmp_path):
    root = element(etree.Element("holder"), "AUTOSAR")
    package = element(element(root, "AR-PACKAGES"), "AR-PACKAGE")
    element(package, "SHORT-NAME", "ActiveEcuC")
    content = element(package, "ELEMENTS")
    for name in ("Can", "CanIf", "BswM"):
        module = element(content, "ECUC-MODULE-CONFIGURATION-VALUES")
        element(module, "SHORT-NAME", name)
        element(module, "DEFINITION-REF", "/MICROSAR/" + name)
        element(module, "CONTAINERS")
    baseline = tmp_path / "baseline.arxml"
    baseline.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8"))
    doc = XmlDocument(baseline)

    def add(parent, name, kind, values=None, references=None):
        node = doc.add_container(parent, name, "/MICROSAR/" + kind)
        for key, value in (values or {}).items():
            doc.set_value(node, key, value, "integer" if str(value).isdigit() else "enum")
        for key, value in (references or {}).items():
            doc.set_value(node, key, value, "ref")
        return node

    can = add(doc.module("Can"), "CanConfigSet", "CanConfigSet")
    canif = add(doc.module("CanIf"), "Init", "CanIfInitCfg")
    hoh = add(canif, "Hoh", "CanIfInitHohCfg")
    controllers = add(doc.module("CanIf"), "Drivers", "CanIfCtrlDrvCfg")
    add(doc.module("CanIf"), "Public", "CanIfPublicCfg", {"CanIfPublicTxConfirmPollingSupport": "false"})
    partition = add(doc.module("BswM"), "Core0", "BswMConfig")
    arbitration = add(partition, "Arbitration", "BswMArbitration")
    control = add(partition, "Control", "BswMModeControl")
    config = tmp_path / "CanIf_Cfg.h"
    config.write_text("", encoding="utf-8")
    header = tmp_path / "CanIf.h"
    header.write_text("CanIf_NotifStatusType CanIf_GetTxConfirmationState(uint8 ControllerId);", encoding="utf-8")
    networks = []
    for index, network in enumerate(("BD", "LC")):
        controller = add(controllers, network, "CanIfCtrlCfg", {"CanIfCtrlId": index}, {"CanIfCtrlCanCtrlRef": "/ActiveEcuC/Can/CanConfigSet/" + network})
        symbol = "CanIfConf_CanIfCtrlCfg_" + network
        with config.open("a", encoding="utf-8") as stream:
            stream.write(f"#define {symbol} {index}u\n")
        add(can, network, "CanController")
        hardware = add(can, f"CN_Project_{network}Messagelis_12345678_Tx", "CanHardwareObject", {"CanObjectId": index, "CanHandleType": "BASIC", "CanObjectType": "TRANSMIT", "CanObjectHwSize": 16, "CanObjectHwFifo": "true"}, {"CanControllerRef": "/ActiveEcuC/Can/CanConfigSet/" + network})
        hth = add(hoh, network, "CanIfHthCfg", references={"CanIfHthIdSymRef": doc.path(hardware), "CanIfHthCanCtrlIdRef": doc.path(controller)})
        buffer = add(canif, network + "Buffer", "CanIfBufferCfg", {"CanIfBufferSize": 20, "CanIfTxBufferHandlingType": "FIFO", "CanIfTxBufferMappedTxPdus": 5}, {"CanIfBufferHthRef": doc.path(hth)})
        enable = add(control, network + "Enable", "BswMActionList")
        disable = add(control, network + "Disable", "BswMActionList")
        expression = add(arbitration, network + "Expression", "BswMLogicalExpression", {"BswMLogicalOperator": "BSWM_AND"})
        rule = add(arbitration, network + "Rule", "BswMRule", references={"BswMRuleExpressionRef": doc.path(expression), "BswMRuleTrueActionList": doc.path(enable), "BswMRuleFalseActionList": doc.path(disable)})
        dbc = tmp_path / (network + ".dbc")
        text = 'VERSION ""\nNS_ :\nBS_:\nBU_: Local Other\n'
        for message_id, name, sender, cycle, send_type in (
            (350, "Fast", "Local", 10, 0), (351, "AlsoFast", "Local", 10, 0),
            (352, "Slow", "Local", 20, 0), (353, "Event", "Local", 10, 1),
            (354, "Foreign", "Other", 10, 0),
        ):
            text += f"BO_ {message_id} {name}: 8 {sender}\n"
            add(canif, name + "_" + network, "CanIfTxPduCfg", {"CanIfTxPduCanId": message_id, "CanIfTxPduCanIdType": "STANDARD_CAN", "CanIfTxPduDlc": 8, "CanIfTxPduId": message_id}, {"CanIfTxPduBufferRef": doc.path(buffer)})
        text += 'BA_DEF_ BO_ "GenMsgCycleTime" INT 0 65535;\nBA_DEF_ BO_ "GenMsgSendType" ENUM "Cyclic","Event";\nBA_DEF_DEF_ "GenMsgSendType" "Cyclic";\n'
        for message_id, cycle in ((350, 10), (351, 10), (352, 20), (353, 10), (354, 10)):
            text += f'BA_ "GenMsgCycleTime" BO_ {message_id} {cycle};\n'
        text += 'BA_ "GenMsgSendType" BO_ 353 1;\n'
        dbc.write_text(text, encoding="utf-8")
        networks.append({"network": network, "dbc": str(dbc), "tx_nodes": ["Local"], "controller_path": doc.path(controller), "controller_symbol": symbol, "tx_rule_path": doc.path(rule)})
    baseline.write_bytes(doc.to_bytes())
    return {"schema_version": 1, "project_root": str(tmp_path), "arxml": str(baseline), "canif_cfg": str(config), "canif_header": str(header), "networks": networks}


def apply(tmp_path, manifest, records, name="result"):
    manifest_path = tmp_path / (name + "-manifest.json")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    issues = tmp_path / (name + "-issues.json")
    issues.write_text(json.dumps(records), encoding="utf-8")
    return run_configuration(argparse.Namespace(command="apply", manifest=manifest_path, issues=issues, all_networks=False, output_dir=tmp_path / name))


def test_issue_scope_includes_all_fast_tx_on_selected_network(tmp_path):
    manifest = fixture_project(tmp_path)
    original = Path(manifest["arxml"]).read_bytes()
    result = apply(tmp_path, manifest, [{"network": "BD", "id": "0x15E"}])
    report = json.loads((Path(result["output_directory"]) / "report.json").read_text(encoding="utf-8"))
    assert [(item["network"], item["id"]) for item in report["messages"]] == [("BD", 350), ("BD", 351)]
    assert report["messages"][0]["new_hardware"].endswith("/CN_Project_BD_15E_Tx")
    assert report["messages"][0]["new_hth"].endswith("/CN_Project_BD_15E_Tx")
    assert report["messages"][0]["new_buffer"].endswith("/CanIfBufferCfg_BD_15E")
    assert report["bswm"][0]["condition"].endswith("/BswMModeCondition_000")
    assert Path(manifest["arxml"]).read_bytes() == original
    doc = XmlDocument(report["arxml"])
    lc = doc.at(manifest["networks"][1]["tx_rule_path"])
    assert ref(doc, lc, "BswMRuleExpressionRef").endswith("/LCExpression")
    assert doc.value(doc.at("/ActiveEcuC/CanIf/Init/BDBuffer"), "CanIfTxBufferMappedTxPdus") == "3"


def test_all_networks_excludes_event_foreign_and_slow_messages(tmp_path):
    manifest = fixture_project(tmp_path)
    _, report = make_plan(manifest, all_networks=True)
    assert [(item["network"], item["id"]) for item in report["messages"]] == [("BD", 350), ("BD", 351), ("LC", 350), ("LC", 351)]


def test_repeat_configuration_reuses_mailboxes_and_gate(tmp_path):
    manifest = fixture_project(tmp_path)
    first = apply(tmp_path, manifest, [{"network": "BD", "id": 350}])
    manifest["arxml"] = str(Path(first["output_directory"]) / "configured.arxml")
    second = apply(tmp_path, manifest, [{"network": "BD", "id": 350}], "second")
    first_doc = XmlDocument(manifest["arxml"])
    second_doc = XmlDocument(Path(second["output_directory"]) / "configured.arxml")
    assert len(first_doc.containers) == len(second_doc.containers)
    assert second_doc.original == first_doc.original


@pytest.mark.parametrize("change", ["wrong_symbol", "wrong_controller_id", "missing_api", "ineligible_issue", "ambiguous_pdu"])
def test_conflicts_refuse_without_output(tmp_path, change):
    manifest = fixture_project(tmp_path)
    records = [{"network": "BD", "id": 350}]
    if change == "wrong_symbol":
        manifest["networks"][0]["controller_symbol"] = "Invented"
    elif change == "wrong_controller_id":
        Path(manifest["canif_cfg"]).write_text("#define CanIfConf_CanIfCtrlCfg_BD 5u\n", encoding="utf-8")
    elif change == "missing_api":
        Path(manifest["canif_header"]).write_text("", encoding="utf-8")
    elif change == "ineligible_issue":
        records[0]["id"] = 353
    else:
        doc = XmlDocument(manifest["arxml"])
        original = doc.at("/ActiveEcuC/CanIf/Init/Fast_BD")
        doc.clone_container(original, original.getparent().getparent(), "Duplicate_BD")
        Path(manifest["arxml"]).write_bytes(doc.to_bytes())
    with pytest.raises((ConfigurationError, KeyError)):
        apply(tmp_path, manifest, records)
    assert not (tmp_path / "result").exists()


def test_multiple_issue_records_and_html_breaks(tmp_path):
    file = tmp_path / "issues.md"
    file.write_text("测试网段：BD<br>简要描述:自发报文自发周期超规格<br>预期结果:id=0x15E\n测试网段：LC<br>自发周期超规格<br>id=0x15E", encoding="utf-8")
    assert issue_records(file) == {"BD": {350}, "LC": {350}}
    manifest = fixture_project(tmp_path)
    result = apply(tmp_path, manifest, [{"network": "BD", "id": 350}, {"network": "LC", "id": 350}])
    assert result["messages"] == 4
    report = json.loads((Path(result["output_directory"]) / "report.json").read_text(encoding="utf-8"))
    assert [(gate["network"], gate["port"].rsplit("/", 1)[-1]) for gate in report["bswm"]] == [("BD", "BDCanNmTxConfirmationState"), ("LC", "LCCanNmTxConfirmationState")]
    assert report["bswm"][1]["user_condition"] == "CanIf_GetTxConfirmationState(CanIfConf_CanIfCtrlCfg_LC)"


def test_preview_does_not_claim_output_validation(tmp_path):
    manifest = fixture_project(tmp_path)
    _, report = make_plan(manifest, issues={"BD": {350}})
    report["validation"] = "PLAN_ONLY"
    text = markdown_report(report)
    assert "尚未生成 ARXML" in text
    assert "输出重解析及生成后的配置约束校验将在 apply 阶段执行" in text
    assert "已执行输出 XML 重解析" not in text
    assert [item["selection_reason"] for item in report["messages"]] == ["reported_issue", "same_network_10ms"]
