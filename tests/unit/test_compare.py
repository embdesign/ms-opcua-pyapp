"""Address-space comparison: identity rules, tolerances and reporting.

All snapshots are hand-built, so these tests are fast, network-free and cover
cases a real device pair cannot produce on demand (renumbered NodeIds, partial
downloads, refused attributes).
"""

from __future__ import annotations

from typing import Any

from opcua_explorer.compare import (
    COMPARISON_FORMAT,
    COMPARISON_FORMAT_VERSION,
    KIND_CHANGED,
    KIND_EXTRA_IN_CANDIDATE,
    KIND_MISSING_IN_CANDIDATE,
    KIND_NOT_COMPARABLE,
    ComparisonConfig,
    compare_address_spaces,
    qualified_key,
)
from opcua_explorer.models import (
    AddressSpace,
    ConnectionInfo,
    Edge,
    NamespaceEntry,
    NodeRecord,
    TraversalConfig,
    ValueSnapshot,
)

NS0 = "http://opcfoundation.org/UA/"
PLANT = "urn:example:plant"


def _namespaces(*uris: str) -> list[NamespaceEntry]:
    return [NamespaceEntry(index, uri) for index, uri in enumerate(uris)]


def _node(
    node_id: str,
    name: str = "Temperature",
    *,
    namespace_uri: str = PLANT,
    browse_index: int = 1,
    **fields: Any,
) -> NodeRecord:
    values: dict[str, Any] = {
        "node_class": "Variable",
        "data_type": "i=11",
        "value_rank": -1,
        "array_dimensions": [],
        "access_level_names": ["CurrentRead"],
        "user_access_level_names": ["CurrentRead"],
        "type_definition": "i=63",
        "display_name": name,
    }
    values.update(fields)
    return NodeRecord(
        node_id=node_id,
        browse_name=name,
        browse_name_namespace_index=browse_index,
        namespace_uri=namespace_uri,
        **values,
    )


def _snapshot(
    endpoint: str,
    nodes: list[NodeRecord],
    *,
    roots: list[str] | None = None,
    references: list[Edge] | None = None,
    namespaces: list[NamespaceEntry] | None = None,
) -> AddressSpace:
    return AddressSpace(
        endpoint_url=endpoint,
        roots=list(roots or []),
        nodes={record.node_id: record for record in nodes},
        references=list(references or []),
        namespaces=list(namespaces or _namespaces(NS0, PLANT)),
    )


def _base_nodes() -> list[NodeRecord]:
    return [
        _node("ns=1;i=5", "Plant", node_class="Object", data_type=None, type_definition="i=58"),
        _node("ns=1;i=7", "Temperature"),
        _node("ns=1;i=8", "Pressure"),
    ]


# -- identity --------------------------------------------------------------


def test_qualified_key_uses_the_namespace_uri() -> None:
    namespaces = _namespaces(NS0, PLANT)
    uri_of = lambda index: next((entry.uri for entry in namespaces if entry.index == index), None)  # noqa: E731
    assert qualified_key("i=85", uri_of) == f"{NS0}|i=85"
    assert qualified_key("ns=1;s=Temperature", uri_of) == f"{PLANT}|s=Temperature"
    assert qualified_key("nsu=urn:inline;i=5", uri_of) == "urn:inline|i=5"
    assert qualified_key(None, uri_of) == ""


def test_identical_snapshots_are_equal() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes())

    result = compare_address_spaces(reference, candidate)

    assert result.is_equal
    assert not result.has_differences
    assert result.matched == 3
    assert result.ignored == 0
    assert result.differences == []
    assert "no difference found" in result.summary_text()


def test_namespace_index_is_not_part_of_identity() -> None:
    reference = _snapshot(
        "opc.tcp://old:4840",
        [_node("ns=2;i=7")],
        namespaces=_namespaces(NS0, "urn:example:other", PLANT),
    )
    # Same URI table, different index assignment: no namespace difference at all.
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;i=7")],
        namespaces=_namespaces(NS0, PLANT, "urn:example:other"),
    )

    result = compare_address_spaces(reference, candidate)

    assert result.is_equal, result.summary_text()
    assert result.matched == 1
    assert result.namespace_differences == []


