"""JSON and CSV export documents."""

from __future__ import annotations

import csv
import json

from opcua_explorer.exporters import export_csv, export_json, snapshot_to_document
from opcua_explorer.models import AddressSpace, TraversalConfig, ValueSnapshot


def test_json_document_structure(sample_snapshot: AddressSpace) -> None:
    document = snapshot_to_document(sample_snapshot)

    assert document["format"] == "opcua-address-space-snapshot"
    # Version 2 adds the optional per-node ``device_attributes`` map.
    assert document["format_version"] == 2
    assert document["generator"]["name"] == "opcua-explorer"
    assert document["source"]["endpoint_url"] == "opc.tcp://demo:4840"
    assert document["source"]["values_included"] is False
    assert document["source"]["download_roots"] == ["i=85"]

    assert set(document["nodes"]) == {"ns=2;s=Plant", "ns=2;s=Temperature", "ns=2;s=Denied"}
    assert document["namespaces"] == [
        {"index": 0, "uri": "http://opcfoundation.org/UA/"},
        {"index": 2, "uri": "urn:demo"},
    ]

    temperature = document["nodes"]["ns=2;s=Temperature"]
    assert temperature["node_class"] == "Variable"
    assert temperature["data_type"] == "i=11"
    assert temperature["data_type_name"] == "Double"
    assert temperature["access_level_names"] == ["CurrentRead", "CurrentWrite"]
    assert temperature["type_definition_name"] == "BaseDataVariableType"
    # Values must not leak into an export that did not request them.
    assert temperature["value"] is None

    denied = document["nodes"]["ns=2;s=Denied"]
    assert denied["unavailable_attributes"] == {"Value": "BadUserAccessDenied"}


def test_json_records_the_full_traversal_configuration(sample_snapshot: AddressSpace) -> None:
    sample_snapshot.config = TraversalConfig(
        max_depth=3,
        max_nodes=50,
        concurrency=2,
        batch_size=10,
        max_references_per_node=7,
        namespace_filter=frozenset({2}),
        include_values=True,
        include_description=False,
        follow_subtypes=False,
    )

    traversal = snapshot_to_document(sample_snapshot)["source"]["traversal"]

    assert traversal == {
        "max_depth": 3,
        "max_nodes": 50,
        "concurrency": 2,
        "batch_size": 10,
        "max_references_per_node": 7,
        "namespace_filter": [2],
        "include_values": True,
        "include_description": False,
        "follow_subtypes": False,
        "include_device_attributes": False,
    }


def test_json_references_are_forward_edges_with_inclusion_flag(sample_snapshot: AddressSpace) -> None:
    document = snapshot_to_document(sample_snapshot)
    references = document["references"]

    assert len(references) == 3
    dangling = [ref for ref in references if not ref["target_included"]]
    assert len(dangling) == 1
    assert dangling[0]["target"] == "ns=2;s=Missing"
    assert references[0]["reference_type_name"] == "Organizes"


def test_json_deduplicates_repeated_edges(sample_snapshot: AddressSpace) -> None:
    sample_snapshot.references.append(sample_snapshot.references[0])
    document = snapshot_to_document(sample_snapshot)
    assert len(document["references"]) == 3


def test_json_summary_and_limitations_are_present(sample_snapshot: AddressSpace) -> None:
    document = snapshot_to_document(sample_snapshot)
    summary = document["summary"]
    assert summary["nodes"] == 3
    assert summary["references"] == 3
    assert summary["inaccessible_nodes"] == 1
    assert document["source"]["limitations"] == ["Test limitation note."]
    assert document["inaccessible_nodes"] == [{"node_id": "ns=2;s=Locked", "status": "BadUserAccessDenied"}]


