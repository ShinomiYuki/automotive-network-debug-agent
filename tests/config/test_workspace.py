"""验证 Config Workspace 的索引复用、配置搜索和路由链查询。"""

from pathlib import Path
from threading import Event

import pytest

import anda.config.workspace as workspace_module
from anda.common.errors import (
    ConfigConflictError,
    ConfigInputError,
    ConfigNotFoundError,
    ConfigNotReadyError,
    ConfigParseError,
)
from anda.config.workspace import ConfigWorkspaceManager

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "config"


@pytest.fixture()
def loaded_workspace() -> tuple[ConfigWorkspaceManager, str]:
    """每个测试使用独立的进程内管理器，避免测试顺序影响复用结果。"""
    manager = ConfigWorkspaceManager()
    result = manager.load_config_workspace(str(FIXTURE_ROOT))
    return manager, result["workspace_id"]


def test_workspace_load_reuses_snapshot_and_force_reload_replaces_it():
    manager = ConfigWorkspaceManager()

    first = manager.load_config_workspace(str(FIXTURE_ROOT))
    reused = manager.load_config_workspace(str(FIXTURE_ROOT))
    reloaded = manager.load_config_workspace(str(FIXTURE_ROOT), force_reload=True)

    assert first["index_ready"] is True
    assert first["message_count"] == 6
    assert first["routing_path_count"] == 5
    assert first["signal_gateway_count"] == 1
    assert reused["workspace_id"] == first["workspace_id"]
    assert reused["reused"] is True
    assert reloaded["workspace_id"] != first["workspace_id"]
    assert reloaded["reused"] is False
    with pytest.raises(ConfigNotFoundError, match="未知 workspace_id"):
        manager.search_config_symbol(first["workspace_id"], "0x416")


def test_background_load_returns_stable_id_and_deduplicates(monkeypatch):
    manager = ConfigWorkspaceManager()
    entered = Event()
    release = Event()
    original_scan = workspace_module._scan_supported_files

    def delayed_scan(scope):
        entered.set()
        release.wait(5)
        return original_scan(scope)

    monkeypatch.setattr(workspace_module, "_scan_supported_files", delayed_scan)
    first = manager.start_load_config_workspace(str(FIXTURE_ROOT))
    assert entered.wait(1)
    second = manager.start_load_config_workspace(str(FIXTURE_ROOT))
    status = manager.get_load_status(first["workspace_id"])

    assert first["index_ready"] is False
    assert second["workspace_id"] == first["workspace_id"]
    assert second["reused"] is True
    assert status["status"] == "loading"
    with pytest.raises(ConfigNotReadyError, match="get_config_load_status"):
        manager.search_config_symbol(first["workspace_id"], "0x416")

    release.set()
    ready = manager.get_load_status(first["workspace_id"], wait_seconds=5)
    assert ready["status"] == "ready"
    assert ready["message_count"] == 6


def test_background_load_surfaces_failure_and_allows_retry(tmp_path):
    corrupt = tmp_path / "corrupt.arxml"
    corrupt.write_text("<AUTOSAR>", encoding="ascii")
    manager = ConfigWorkspaceManager()

    first = manager.start_load_config_workspace(str(corrupt))
    failed = manager.get_load_status(first["workspace_id"], wait_seconds=5)
    retry = manager.start_load_config_workspace(str(corrupt))

    assert failed["status"] == "failed"
    assert "ConfigParseError" in failed["error"]
    assert retry["workspace_id"] != first["workspace_id"]
    assert retry["reused"] is False
    assert manager.get_load_status(retry["workspace_id"], wait_seconds=5)["status"] == (
        "failed"
    )


def test_workspace_can_load_one_explicit_arxml_without_neighbor_projects():
    manager = ConfigWorkspaceManager()
    selected_file = FIXTURE_ROOT / "arxml" / "ecuc_canif.arxml"

    loaded = manager.load_config_workspace(str(selected_file))
    route = manager.trace_message_route(
        loaded["workspace_id"], arbitration_id=0x534, source_network="Network_BD"
    )

    assert loaded["root_path"] == str(selected_file.resolve())
    assert loaded["supported_file_count"] == 1
    assert route["message"]["name"] == "Message_534_Rx"


