"""Browse primitives: NodeId handling, reference classification and continuation points.

The continuation-point tests use a fake session, so the cancellation/release path
is exercised without a live server.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from asyncua import ua

from opcua_explorer.errors import InvalidNodeIdError
from opcua_explorer.opcua.browse import (
    HIERARCHICAL_FALLBACK_IDS,
    browse_children,
    browse_many,
    collect_hierarchical_reference_type_ids,
    is_forward_hierarchical,
    node_id_to_string,
    parse_node_id,
    reference_to_child,
    to_node_id,
)


def _reference(name: str, *, forward: bool = True, reference_type: int = ua.ObjectIds.Organizes):
    reference = ua.ReferenceDescription()
    reference.ReferenceTypeId = ua.NodeId(reference_type)
    reference.IsForward = forward
    reference.NodeId = ua.NodeId(name, 2)
    reference.BrowseName = ua.QualifiedName(name, 2)
    reference.DisplayName = ua.LocalizedText(name, "en")
    reference.NodeClass = ua.NodeClass.Object
    reference.TypeDefinition = ua.NodeId(ua.ObjectIds.FolderType)
    return reference


def test_parse_node_id_accepts_common_forms() -> None:
    assert parse_node_id("ns=2;i=42").to_string() == "ns=2;i=42"
    assert parse_node_id("  ns=2;s=Temperature ").to_string() == "ns=2;s=Temperature"
    assert parse_node_id("i=85").to_string() == "i=85"


@pytest.mark.parametrize("text", ["", "   ", "not-a-nodeid", "ns=x;i=1"])
def test_parse_node_id_rejects_invalid_input(text: str) -> None:
    with pytest.raises(InvalidNodeIdError) as excinfo:
        parse_node_id(text)
    assert "NodeId" in str(excinfo.value)


def test_to_node_id_and_string_roundtrip() -> None:
    assert to_node_id(85).to_string() == "i=85"
    assert to_node_id("ns=2;s=X").to_string() == "ns=2;s=X"
    node_id = ua.NodeId("X", 2)
    assert to_node_id(node_id) is node_id
    assert node_id_to_string(node_id) == "ns=2;s=X"


def test_reference_to_child_maps_all_fields() -> None:
    child = reference_to_child(_reference("Plant"), reference_type_name="Organizes", type_definition_name="FolderType")
    assert child.node_id == "ns=2;s=Plant"
    assert child.browse_name == "Plant"
    assert child.browse_name_namespace_index == 2
    assert child.display_name == "Plant"
    assert child.node_class == "Object"
    assert child.reference_type == "i=35"
    assert child.reference_type_name == "Organizes"
    assert child.is_forward is True
    assert child.type_definition == "i=61"
    assert child.type_definition_name == "FolderType"


def test_is_forward_hierarchical() -> None:
    hierarchical = frozenset({"i=33", "i=35", "i=47"})
    assert is_forward_hierarchical(_reference("A", reference_type=ua.ObjectIds.Organizes), hierarchical) is True
    assert is_forward_hierarchical(_reference("A", reference_type=ua.ObjectIds.HasTypeDefinition), hierarchical) is False
    assert (
        is_forward_hierarchical(
            _reference("A", forward=False, reference_type=ua.ObjectIds.Organizes), hierarchical
        )
        is False
    )


def test_fallback_reference_ids_contain_structural_types() -> None:
    ids = {ua.NodeId(value).to_string() for value in HIERARCHICAL_FALLBACK_IDS}
    assert "i=33" in ids  # HierarchicalReferences
    assert "i=35" in ids  # Organizes
    assert "i=47" in ids  # HasComponent
    assert "i=46" in ids  # HasProperty
    assert "i=40" not in ids  # HasTypeDefinition is not hierarchical


class PagedSession:
    """Fake UaSession returning fixed pages linked by continuation points."""

    def __init__(self, pages: list[list[ua.ReferenceDescription]], *, delay: float = 0.0) -> None:
        self.pages = pages
        self.delay = delay
        self.released: list[bytes] = []
        self.browse_calls = 0
        self.browse_next_calls = 0

    def _result(self, page: int) -> ua.BrowseResult:
        result = ua.BrowseResult()
        result.StatusCode = ua.StatusCode()
        result.References = list(self.pages[page])
        result.ContinuationPoint = str(page + 1).encode() if page + 1 < len(self.pages) else None
        return result

    async def browse(self, parameters: ua.BrowseParameters) -> list[ua.BrowseResult]:
        self.browse_calls += 1
        return [self._result(0) for _ in parameters.NodesToBrowse]

    async def browse_next(self, parameters: ua.BrowseNextParameters) -> list[ua.BrowseResult]:
        self.browse_next_calls += 1
        if parameters.ReleaseContinuationPoints:
            self.released.extend(parameters.ContinuationPoints)
            return []
        if self.delay:
            await asyncio.sleep(self.delay)
        return [self._result(int(point)) for point in parameters.ContinuationPoints]


async def test_browse_many_follows_continuation_points() -> None:
    session = PagedSession([[_reference("A")], [_reference("B")], [_reference("C")]])
    outcomes = await browse_many(session, ["ns=2;s=Root"], batch_size=1)

    assert len(outcomes) == 1
    assert outcomes[0].ok is True
    assert [reference.BrowseName.Name for reference in outcomes[0].references] == ["A", "B", "C"]
    assert session.browse_calls == 1
    assert session.browse_next_calls == 2
    assert session.released == []


async def test_browse_many_reports_per_node_status() -> None:
    class DeniedSession(PagedSession):
        async def browse(self, parameters: ua.BrowseParameters) -> list[ua.BrowseResult]:
            result = ua.BrowseResult()
            result.StatusCode = ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
            result.References = []
            result.ContinuationPoint = None
            return [result for _ in parameters.NodesToBrowse]

    outcomes = await browse_many(DeniedSession([[]]), ["ns=2;s=Secret"])
    assert outcomes[0].ok is False
    assert outcomes[0].status == "BadUserAccessDenied"
    assert outcomes[0].references == []


async def test_cancelling_browse_releases_continuation_points() -> None:
    session = PagedSession([[_reference("A")], [_reference("B")], [_reference("C")]], delay=5.0)
    task = asyncio.create_task(browse_many(session, ["ns=2;s=Root"], batch_size=1))

    await asyncio.sleep(0.05)
    assert session.browse_next_calls == 1  # blocked inside BrowseNext
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.released, "an outstanding continuation point must be released on cancellation"
    assert session.released[0] == b"1"


async def test_collect_hierarchical_reference_types_falls_back_on_error() -> None:
    class BrokenSession:
        async def browse(self, parameters):  # noqa: ANN001 - test double
            raise RuntimeError("server does not support browsing types")

    ids, source = await collect_hierarchical_reference_type_ids(BrokenSession())
    assert source == "fallback"
    assert "i=33" in ids


class ChildrenSession:
    """Fake session for browse_children: fixed references, resolvable type names."""

    _TYPE_NAMES = {
        int(ua.ObjectIds.HierarchicalReferences): "HierarchicalReferences",
        int(ua.ObjectIds.Organizes): "Organizes",
        int(ua.ObjectIds.HasComponent): "HasComponent",
    }

    def __init__(self, references: list[ua.ReferenceDescription]) -> None:
        self.references = list(references)
        self.browse_calls = 0

    async def browse(self, parameters: ua.BrowseParameters) -> list[ua.BrowseResult]:
        self.browse_calls += 1
        result = ua.BrowseResult()
        result.StatusCode = ua.StatusCode()
        result.References = list(self.references)
        result.ContinuationPoint = None
        return [result for _ in parameters.NodesToBrowse]

    async def read(self, parameters: ua.ReadParameters) -> list[ua.DataValue]:
        results = []
        for read_value_id in parameters.NodesToRead:
            identifier = int(getattr(read_value_id.NodeId, "Identifier", 0) or 0)
            name = self._TYPE_NAMES.get(identifier, "Reference")
            results.append(ua.DataValue(ua.Variant(ua.QualifiedName(name, 0), ua.VariantType.QualifiedName)))
        return results


async def test_browse_children_merges_the_same_reference_returned_twice() -> None:
    reference = _reference("Output1", reference_type=ua.ObjectIds.HasComponent)
    session = ChildrenSession([reference, _reference("Output1", reference_type=ua.ObjectIds.HasComponent)])

    children = await browse_children(session, "ns=2;s=Outputs")

    assert [child.node_id for child in children] == ["ns=2;s=Output1"]
    # Identical reference type: nothing to explain in the tooltip.
    assert children[0].additional_reference_types == []


async def test_browse_children_merges_the_same_node_over_different_reference_types() -> None:
    session = ChildrenSession(
        [
            _reference("Output1", reference_type=ua.ObjectIds.Organizes),
            _reference("Output1", reference_type=ua.ObjectIds.HasComponent),
        ]
    )

    children = await browse_children(session, "ns=2;s=Outputs")

    assert len(children) == 1
    assert children[0].node_id == "ns=2;s=Output1"
    assert children[0].reference_type == ua.NodeId(ua.ObjectIds.Organizes).to_string()
    assert children[0].reference_type_name == "Organizes"
    # The merged-away type stays available for the GUI tooltip.
    assert children[0].additional_reference_types == ["HasComponent"]


async def test_browse_children_keeps_one_row_when_a_node_id_is_reused(caplog) -> None:
    first = _reference("Output2", reference_type=ua.ObjectIds.HasComponent)
    second = _reference("Output3", reference_type=ua.ObjectIds.HasComponent)
    second.NodeId = first.NodeId  # the server points two different names at one node
    session = ChildrenSession([first, second])

    with caplog.at_level(logging.WARNING, logger="opcua_explorer.opcua.browse"):
        children = await browse_children(session, "ns=2;s=Outputs")

    assert len(children) == 1
    assert children[0].display_name == "Output2"
    assert any("different names" in record.getMessage() for record in caplog.records)


async def test_browse_children_keeps_distinct_nodes_with_the_same_name() -> None:
    first = _reference("Output2", reference_type=ua.ObjectIds.HasComponent)
    second = _reference("Output2", reference_type=ua.ObjectIds.HasComponent)
    second.NodeId = ua.NodeId("Output2-other", 2)
    session = ChildrenSession([first, second])

    children = await browse_children(session, "ns=2;s=Outputs")

    assert [child.node_id for child in children] == ["ns=2;s=Output2", "ns=2;s=Output2-other"]