def test_changed_identifier_matches_by_browse_path() -> None:
    reference = _snapshot(
        "opc.tcp://old:4840",
        [_node("ns=1;i=5", "Plant", node_class="Object", data_type=None), _node("ns=1;i=7", "Temperature")],
        roots=["ns=1;i=5"],
        references=[Edge("ns=1;i=5", "ns=1;i=7", "i=35", "Organizes")],
    )
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;i=5", "Plant", node_class="Object", data_type=None), _node("ns=1;i=107", "Temperature")],
        roots=["ns=1;i=5"],
        references=[Edge("ns=1;i=5", "ns=1;i=107", "i=35", "Organizes")],
    )

    result = compare_address_spaces(reference, candidate)

    assert result.is_equal, result.summary_text()
    assert result.matched == 2
    assert result.missing_in_candidate == []
    assert result.extra_in_candidate == []


def test_path_matching_needs_an_unambiguous_node() -> None:
    reference = _snapshot(
        "opc.tcp://old:4840",
        [
            _node("ns=1;i=5", "Plant", node_class="Object", data_type=None),
            _node("ns=1;i=7", "Temperature"),
            _node("ns=1;i=9", "Temperature"),
        ],
        roots=["ns=1;i=5"],
        references=[
            Edge("ns=1;i=5", "ns=1;i=7", "i=35", "Organizes"),
            Edge("ns=1;i=5", "ns=1;i=9", "i=35", "Organizes"),
        ],
    )
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [
            _node("ns=1;i=5", "Plant", node_class="Object", data_type=None),
            _node("ns=1;i=107", "Temperature"),
            _node("ns=1;i=109", "Temperature"),
        ],
        roots=["ns=1;i=5"],
        references=[
            Edge("ns=1;i=5", "ns=1;i=107", "i=35", "Organizes"),
            Edge("ns=1;i=5", "ns=1;i=109", "i=35", "Organizes"),
        ],
    )

    result = compare_address_spaces(reference, candidate)

    # Two nodes share the same path, so neither identifier change may be guessed.
    assert result.matched == 1
    assert len(result.missing_in_candidate) == 2
    assert len(result.extra_in_candidate) == 2


def test_node_only_in_reference_is_missing() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes()[:2])

    result = compare_address_spaces(reference, candidate)

    assert len(result.missing_in_candidate) == 1
    difference = result.missing_in_candidate[0]
    assert difference.kind == KIND_MISSING_IN_CANDIDATE
    assert difference.reference_node_id == "ns=1;i=8"
    assert difference.candidate_node_id is None
    assert difference.browse_path == "Pressure"
    assert not result.is_equal


def test_node_only_in_candidate_is_extra() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes()[:2])
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes())

    result = compare_address_spaces(reference, candidate)

    assert len(result.extra_in_candidate) == 1
    assert result.extra_in_candidate[0].kind == KIND_EXTRA_IN_CANDIDATE
    assert result.extra_in_candidate[0].candidate_node_id == "ns=1;i=8"


# -- attributes ------------------------------------------------------------


def test_changed_node_class_is_reported() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7")])
    candidate = _snapshot("opc.tcp://new:4840", [_node("ns=1;i=7", node_class="Object", data_type=None)])

    result = compare_address_spaces(reference, candidate)

    assert len(result.changed) == 1
    difference = result.changed[0]
    assert difference.kind == KIND_CHANGED
    assert [item.attribute for item in difference.attributes] == ["NodeClass", "DataType"]
    assert difference.attributes[0].reference == "Variable"
    assert difference.attributes[0].candidate == "Object"


def test_access_level_difference_is_reported() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7")])
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;i=7", access_level_names=["CurrentRead", "CurrentWrite"])],
    )

    result = compare_address_spaces(reference, candidate)

    assert len(result.changed) == 1
    assert [item.attribute for item in result.changed[0].attributes] == ["AccessLevel"]