def test_workspace_accepts_separate_project_and_arxml_paths():
    manager = ConfigWorkspaceManager()
    project_root = FIXTURE_ROOT / "source"
    selected_arxml = FIXTURE_ROOT / "arxml" / "ecuc_canif.arxml"

    loaded = manager.load_config_workspace(
        str(project_root), arxml_paths=[str(selected_arxml)]
    )
    route = manager.trace_message_route(
        loaded["workspace_id"], arbitration_id=0x534, source_network="Network_BD"
    )

    assert loaded["supported_file_count"] == 2
    assert loaded["arxml_file_count"] == 1
    assert loaded["source_file_count"] == 1
    assert loaded["workspace_mode"] == "source_plus_arxml"
    assert loaded["arxml_enrichment_enabled"] is True
    assert route["routing_paths"][0]["name"] == "Route_534"


def test_source_only_workspace_and_source_first_inspection():
    manager = ConfigWorkspaceManager()
    project_root = FIXTURE_ROOT / "source"

    loaded = manager.load_config_workspace(str(project_root))
    inspected = manager.inspect_source_symbol(
        loaded["workspace_id"], "VehicleSpeed_GW", context_lines=1
    )

    assert loaded["arxml_file_count"] == 0
    assert loaded["source_file_count"] == 1
    assert loaded["workspace_mode"] == "source_only"
    assert loaded["arxml_enrichment_enabled"] is False
    assert inspected["inspection_basis"] == "exact_c_identifier"
    assert inspected["semantic_inference_performed"] is False
    assert inspected["role_counts"]["function_declaration_or_definition"] == 1
    assert inspected["config_match_count"] == 0


def test_trace_message_route_reports_two_destinations_with_evidence(
    loaded_workspace,
):
    manager, workspace_id = loaded_workspace

    result = manager.trace_message_route(
        workspace_id,
        arbitration_id=0x416,
        source_network="SU",
        destination_network="IC",
    )

    assert result["route_found"] is True
    assert result["requested_destination_found"] is True
    assert result["message"]["name"] == "CanFrame_416"
    assert result["message"]["network"] == "SU"
    route = result["routing_paths"][0]
    assert route["name"] == "PduRRoutingPath_416_SU"
    assert route["source"]["pdu"] == "IPdu_416_SU"
    assert {item["pdu"] for item in route["destinations"]} == {
        "IPdu_416_IC",
        "IPdu_416_GL",
    }
    assert {
        network for item in route["destinations"] for network in item["networks"]
    } == {
        "IC",
        "GL",
    }
    assert route["evidence"]["file"].endswith("pdur.arxml")
    assert route["evidence"]["line"] > 0


def test_route_absence_and_empty_destination_are_distinguished(loaded_workspace):
    manager, workspace_id = loaded_workspace

    no_route = manager.trace_message_route(workspace_id, arbitration_id=0x700)
    empty_destination = manager.trace_message_route(workspace_id, arbitration_id=0x600)

    assert no_route["route_found"] is False
    assert no_route["routing_paths"] == []
    assert empty_destination["route_found"] is True
    assert empty_destination["routing_paths"][0]["destination_missing"] is True


def test_single_destination_and_unknown_message(loaded_workspace):
    manager, workspace_id = loaded_workspace

    single = manager.trace_message_route(workspace_id, arbitration_id=0x500)

    assert [item["pdu"] for item in single["routing_paths"][0]["destinations"]] == [
        "IPdu_500_IC"
    ]
    assert single["routing_path_count"] == 1
    selected_pdu = manager.inspect_pdu(workspace_id, "/Network/IPdu_500_SU")
    assert selected_pdu["reference"] == "/Network/IPdu_500_SU"
    assert [route["name"] for route in selected_pdu["routing_as_source"]] == [
        "PduRRoutingPath_500_SU"
    ]
    with pytest.raises(ConfigNotFoundError, match="0x999"):
        manager.trace_message_route(workspace_id, arbitration_id=0x999)


