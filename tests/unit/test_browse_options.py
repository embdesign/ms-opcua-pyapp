"""Unit tests for the address-space browse filters (no server)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from asyncua import ua

from opcua_explorer.models import BrowseOptions
from opcua_explorer.opcua.browse import browse_children


def _reference(target: str, reference_type: int = ua.ObjectIds.Organizes) -> ua.ReferenceDescription:
    reference = ua.ReferenceDescription()
    reference.NodeId = ua.NodeId.from_string(target)
    reference.BrowseName = ua.QualifiedName("Child", 2)
    reference.DisplayName = ua.LocalizedText(Text="Child")
    reference.ReferenceTypeId = ua.NodeId(reference_type)
    reference.TypeDefinition = ua.NodeId(ua.ObjectIds.BaseObjectType)
    reference.NodeClass = ua.NodeClass.Object
    reference.IsForward = True
    return reference


class _FakeSession:
    def __init__(self, references: list[ua.ReferenceDescription] | None = None) -> None:
        self.requests: list[ua.BrowseParameters] = []
        self._references = references if references is not None else [_reference("ns=2;s=Child")]

    async def browse(self, params: ua.BrowseParameters) -> list[ua.BrowseResult]:
        self.requests.append(params)
        result = ua.BrowseResult()
        result.StatusCode = ua.StatusCode(ua.StatusCodes.Good)
        result.ContinuationPoint = b""
        result.References = list(self._references)
        return [result]

    async def browse_next(self, params: ua.BrowseNextParameters) -> list[ua.BrowseResult]:  # pragma: no cover
        raise AssertionError("no continuation point was requested")

    async def read(self, params: ua.ReadParameters) -> list[ua.DataValue]:  # pragma: no cover - type names
        results = []
        for _read_value in params.NodesToRead:
            data_value = ua.DataValue(ua.Variant("Organizes", ua.VariantType.LocalizedText))
            data_value.StatusCode = ua.StatusCode(ua.StatusCodes.Good)
            data_value.SourceTimestamp = datetime(2026, 11, 18, tzinfo=UTC)
            results.append(data_value)
        return results


def _description(session: _FakeSession) -> ua.BrowseDescription:
    return session.requests[0].NodesToBrowse[0]


def test_options_normalise_defaults_and_clamp() -> None:
    messy = BrowseOptions(
        reference_type=" i=47 ", include_subtypes=0, direction="Sideways", node_class_mask=-4  # type: ignore[arg-type]
    ).normalised()

    assert messy == BrowseOptions(reference_type="i=47", include_subtypes=False, direction="Forward", node_class_mask=0)
    assert BrowseOptions().is_default() is True
    assert BrowseOptions(reference_type="i=35").is_default() is False


def test_option_label_names_the_filter() -> None:
    assert BrowseOptions().label() == "Hierarchical references, Forward"
    assert BrowseOptions(reference_type="i=35", include_subtypes=False, node_class_mask=2).label() == (
        "Organizes, Forward, no subtypes, Variable"
    )
    assert BrowseOptions(reference_type="ns=2;i=99").label().startswith("ns=2;i=99")


def test_option_document_is_json_safe() -> None:
    assert BrowseOptions(reference_type="i=35", direction="Inverse").to_dict() == {
        "reference_type": "i=35",
        "include_subtypes": True,
        "direction": "Inverse",
        "node_class_mask": 0,
    }


async def test_the_default_browse_is_unchanged() -> None:
    session = _FakeSession()

    children = await browse_children(session, "i=85")

    description = _description(session)
    assert description.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    assert description.BrowseDirection == ua.BrowseDirection.Forward
    assert description.IncludeSubtypes is True
    assert description.NodeClassMask == 0
    assert [child.node_id for child in children] == ["ns=2;s=Child"]


async def test_options_are_mapped_onto_the_browse_description() -> None:
    session = _FakeSession()
    options = BrowseOptions(reference_type="i=47", include_subtypes=False, direction="Both", node_class_mask=2)

    await browse_children(session, "i=85", options=options)

    description = _description(session)
    assert description.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasComponent)
    assert description.BrowseDirection == ua.BrowseDirection.Both
    assert description.IncludeSubtypes is False
    assert description.NodeClassMask == 2


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Forward", ua.BrowseDirection.Forward),
        ("Inverse", ua.BrowseDirection.Inverse),
        ("Both", ua.BrowseDirection.Both),
    ],
)
async def test_each_direction_reaches_the_request(name: str, expected: ua.BrowseDirection) -> None:
    session = _FakeSession()

    await browse_children(session, "i=85", options=BrowseOptions(direction=name))

    assert _description(session).BrowseDirection is expected


async def test_the_follow_subtypes_flag_still_applies_without_options() -> None:
    session = _FakeSession()

    await browse_children(session, "i=85", follow_subtypes=False)

    assert _description(session).IncludeSubtypes is False


async def test_child_results_still_merge_duplicates() -> None:
    session = _FakeSession([_reference("ns=2;s=Child"), _reference("ns=2;s=Child", ua.ObjectIds.HasComponent)])

    children = await browse_children(session, "i=85")

    assert len(children) == 1
    session_refs: Any = children[0].additional_reference_types
    assert "i=47" in session_refs