def test_data_type_is_compared_by_namespace_uri() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7", data_type="ns=1;i=3005")])
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=2;i=7", data_type="ns=2;i=3005")],
        namespaces=_namespaces(NS0, "urn:example:other", PLANT),
    )

    result = compare_address_spaces(reference, candidate)

    assert result.missing_in_candidate == []
    assert result.extra_in_candidate == []
    assert result.changed == []


def test_strict_tolerance_compares_display_text() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7", display_name="Temperature")])
    candidate = _snapshot("opc.tcp://new:4840", [_node("ns=1;i=7", display_name="Temperatur")])

    functional = compare_address_spaces(reference, candidate)
    strict = compare_address_spaces(reference, candidate, ComparisonConfig(tolerance="strict"))

    assert functional.is_equal
    assert [item.attribute for item in strict.changed[0].attributes] == ["DisplayName"]


def test_unknown_tolerance_falls_back_to_functional() -> None:
    config = ComparisonConfig(tolerance="nonsense").normalised()
    assert config.tolerance == "functional"


def test_attribute_unavailable_on_one_side_is_not_comparable() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7")])
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;i=7", access_level_names=[], unavailable={"AccessLevel": "BadAttributeIdInvalid"})],
    )

    result = compare_address_spaces(reference, candidate)

    assert result.changed == []
    assert len(result.not_comparable) == 1
    difference = result.not_comparable[0]
    assert difference.kind == KIND_NOT_COMPARABLE
    assert difference.attributes[0].attribute == "AccessLevel"
    assert "candidate" in difference.attributes[0].note


def test_attribute_unavailable_on_both_sides_is_not_a_difference() -> None:
    unavailable = {"AccessLevel": "BadAttributeIdInvalid"}
    reference = _snapshot(
        "opc.tcp://old:4840",
        [_node("ns=1;i=7", access_level_names=[], unavailable=unavailable)],
    )
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;i=7", access_level_names=[], unavailable=unavailable)],
    )

    result = compare_address_spaces(reference, candidate)

    assert result.is_equal, result.summary_text()


# -- values ----------------------------------------------------------------


def _value(value: Any, *, variant_type: str = "Double", status: str = "Good") -> ValueSnapshot:
    return ValueSnapshot(value=value, status=status, variant_type=variant_type)


def test_values_are_not_compared_unless_listed() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7", value=_value(20.5))])
    candidate = _snapshot("opc.tcp://new:4840", [_node("ns=1;i=7", value=_value(21.5))])

    result = compare_address_spaces(reference, candidate)

    assert result.is_equal, result.summary_text()


def test_listed_value_payload_difference_is_reported() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7", value=_value(20.5))])
    candidate = _snapshot("opc.tcp://new:4840", [_node("ns=1;i=7", value=_value(21.5))])
    config = ComparisonConfig(value_node_ids=frozenset({"ns=1;i=7"}))

    result = compare_address_spaces(reference, candidate, config)

    assert len(result.changed) == 1
    assert [item.attribute for item in result.changed[0].attributes] == ["Value.Payload"]


def test_float_tolerance_applies_to_value_payload() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7", value=_value(20.5))])
    candidate = _snapshot("opc.tcp://new:4840", [_node("ns=1;i=7", value=_value(20.6))])
    config = ComparisonConfig(value_node_ids=frozenset({"ns=1;i=7"}), float_tolerance=0.15)

    result = compare_address_spaces(reference, candidate, config)

    assert result.is_equal, result.summary_text()


def test_value_type_difference_is_reported_when_listed() -> None:
    reference = _snapshot("opc.tcp://old:4840", [_node("ns=1;i=7", value=_value(20.5))])
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;i=7", value=_value("20.5", variant_type="String"))],
    )
    config = ComparisonConfig(value_node_ids=frozenset({"ns=1;i=7"}))

    result = compare_address_spaces(reference, candidate, config)

    assert [item.attribute for item in result.changed[0].attributes] == ["Value.Type", "Value.Payload"]


