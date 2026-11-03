"""Exercise preservation and safe output with small synthetic ECUC documents."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from arxml_io import XmlDocument, definition_ref, local_name

XML = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?editor preserve="yes"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR.xsd">
  <!-- unrelated comment -->
  <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>ActiveEcuC</SHORT-NAME><ELEMENTS>
    <ECUC-MODULE-CONFIGURATION-VALUES UUID="module-id">
      <SHORT-NAME>Can</SHORT-NAME><DEFINITION-REF DEST="ECUC-MODULE-DEF">/MICROSAR/Can</DEFINITION-REF>
      <CONTAINERS><ECUC-CONTAINER-VALUE UUID="config-id"><SHORT-NAME>CanConfigSet</SHORT-NAME><DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/MICROSAR/Can/CanConfigSet</DEFINITION-REF>
        <SUB-CONTAINERS>
          <ECUC-CONTAINER-VALUE UUID="mailbox-id"><SHORT-NAME>SharedTx</SHORT-NAME><DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/MICROSAR/Can/CanConfigSet/CanHardwareObject</DEFINITION-REF>
            <PARAMETER-VALUES><ECUC-TEXTUAL-PARAM-VALUE><DEFINITION-REF DEST="ECUC-ENUMERATION-PARAM-DEF">/MICROSAR/Can/CanConfigSet/CanHardwareObject/CanHandleType</DEFINITION-REF><VALUE>BASIC</VALUE></ECUC-TEXTUAL-PARAM-VALUE></PARAMETER-VALUES>
          </ECUC-CONTAINER-VALUE>
        </SUB-CONTAINERS>
      </ECUC-CONTAINER-VALUE></CONTAINERS>
    </ECUC-MODULE-CONFIGURATION-VALUES>
  </ELEMENTS></AR-PACKAGE></AR-PACKAGES>
</AUTOSAR>
'''


def document(tmp_path, data=None):
    path = tmp_path / "source.arxml"
    path.write_bytes(data if data is not None else XML.replace("\n", "\r\n").encode("utf-8"))
    return XmlDocument(path)


def test_existing_value_changes_only_its_original_bytes(tmp_path):
    doc = document(tmp_path)
    node = doc.at("/ActiveEcuC/Can/CanConfigSet/SharedTx")
    doc.set_value(node, "CanHandleType", "FULL", kind="enum")
    assert doc.to_bytes() == doc.original.replace(b"<VALUE>BASIC</VALUE>", b"<VALUE>FULL</VALUE>")
    assert doc.value(node, "CanHandleType") == "FULL"


def test_no_edit_is_byte_identical_including_bom(tmp_path):
    doc = document(tmp_path, b"\xef\xbb\xbf" + XML.encode("utf-8"))
    assert doc.to_bytes() == doc.original


def test_clone_and_new_values_preserve_unrelated_content(tmp_path):
    doc = document(tmp_path)
    parent = doc.at("/ActiveEcuC/Can/CanConfigSet")
    source = doc.at("/ActiveEcuC/Can/CanConfigSet/SharedTx")
    clone = doc.clone_container(source, parent, "DedicatedTx")
    assert clone.get("UUID") != source.get("UUID")
    doc.set_value(clone, "CanHandleType", "FULL", kind="enum")
    doc.set_value(clone, "CanObjectHwFifo", False, kind="boolean")
    doc.set_value(clone, "CanControllerRef", "/ActiveEcuC/Can/CanConfigSet/BD", kind="symbolic-ref")
    result = doc.to_bytes()
    assert b"<!-- unrelated comment -->" in result
    assert result.startswith(doc.original[:doc.original.index(b"<SUB-CONTAINERS>")])
    assert b"\r\n" in result and b"\n" not in result.replace(b"\r\n", b"")
    assert doc.value(source, "CanHandleType") == "BASIC"
    out = doc.save_new(tmp_path / "new.arxml")
    loaded = XmlDocument(out)
    new = loaded.at("/ActiveEcuC/Can/CanConfigSet/DedicatedTx")
    assert loaded.value(new, "CanObjectHwFifo") == "false"
    assert loaded.value(new, "CanControllerRef") == "/ActiveEcuC/Can/CanConfigSet/BD"
    assert definition_ref(new) == definition_ref(source)


def test_group_insertion_uses_schema_order_and_multi_reference(tmp_path):
    doc = document(tmp_path)
    parent = doc.at("/ActiveEcuC/Can/CanConfigSet")
    node = doc.add_container(parent, "NewCondition", "/MICROSAR/BswM/BswMModeCondition")
    doc.set_value(node, "Port", "/Port/A", kind="choice-ref")
    doc.set_value(node, "Operator", "BSWM_EQUALS", kind="enum")
    doc.append_value(node, "Port", "/Port/B")
    assert [local_name(child) for child in node] == ["SHORT-NAME", "DEFINITION-REF", "PARAMETER-VALUES", "REFERENCE-VALUES"]
    assert doc.values(node, "Port") == ["/Port/A", "/Port/B"]
    with pytest.raises(ValueError, match="Expected one value"):
        doc.value(node, "Port")
    doc.to_bytes()


def test_refuses_input_and_existing_outputs(tmp_path):
    doc = document(tmp_path)
    with pytest.raises(FileExistsError):
        doc.save_new(doc.source_path)
    output = tmp_path / "existing.arxml"
    output.write_bytes(b"unchanged")
    with pytest.raises(FileExistsError):
        doc.save_new(output)
    assert output.read_bytes() == b"unchanged"


def test_validator_failure_leaves_no_output_or_temporary_file(tmp_path):
    doc = document(tmp_path)

    def reject(_document):
        raise ValueError("rejected")

    with pytest.raises(ValueError, match="rejected"):
        doc.save_new(tmp_path / "new.arxml", validator=reject)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.arxml"]


def test_publish_race_never_overwrites_competing_file(tmp_path):
    doc = document(tmp_path)
    target = tmp_path / "new.arxml"

    def other_writer(_document):
        target.write_bytes(b"other writer")

    with pytest.raises(FileExistsError):
        doc.save_new(target, validator=other_writer)
    assert target.read_bytes() == b"other writer"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_forbids_dtd_in_supported_encodings(tmp_path, encoding):
    data = '<?xml version="1.0" encoding="ENC"?><!DOCTYPE AUTOSAR [<!ENTITY external SYSTEM "file:///secret">]><AUTOSAR xmlns="urn:test"/>'.replace("ENC", encoding).encode(encoding)
    with pytest.raises(ValueError, match="DTD"):
        document(tmp_path, data)


def test_utf16_keeps_encoding_and_structure(tmp_path):
    data = XML.replace('encoding="UTF-8"', 'encoding="UTF-16"').replace("\n", "\r\n").encode("utf-16")
    doc = document(tmp_path, data)
    doc.set_value(doc.at("/ActiveEcuC/Can/CanConfigSet/SharedTx"), "CanHandleType", "FULL", kind="enum")
    result = doc.to_bytes()
    assert result.startswith(b"\xff\xfe")
    assert "<VALUE>FULL</VALUE>" in result.decode("utf-16")
    assert '<!-- unrelated comment -->' in result.decode("utf-16")
