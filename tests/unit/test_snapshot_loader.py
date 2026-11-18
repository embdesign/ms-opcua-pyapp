"""Loading an exported JSON address-space document back into a snapshot."""

from __future__ import annotations

import json

import pytest

from opcua_explorer.compare import ComparisonConfig, compare_address_spaces
from opcua_explorer.errors import ExportError
from opcua_explorer.exporters import export_json, load_json_snapshot, snapshot_from_document
from opcua_explorer.models import (
    AddressSpace,
    NamespaceEntry,
    NodeRecord,
    TraversalConfig,
    ValueSnapshot,
)


def _write(document: dict, path) -> str:  # noqa: ANN001 - pytest tmp_path
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def test_export_then_load_round_trips_the_snapshot(sample_snapshot, tmp_path) -> None:
    sample_snapshot.limits_hit = ["max_nodes"]  # a truncated export must not look complete
    target = tmp_path / "snapshot.json"
    export_json(sample_snapshot, target, indent=2)

    loaded = load_json_snapshot(target)

    assert loaded.endpoint_url == sample_snapshot.endpoint_url
    assert loaded.roots == sample_snapshot.roots
    assert loaded.limits_hit == ["max_nodes"]
    assert sorted(loaded.nodes) == sorted(sample_snapshot.nodes)
    for node_id, record in sample_snapshot.nodes.items():
        expected = record.to_dict()
        expected["value"] = None  # values are not exported unless the download included them
        assert loaded.nodes[node_id].to_dict() == expected
    # The writer sorts references, so compare them as a set of keys.
    assert sorted(edge.key for edge in loaded.references) == sorted(
        edge.key for edge in sample_snapshot.references
    )
    assert loaded.namespaces == sample_snapshot.namespaces
    assert loaded.inaccessible == sample_snapshot.inaccessible
    assert loaded.limitations == sample_snapshot.limitations
    # The download bounds are part of the record, so a baseline keeps its provenance.
    assert loaded.config == sample_snapshot.config


def test_traversal_configuration_round_trips(sample_snapshot, tmp_path) -> None:
    sample_snapshot.config = TraversalConfig(
        max_depth=4,
        max_nodes=25,
        concurrency=3,
        batch_size=15,
        max_references_per_node=9,
        namespace_filter=frozenset({0, 2}),
        include_values=True,
        include_description=False,
        follow_subtypes=False,
    )
    target = tmp_path / "snapshot.json"
    export_json(sample_snapshot, target)

    loaded = load_json_snapshot(target)

    assert loaded.config == sample_snapshot.config
    assert loaded.config.namespace_filter == frozenset({0, 2})


def test_loaded_snapshot_compares_equal_to_the_original(sample_snapshot, tmp_path) -> None:
    target = tmp_path / "snapshot.json"
    export_json(sample_snapshot, target)
    loaded = load_json_snapshot(target)

    result = compare_address_spaces(
        sample_snapshot,
        loaded,
        ComparisonConfig(compare_references=True),
    )

    assert result.is_equal, result.summary_text()
    # The fixture contains one inaccessible node, so both sides warn about it.
    assert all("could not browse" in warning for warning in result.warnings)


def test_values_survive_when_the_download_included_them(tmp_path) -> None:
    snapshot = AddressSpace(
        endpoint_url="opc.tcp://demo:4840",
        nodes={
            "ns=2;s=Temperature": NodeRecord(
                node_id="ns=2;s=Temperature",
                node_class="Variable",
                browse_name="Temperature",
                browse_name_namespace_index=2,
                display_name="Temperature",
                namespace_uri="urn:demo",
                data_type="i=11",
                value_rank=-1,
                array_dimensions=[],
                access_level_names=["CurrentRead"],
                user_access_level_names=["CurrentRead"],
                type_definition="i=63",
                value=ValueSnapshot(value=21.5, variant_type="Double", status="Good", encoding="json"),
            )
        },
        namespaces=[NamespaceEntry(0, "http://opcfoundation.org/UA/"), NamespaceEntry(2, "urn:demo")],
        values_included=True,
    )
    target = tmp_path / "with-values.json"
    export_json(snapshot, target)

    loaded = load_json_snapshot(target)

    value = loaded.nodes["ns=2;s=Temperature"].value
    assert value is not None
    assert value.value == 21.5
    assert value.variant_type == "Double"
    assert value.status == "Good"


def test_missing_endpoint_falls_back_to_the_file_name(tmp_path) -> None:
    document = {
        "format": "opcua-address-space-snapshot",
        "format_version": 1,
        "nodes": {"i=85": {"node_id": "i=85", "node_class": "Object", "browse_name": "Objects"}},
        "namespaces": [{"index": 0, "uri": "http://opcfoundation.org/UA/"}],
    }
    loaded = load_json_snapshot(_write(document, tmp_path / "baseline.json"))

    assert loaded.endpoint_url.endswith("baseline.json")
    assert loaded.nodes["i=85"].node_class == "Object"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"format": "something-else", "format_version": 1, "nodes": {"i=85": {}}}, "Not an OPC UA address-space"),
        (
            {"format": "opcua-address-space-snapshot", "format_version": 99, "nodes": {"i=85": {}}},
            "Unsupported format_version 99",
        ),
        ({"format": "opcua-address-space-snapshot", "format_version": 1, "nodes": {}}, "contains no nodes"),
        (
            {
                "format": "opcua-address-space-snapshot",
                "format_version": 1,
                "nodes": {"i=85": {}},
                "references": [{"source": "i=85"}],
            },
            "missing its source or target",
        ),
    ],
)
def test_invalid_documents_are_refused(document, message, tmp_path) -> None:
    with pytest.raises(ExportError, match=message):
        load_json_snapshot(_write(document, tmp_path / "bad.json"))


def test_broken_json_and_missing_files_are_reported(tmp_path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(ExportError, match="not valid JSON"):
        load_json_snapshot(broken)
    with pytest.raises(ExportError, match="could not be read"):
        load_json_snapshot(tmp_path / "missing.json")


def test_document_without_a_node_object_is_refused() -> None:
    with pytest.raises(ExportError, match="not a JSON object"):
        snapshot_from_document([])