# -- expectations and partial snapshots ------------------------------------


def test_ignore_list_skips_expected_differences() -> None:
    reference = _snapshot(
        "opc.tcp://old:4840",
        [_node("ns=1;i=7"), _node("ns=1;i=8", "Serial", data_type="i=12")],
    )
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;i=7"), _node("ns=1;i=8", "Serial", data_type="i=12", value=_value("A-42"))],
        namespaces=_namespaces(NS0, PLANT),
    )
    config = ComparisonConfig(ignore_node_ids=frozenset({"ns=1;i=8"}))

    result = compare_address_spaces(reference, candidate, config)

    assert result.is_equal, result.summary_text()
    assert result.ignored == 1
    assert result.matched == 1


def test_partial_snapshot_produces_warnings() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    reference.limits_hit = ["max_nodes"]
    reference.cancelled = True
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes())
    candidate.inaccessible = {"ns=1;i=8": "BadNodeIdUnknown"}

    result = compare_address_spaces(reference, candidate)

    joined = " ".join(result.warnings)
    assert result.has_warnings
    assert "max_nodes" in joined
    assert "cancelled" in joined
    assert "could not browse 1 node" in joined


def test_inaccessible_node_is_not_comparable() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes())
    candidate.inaccessible = {"ns=1;i=8": "BadNodeIdUnknown"}

    result = compare_address_spaces(reference, candidate)

    assert len(result.not_comparable) == 1
    difference = result.not_comparable[0]
    assert difference.reference_node_id == "ns=1;i=8"
    assert "BadNodeIdUnknown" in difference.note
    assert result.matched == 2


def test_different_identity_produces_a_warning() -> None:
    def connection(endpoint: str, auth: str) -> ConnectionInfo:
        return ConnectionInfo(
            endpoint_url=endpoint,
            security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#None",
            security_policy_label="None",
            security_mode="None",
            auth_mode=auth,
        )

    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    reference.connection = connection("opc.tcp://old:4840", "Anonymous")
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes())
    candidate.connection = connection("opc.tcp://new:4840", "UserName")

    result = compare_address_spaces(reference, candidate)

    assert result.is_equal
    assert any("different identities" in warning for warning in result.warnings)


# -- namespaces and references ---------------------------------------------


def test_namespace_only_on_one_side_is_reported() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    candidate = _snapshot(
        "opc.tcp://new:4840",
        _base_nodes(),
        namespaces=[NamespaceEntry(0, NS0)],
    )

    result = compare_address_spaces(reference, candidate)

    assert result.namespace_differences == [
        {"uri": PLANT, "reference_index": 1, "candidate_index": None}
    ]
    assert any("namespace URI" in warning for warning in result.warnings)


def test_reference_differences_when_enabled() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    candidate = _snapshot(
        "opc.tcp://new:4840",
        _base_nodes(),
    )
    candidate.references = [
        Edge("ns=1;i=5", "ns=1;i=7", "i=47", "HasComponent"),
        Edge("ns=1;i=5", "ns=1;i=8", "i=47", "HasComponent"),
    ]

    without = compare_address_spaces(reference, candidate)
    with_references = compare_address_spaces(
        reference, candidate, ComparisonConfig(compare_references=True)
    )

    assert without.reference_differences == []
    assert len(with_references.reference_differences) == 2
    first = with_references.reference_differences[0]
    assert first.kind == KIND_EXTRA_IN_CANDIDATE
    assert first.reference_type_name == "HasComponent"
    assert first.source == f"{PLANT}|i=5"
    assert first.target == f"{PLANT}|i=7"


# -- truncation and documents ----------------------------------------------