def test_davinci_ecuc_canif_route_uses_definition_refs_not_reference_order(
    loaded_workspace,
):
    manager, workspace_id = loaded_workspace

    result = manager.trace_message_route(
        workspace_id,
        arbitration_id=0x534,
        source_network="Network_BD",
        destination_network="Network_IC",
    )

    assert result["message"]["name"] == "Message_534_Rx"
    assert result["message"]["pdu_references"] == ["/ActiveEcuC/EcuC/Pdu_534_Rx"]
    assert result["routing_paths"][0]["name"] == "Route_534"
    assert result["routing_paths"][0]["source"]["pdu"] == "Pdu_534_Rx"
    assert result["routing_paths"][0]["destinations"][0]["pdu"] == "Pdu_534_Tx"
    assert result["requested_destination_found"] is True

    transmitted = manager.trace_message_route(
        workspace_id,
        message_name="Message_534_Tx",
        source_network="Network_IC",
    )
    assert transmitted["message"]["direction"] == "send"
    assert transmitted["message"]["network"] == "Network_IC"


def test_communication_inspection_combines_canif_com_timing_and_timeout(
    loaded_workspace,
):
    manager, workspace_id = loaded_workspace

    received = manager.inspect_communication(
        workspace_id, arbitration_id=0x534, network="Network_BD"
    )
    transmitted = manager.inspect_communication(workspace_id, pdu_name="Pdu_534_Tx")

    assert received["message"]["direction"] == "receive"
    assert received["message"]["length_bytes"] == 16
    assert received["message"]["is_fd"] is True
    rx_object = received["communication_objects"][0]
    assert rx_object["canif"][0]["can_id_type"] == "STANDARD_CAN_FD"
    rx_signal_properties = {
        item["name"]: item["value"]
        for item in rx_object["com_ipdus"][0]["signals"][0]["properties"]
    }
    assert rx_signal_properties["ComTimeout"] == "0.5"
    assert rx_signal_properties["ComRxDataTimeoutAction"] == "REPLACE"
    assert rx_signal_properties["ComRxDataTimeoutSubstitutionValue"] == "65535"

    tx_configs = transmitted["communication_objects"][0]["com_ipdus"][0][
        "timing_and_mode_configs"
    ]
    tx_parameters = {
        parameter["name"]: parameter["value"]
        for config in tx_configs
        for parameter in config["parameters"]
    }
    assert tx_parameters["ComMinimumDelayTime"] == "0.005"
    assert tx_parameters["ComTxModeMode"] == "PERIODIC"
    assert tx_parameters["ComTxModeTimePeriod"] == "0.02"


def test_signal_gateway_and_ipdu_group_direct_controls(loaded_workspace):
    manager, workspace_id = loaded_workspace

    signal = manager.trace_signal_gateway(workspace_id, "VehicleSpeed")
    group = manager.inspect_ipdu_group(workspace_id, "TxGroup")

    assert signal["gateway_found"] is True
    assert signal["gateways"][0]["mapping"] == "VehicleSpeed_GW"
    assert signal["gateways"][0]["source"]["signal"] == "VehicleSpeed_Rx"
    assert signal["gateways"][0]["destinations"][0]["signal"] == ("VehicleSpeed_Tx")
    assert signal["gateways"][0]["source"]["ipdus"][0]["name"] == ("ComPdu_534_Rx")

    assert group["member_count"] == 1
    assert group["members"][0]["name"] == "ComPdu_534_Tx"
    assert {item["module"] for item in group["direct_control_references"]} == {
        "BswM",
        "ComM",
    }
    bswm = next(
        item for item in group["direct_control_references"] if item["module"] == "BswM"
    )
    assert {item["name"]: item["value"] for item in bswm["parameters"]}[
        "BswMComPduGroupSwitchAction"
    ] == "START"
    assert group["runtime_state_inferred"] is False


