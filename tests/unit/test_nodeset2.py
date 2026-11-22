"""Unit tests for the NodeSet2 export helpers (no server, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opcua_explorer.models import AddressSpace, NodeRecord
from opcua_explorer.opcua.nodeset2 import (
    NodeSet2Config,
    NodeSet2Result,
    NodeSetDependencyCollector,
    ValidationResult,
    _exporter_limitations,
    _inspect_nodeset,
    _structural_validation,
    _validate_schema,
)

_UANS = "http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"

_GOOD = """<?xml version="1.0" encoding="utf-8"?>
<UANodeSet xmlns="{ns}" xmlns:uax="http://opcfoundation.org/UA/2008/02/Types.xsd">
  <NamespaceUris>
    <Uri>urn:test:demo</Uri>
  </NamespaceUris>
  <UAObject NodeId="ns=1;s=Plant" BrowseName="1:Plant" ParentNodeId="i=85">
    <DisplayName>Plant</DisplayName>
    <References>
      <Reference ReferenceType="HasComponent">ns=1;s=Motor</Reference>
      <Reference ReferenceType="Organizes">i=85</Reference>
    </References>
  </UAObject>
  <UAObject NodeId="ns=1;s=Motor" BrowseName="1:Motor" ParentNodeId="ns=1;s=Plant">
    <DisplayName>Motor</DisplayName>
    <References>
      <Reference ReferenceType="HasTypeDefinition">i=58</Reference>
    </References>
  </UAObject>
</UANodeSet>
""".replace("{ns}", _UANS)

_DANGLING = _GOOD.replace(
    '<Reference ReferenceType="Organizes">i=85</Reference>',
    '<Reference ReferenceType="Organizes">ns=1;s=Nowhere</Reference>',
)

_MISSING_PARENT = _GOOD.replace('ParentNodeId="ns=1;s=Plant"', 'ParentNodeId="ns=1;s=Unknown"')

_UNDECLARED_NAMESPACE = _GOOD.replace('ParentNodeId="i=85"', 'ParentNodeId="ns=7;s=Parent"')


def _write(tmp_path: Path, text: str, name: str = "model.xml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_inspect_reads_namespaces_nodes_and_references(tmp_path):
    inspection = _inspect_nodeset(_write(tmp_path, _GOOD))

    assert inspection.namespaces == ["urn:test:demo"]
    assert inspection.node_ids == {"ns=1;s=Plant", "ns=1;s=Motor"}
    assert "i=85" in inspection.parents
    assert ("ns=1;s=Plant", "ns=1;s=Motor") in inspection.references


def test_structural_validation_accepts_a_consistent_file(tmp_path):
    errors, dangling = _structural_validation(_inspect_nodeset(_write(tmp_path, _GOOD)))

    assert errors == []
    assert dangling == []


def test_structural_validation_reports_dangling_references(tmp_path):
    errors, dangling = _structural_validation(_inspect_nodeset(_write(tmp_path, _DANGLING)))

    assert errors == []
    assert dangling == [{"source": "ns=1;s=Plant", "target": "ns=1;s=Nowhere"}]


def test_structural_validation_reports_unresolvable_parent(tmp_path):
    errors, _ = _structural_validation(_inspect_nodeset(_write(tmp_path, _MISSING_PARENT)))

    assert any("ParentNodeId ns=1;s=Unknown" in message for message in errors)


def test_structural_validation_reports_undeclared_namespace(tmp_path):
    errors, _ = _structural_validation(_inspect_nodeset(_write(tmp_path, _UNDECLARED_NAMESPACE)))

    assert any("ns=7" in message and "declared" in message for message in errors)


def test_inspect_rejects_malformed_xml(tmp_path):
    with pytest.raises(Exception, match=".*"):
        _inspect_nodeset(_write(tmp_path, "<UANodeSet><broken>"))


def test_schema_validation_is_not_requested_by_default(tmp_path):
    attempted, valid, error = _validate_schema(_write(tmp_path, _GOOD), None)

    assert attempted is False
    assert valid is None
    assert error is None


def test_schema_validation_reports_missing_schema_file(tmp_path):
    attempted, valid, error = _validate_schema(_write(tmp_path, _GOOD), str(tmp_path / "missing.xsd"))

    assert attempted is True
    assert valid is None
    assert error and "not found" in error


def test_schema_validation_skips_without_lxml(tmp_path, monkeypatch):
    schema = _write(tmp_path, "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>", "schema.xsd")
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("lxml"):
            raise ImportError("lxml is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    attempted, valid, error = _validate_schema(_write(tmp_path, _GOOD), str(schema))

    assert attempted is True
    assert valid is None
    assert error and "lxml" in error


def test_schema_validation_runs_when_lxml_is_available(tmp_path):
    pytest.importorskip("lxml")
    schema = _write(
        tmp_path,
        """<?xml version="1.0" encoding="utf-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
           xmlns="http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
           elementFormDefault="qualified">
  <xs:element name="UANodeSet">
    <xs:complexType>
      <xs:sequence>
        <xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:anyAttribute processContents="skip"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