def test_difference_list_is_truncated_but_counts_stay_complete() -> None:
    reference_nodes = [_node(f"ns=1;i={index}", f"N{index}") for index in range(1, 6)]
    candidate_nodes = [
        _node(f"ns=1;i={index}", f"N{index}", node_class="Object", data_type=None)
        for index in range(1, 6)
    ]
    config = ComparisonConfig(max_differences=2)

    result = compare_address_spaces(
        _snapshot("opc.tcp://old:4840", reference_nodes),
        _snapshot("opc.tcp://new:4840", candidate_nodes),
        config,
    )

    assert len(result.changed) == 2
    assert result.truncated_differences == 3
    assert result.difference_count == 5
    assert result.has_differences
    assert any("were not listed" in warning for warning in result.warnings)


def test_summary_document_is_versioned() -> None:
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes()[:2])

    document = compare_address_spaces(reference, candidate).to_dict()

    assert document["format"] == COMPARISON_FORMAT
    assert document["version"] == COMPARISON_FORMAT_VERSION
    assert document["counts"][KIND_MISSING_IN_CANDIDATE] == 1
    assert document["limitations"]
    assert document["differences"][0]["kind"] == KIND_MISSING_IN_CANDIDATE
    assert document["reference"]["nodes"] == 3
    assert document["candidate"]["nodes"] == 2


# -- device attributes and references that leave the download --------------


def _device_snapshot(endpoint: str, *, write_mask: str, include: bool = True) -> AddressSpace:
    node = _node("ns=1;i=7", device_attributes={"WriteMask": write_mask, "Historizing": "False"})
    snapshot = _snapshot(endpoint, [*_base_nodes()[:1], node])
    snapshot.config = TraversalConfig(include_device_attributes=include)
    return snapshot


def test_device_attributes_need_the_device_tolerance() -> None:
    """Write masks and historizing are invisible to functional/strict by contract."""
    reference = _device_snapshot("opc.tcp://old:4840", write_mask="CurrentWrite")
    candidate = _device_snapshot("opc.tcp://new:4840", write_mask="CurrentRead")

    functional = compare_address_spaces(
        reference, candidate, ComparisonConfig(tolerance="functional")
    )
    strict = compare_address_spaces(reference, candidate, ComparisonConfig(tolerance="strict"))
    device = compare_address_spaces(reference, candidate, ComparisonConfig(tolerance="device"))

    assert functional.changed == [], functional.summary_text()
    assert strict.changed == [], strict.summary_text()
    assert len(device.changed) == 1
    assert device.device_attributes_compared is True
    attributes = {item.attribute for item in device.changed[0].attributes}
    assert "WriteMask" in attributes
    assert device.changed[0].attributes[0].reference != device.changed[0].attributes[0].candidate


def test_the_device_tolerance_says_when_a_snapshot_lacks_device_attributes() -> None:
    """A snapshot without them must not look equal — it was not looked at."""
    reference = _device_snapshot("opc.tcp://old:4840", write_mask="CurrentWrite")
    candidate = _device_snapshot("opc.tcp://new:4840", write_mask="CurrentRead", include=False)

    result = compare_address_spaces(reference, candidate, ComparisonConfig(tolerance="device"))

    assert result.device_attributes_compared is False
    assert result.changed == []
    assert any("downloaded without them" in warning for warning in result.warnings)
    assert result.to_dict()["device_attributes_compared"] is False


def test_reference_edges_outside_the_download_are_counted_not_reported_as_differences() -> None:
    """A reference to a node outside the subtree is not evidence of a difference."""
    reference = _snapshot("opc.tcp://old:4840", _base_nodes())
    reference.references = [Edge("ns=1;i=5", "ns=1;i=999", "i=47", "HasComponent")]
    candidate = _snapshot("opc.tcp://new:4840", _base_nodes())

    result = compare_address_spaces(reference, candidate, ComparisonConfig(compare_references=True))

    assert result.reference_differences == []
    assert result.reference_edges_not_compared == 1
    assert any("outside that download" in warning for warning in result.warnings)
    assert "were not compared" in result.summary_text()
    assert result.to_dict()["reference_edges_not_compared"] == 1
