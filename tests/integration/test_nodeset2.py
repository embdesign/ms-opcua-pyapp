"""NodeSet2 export: dependency closure, honest reporting, and a round-trip import."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from asyncua import Server, ua

from opcua_explorer.errors import ExportError
from opcua_explorer.models import TraversalConfig
from opcua_explorer.opcua.nodeset2 import NodeSet2Config


async def _snapshot(service, demo_model, roots: list[str], **config_kwargs):
    return await service.download(roots, TraversalConfig(batch_size=50, **config_kwargs))


def _elements(path: Path) -> list[ET.Element]:
    tree = ET.parse(path)  # noqa: S314 - file produced by this application
    return list(tree.getroot())


def _node_ids(path: Path) -> set[str]:
    return {element.attrib["NodeId"] for element in _elements(path) if element.attrib.get("NodeId")}


async def _import(path: Path) -> list[ua.NodeId]:
    server = Server()
    await server.init()
    try:
        return await server.import_xml(xmlstring=path.read_text(encoding="utf-8"))
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_export_objects_subtree_writes_file(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    target = tmp_path / "objects.xml"

    result = await service.export_nodeset2(target, snapshot, NodeSet2Config())

    assert target.is_file()
    assert result.nodes_written > 0
    assert result.bytes_written > 0
    assert result.validation.well_formed is True
    assert result.validation.structural_errors == []
    assert result.namespaces[0]["uri"].startswith("http://opcfoundation.org/UA/")
    assert any("urn:opcua-explorer:demo" in entry["uri"] for entry in result.namespaces)
    # Namespace 0 nodes are referenced, never written into the file.
    assert all(not node_id.startswith("ns=0;") for node_id in result.node_ids_written)
    # Attributes the XML exporter cannot write are reported, not dropped silently.
    assert any("WriteMask" in note for note in result.limitations)
    assert any("RolePermissions" in note for note in result.limitations)


@pytest.mark.asyncio
async def test_export_is_importable_into_a_fresh_server(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    target = tmp_path / "objects.xml"

    result = await service.export_nodeset2(target, snapshot, NodeSet2Config())
    assert result.dangling_references == []

    imported = await _import(target)
    assert imported
    plant_id = next(node_id for node_id in imported if "Plant" in node_id.to_string())
    assert plant_id.NamespaceIndex != 0


@pytest.mark.asyncio
async def test_method_arguments_are_written_even_without_values(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    target = tmp_path / "objects.xml"

    result = await service.export_nodeset2(target, snapshot, NodeSet2Config(include_values=False))

    assert result.argument_values_written >= 1
    assert "InputArguments" in target.read_text(encoding="utf-8")
    # The method argument properties are collected as dependencies, not dangling refs.
    assert result.dangling_references == []


@pytest.mark.asyncio
async def test_imported_method_keeps_its_argument_metadata(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    target = tmp_path / "objects.xml"
    await service.export_nodeset2(target, snapshot, NodeSet2Config())

    server = Server()
    await server.init()
    try:
        imported = await server.import_xml(xmlstring=target.read_text(encoding="utf-8"))
        motor_id = next(node_id for node_id in imported if "Motor" in node_id.to_string())
        motor = server.get_node(motor_id)
        method = await motor.get_child(ua.QualifiedName("Start", motor_id.NamespaceIndex))
        properties = await method.get_properties()
        assert [await prop.read_browse_name() for prop in properties]
        arguments = await properties[0].read_value()
        assert arguments and arguments[0].DataType == ua.NodeId(ua.ObjectIds.Double)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_parent_chain_is_exported_for_nested_nodes(service, demo_model, tmp_path):
    motor = demo_model.node_ids["motor"]
    snapshot = await _snapshot(service, demo_model, [motor])
    target = tmp_path / "motor.xml"

    result = await service.export_nodeset2(target, snapshot, NodeSet2Config())

    written = _node_ids(target)
    assert any("Motor" in node_id for node_id in written)
    # The ancestors are part of the file, otherwise ParentNodeId would dangle.
    assert any("Line1" in node_id for node_id in written)
    assert any("Plant" in node_id for node_id in written)
    assert result.dangling_references == []
    for element in _elements(target):
        parent = element.attrib.get("ParentNodeId")
        if parent and not parent.startswith("ns=0;") and "i=85" not in parent:
            assert any(parent in node_id for node_id in written)


@pytest.mark.asyncio
async def test_unsupported_custom_data_type_is_reported_not_written(service, demo_model, tmp_path):
    # The demo defines its custom DataType under Types, so that folder must be downloaded too.
    snapshot = await _snapshot(service, demo_model, ["i=85", "i=86"])
    target = tmp_path / "objects-and-types.xml"

    result = await service.export_nodeset2(target, snapshot, NodeSet2Config())

    reported = {entry["node_id"] for entry in result.unsupported_data_types}
    assert "ns=2;s=MeasurementType" in reported
    assert all("MeasurementType" not in node_id for node_id in result.node_ids_written)
    assert any("DataTypeDefinition" in entry["reason"] for entry in result.unsupported_data_types)
    assert any("custom DataType" in note for note in result.limitations)
    # The file is still valid and importable.
    assert result.validation.well_formed is True
    assert await _import(target)


@pytest.mark.asyncio
async def test_truncated_snapshot_is_reported(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=84"], max_nodes=40)
    assert snapshot.limits_hit
    target = tmp_path / "partial.xml"

    result = await service.export_nodeset2(target, snapshot, NodeSet2Config())

    assert any("truncated" in note for note in result.limitations)
    assert result.validation.well_formed is True


@pytest.mark.asyncio
async def test_export_rejects_empty_snapshot(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    snapshot.nodes.clear()

    with pytest.raises(ExportError):
        await service.export_nodeset2(tmp_path / "empty.xml", snapshot, NodeSet2Config())


@pytest.mark.asyncio
async def test_disabling_dependencies_is_reported(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    target = tmp_path / "no-deps.xml"

    result = await service.export_nodeset2(
        target, snapshot, NodeSet2Config(include_dependencies=False)
    )

    assert result.dependency_nodes_added == 0
    assert any("Dependency collection was disabled" in note for note in result.limitations)


@pytest.mark.asyncio
async def test_schema_validation_skips_without_lxml_or_reports_missing_file(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    target = tmp_path / "objects.xml"
    missing_schema = tmp_path / "does-not-exist.xsd"

    result = await service.export_nodeset2(
        target, snapshot, NodeSet2Config(schema_path=str(missing_schema))
    )

    assert result.validation.schema_attempted is True
    # Either the file is missing or lxml is not installed: both must be reported, never ignored.
    assert result.validation.schema_valid is None
    assert result.validation.schema_error
    assert result.validation.status in {"not-validated", "valid"}


@pytest.mark.asyncio
async def test_result_document_contains_no_secrets(service, demo_model, tmp_path):
    snapshot = await _snapshot(service, demo_model, ["i=85"])
    target = tmp_path / "objects.xml"

    result = await service.export_nodeset2(target, snapshot, NodeSet2Config())
    document = result.to_dict()

    assert document["format"] == "opcua-nodeset2-export"
    assert document["nodes_written"] == result.nodes_written
    assert "password" not in str(document).lower()