""",
        "schema.xsd",
    )

    attempted, valid, error = _validate_schema(_write(tmp_path, _GOOD), str(schema))

    assert attempted is True
    assert valid is True, error


def test_schema_validation_reports_an_invalid_document(tmp_path):
    pytest.importorskip("lxml")
    schema = _write(
        tmp_path,
        """<?xml version="1.0" encoding="utf-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
           xmlns="http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
           elementFormDefault="qualified">
  <xs:element name="UANodeSet">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="Mandatory" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
""",
        "strict.xsd",
    )

    attempted, valid, error = _validate_schema(_write(tmp_path, _GOOD), str(schema))

    assert attempted is True
    assert valid is False
    assert error and "Mandatory" in error


def test_config_is_normalised_and_bounded():
    config = NodeSet2Config(max_dependency_nodes=10_000_000, schema_path="  ").normalised()

    assert config.max_dependency_nodes == 20000
    assert config.schema_path is None
    assert config.to_dict()["max_dependency_nodes"] == 20000


def test_negative_dependency_budget_is_clamped_to_zero():
    assert NodeSet2Config(max_dependency_nodes=-5).normalised().max_dependency_nodes == 0


def test_collector_records_view_nodes_it_cannot_write():
    snapshot = AddressSpace(
        endpoint_url="opc.tcp://demo:4840",
        nodes={
            "ns=2;s=PlantView": NodeRecord(node_id="ns=2;s=PlantView", node_class="View"),
            "ns=2;s=Plant": NodeRecord(node_id="ns=2;s=Plant", node_class="Object"),
        },
    )
    collector = NodeSetDependencyCollector(None, None, snapshot, NodeSet2Config())

    selected = collector._select_for_writing()  # noqa: SLF001 - the selection rule is the unit

    assert selected == ["ns=2;s=Plant"]
    assert collector.skipped_views == ["ns=2;s=PlantView"]


def test_exporter_limitations_name_attributes_asyncua_cannot_write():
    notes = _exporter_limitations({"Object"}, [])
    text = " ".join(notes)
    assert "WriteMask" in text and "UserWriteMask" in text
    assert "AccessRestrictions" in text and "RolePermissions" in text
    assert "Symmetric" not in text
    assert "custom DataType" not in text
    assert "View node(s)" not in text


def test_exporter_limitations_cover_types_and_skipped_views():
    notes = _exporter_limitations({"ReferenceType", "DataType"}, ["ns=2;s=PlantView"])
    text = " ".join(notes)

    assert "Symmetric" in text and "ReferenceType" in text
    assert "IsAbstract is not written for custom DataType" in text
    assert "1 View node(s) were not written" in text
    assert "ParentNodeId" in text


def test_validation_status_reflects_schema_outcome():
    assert ValidationResult().status == "not-validated"
    assert ValidationResult(schema_attempted=True, schema_valid=None).schema_status == "skipped"
    assert ValidationResult(schema_valid=True).status == "valid"
    assert ValidationResult(schema_valid=False, schema_attempted=True).status == "invalid"
    assert ValidationResult(structural_errors=["boom"]).status == "invalid"
    assert ValidationResult(well_formed=False).status == "invalid"


def test_result_summary_reports_limitations():
    result = NodeSet2Result(
        path=Path("/tmp/model.xml"),
        nodes_written=12,
        dependency_nodes_added=3,
        argument_values_written=2,
        missing_dependencies=[{"node_id": "ns=2;s=Missing", "reason": "not readable"}],
        unsupported_data_types=[{"node_id": "ns=2;s=T", "reason": "no DataTypeDefinition attribute"}],
        dangling_references=[{"source": "ns=2;s=A", "target": "ns=2;s=B"}],
        limitations=["snapshot was truncated"],
    )

    text = result.summary_text()

    assert "12" in text
    assert "argument property value" in text
    assert "ns=2;s=Missing" in text
    assert "snapshot was truncated" in text
    assert result.summary()["validation"] == "not-validated"
    document = result.to_dict()
    assert document["format_version"] == 1
    assert document["argument_values_written"] == 2