def test_search_inspect_and_source_context_are_bounded(loaded_workspace):
    manager, workspace_id = loaded_workspace

    search = manager.search_config_symbol(workspace_id, "0x416", limit=500)
    pdu = manager.inspect_pdu(workspace_id, "IPdu_416_IC")
    context = manager.find_source_context(
        workspace_id,
        "PduRRoutingPath_416_SU",
        limit=500,
        context_lines=500,
    )

    assert search["limit_applied"] == 50
    assert search["matches"][0]["arbitration_id"] == 0x416
    assert pdu["networks"] == ["IC"]
    assert pdu["routing_as_destination"][0]["name"] == "PduRRoutingPath_416_SU"
    assert context["limit_applied"] == 50
    assert context["context_lines_applied"] == 5
    assert context["total_count"] == 3
    assert all(
        len(line["text"]) <= 240
        for match in context["matches"]
        for line in match["context"]
    )
    assert context["matches"][0]["line"] > 0


def test_symbol_search_supports_pdu_multiple_matches_and_empty_result(
    loaded_workspace,
):
    manager, workspace_id = loaded_workspace

    pdu_matches = manager.search_config_symbol(workspace_id, "IPdu_416")
    missing = manager.search_config_symbol(workspace_id, "DefinitelyMissingSymbol")

    assert pdu_matches["total_count"] > 1
    assert any(match["match_type"] == "pdu" for match in pdu_matches["matches"])
    assert missing["total_count"] == 0
    assert missing["matches"] == []


def test_source_symbol_search_and_context_report_roles(loaded_workspace):
    manager, workspace_id = loaded_workspace

    search = manager.search_source_symbol(workspace_id, "VehicleSpeed", limit=20)
    context = manager.find_source_context(
        workspace_id, "VehicleSpeed_GW", context_lines=1
    )

    assert {item["symbol"] for item in search["matches"]} >= {
        "VehicleSpeed_GW",
        "VehicleSpeed_Rx",
        "VehicleSpeed_Tx",
    }
    assert context["role_counts"]["function_declaration_or_definition"] == 1
    assert context["matches"][0]["role"] == ("function_declaration_or_definition")

    inspected = manager.inspect_source_symbol(
        workspace_id, "VehicleSpeed_GW", context_lines=1
    )
    assert inspected["config_link_basis"] == "exact_same_identifier"
    assert inspected["semantic_inference_performed"] is False

    linked = manager.inspect_source_symbol(workspace_id, "VehicleSpeed_Rx")
    assert linked["config_match_count"] >= 1
    assert any(match["name"] == "VehicleSpeed_Rx" for match in linked["config_matches"])


def test_ambiguous_can_id_requires_network_and_reports_candidate_evidence(
    loaded_workspace,
):
    manager, workspace_id = loaded_workspace

    with pytest.raises(ConfigConflictError) as captured:
        manager.trace_message_route(workspace_id, arbitration_id=0x534)

    message = str(captured.value)
    assert "Message_534_Rx" in message
    assert "Message_534_Tx" in message
    assert "PDU=Pdu_534_Rx" in message
    assert "ecuc_canif.arxml" in message


def test_invalid_workspace_and_malformed_arxml_return_domain_errors(tmp_path):
    manager = ConfigWorkspaceManager()

    with pytest.raises(ConfigInputError, match="不存在"):
        manager.load_config_workspace(str(tmp_path / "missing"))

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("unsupported", encoding="utf-8")
    with pytest.raises(ConfigInputError, match="没有支持"):
        manager.load_config_workspace(str(empty))

    malformed = tmp_path / "broken.arxml"
    malformed.write_text("<AUTOSAR><SHORT-NAME>broken", encoding="utf-8")
    with pytest.raises(ConfigParseError, match="ARXML 格式损坏"):
        manager.load_config_workspace(str(tmp_path))