def test_export_json_writes_file(sample_snapshot: AddressSpace, tmp_path) -> None:
    target = tmp_path / "nested" / "space.json"
    result = export_json(sample_snapshot, target)

    assert result.path == target
    assert result.nodes == 3
    assert result.references == 3
    assert result.bytes_written > 0

    reloaded = json.loads(target.read_text(encoding="utf-8"))
    assert reloaded["nodes"]["ns=2;s=Plant"]["display_name"] == "Plant"


def test_json_includes_values_when_requested(sample_snapshot: AddressSpace) -> None:
    sample_snapshot.values_included = True
    document = snapshot_to_document(sample_snapshot)
    value = document["nodes"]["ns=2;s=Temperature"]["value"]
    assert value["value"] == 21.5
    assert value["variant_type"] == "Double"
    assert value["encoding"] == "json"


def test_csv_rows_and_columns(sample_snapshot: AddressSpace, tmp_path) -> None:
    target = tmp_path / "space.csv"
    result = export_csv(sample_snapshot, target)

    assert result.rows == 3
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel

    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 3
    by_id = {row["node_id"]: row for row in rows}
    temperature = by_id["ns=2;s=Temperature"]
    assert temperature["display_name"] == "Temperature"
    assert temperature["access_level_names"] == "CurrentRead;CurrentWrite"
    assert temperature["references_out"] == "0"
    assert temperature["references_in"] == "1"
    assert temperature["value"] == ""  # values excluded
    assert temperature["value_status"] == ""
    assert temperature["value_encoding"] == ""
    assert temperature["unavailable_attributes"] == ""
    assert temperature["description"] == 'Line temperature, in "°C", sampled'

    # A refused Value keeps its StatusCode instead of looking like an excluded value.
    denied = by_id["ns=2;s=Denied"]
    assert denied["value"] == ""
    assert denied["value_status"] == "BadUserAccessDenied"
    assert denied["value_encoding"] == "unavailable"
    assert denied["unavailable_attributes"] == "Value=BadUserAccessDenied"

    plant = by_id["ns=2;s=Plant"]
    assert plant["references_in"] == "1"
    assert plant["references_out"] == "2"


def test_csv_includes_values_when_requested(sample_snapshot: AddressSpace, tmp_path) -> None:
    sample_snapshot.values_included = True
    sample_snapshot.nodes["ns=2;s=Temperature"].value = ValueSnapshot(
        value=[1.0, 2.0], variant_type="Double", status="Good", source_timestamp="2026-01-01T00:00:00.000Z"
    )
    target = tmp_path / "space.csv"
    export_csv(sample_snapshot, target)

    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["node_id"]: row for row in csv.DictReader(handle)}
    temperature = rows["ns=2;s=Temperature"]
    assert temperature["value"] == "[1.0,2.0]"
    assert temperature["value_status"] == "Good"
    assert temperature["value_encoding"] == "json"
    assert temperature["value_variant_type"] == "Double"
    assert temperature["value_source_timestamp"].startswith("2026-01-01")


def test_csv_preserves_encoding_and_every_unavailable_attribute(sample_snapshot: AddressSpace, tmp_path) -> None:
    sample_snapshot.values_included = True
    temperature = sample_snapshot.nodes["ns=2;s=Temperature"]
    temperature.value = ValueSnapshot(
        value={"$type": "ByteString", "base64": "AAE="},
        variant_type="ByteString",
        status="Good",
        encoding="unsupported",
        unsupported=[{"kind": "binary", "detail": "undecoded ExtensionObject"}],
    )
    temperature.unavailable.update(
        {"DataType": "BadAttributeIdInvalid", "AccessLevel": "BadNotReadable"}
    )
    target = tmp_path / "space.csv"
    export_csv(sample_snapshot, target)

    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["node_id"]: row for row in csv.DictReader(handle)}
    row = rows["ns=2;s=Temperature"]

    assert row["value_encoding"] == "unsupported"
    # Sorted Attribute=StatusCode pairs, so the column is stable between runs.
    assert row["unavailable_attributes"] == (
        "AccessLevel=BadNotReadable;DataType=BadAttributeIdInvalid"
    )
