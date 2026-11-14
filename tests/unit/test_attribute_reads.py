"""Bulk attribute reads must survive values the client cannot decode.

The regression this covers: a server sent a structure whose enumeration field
carried ``0xFFFFFF``, so asyncua raised ``ValueError: 16777215 is not a valid
MessageSecurityMode`` while decoding a Read response. Because one Read response
is decoded as a whole, a single such value aborted the download of 1238 nodes.
The readers now bisect the chunk and record only the offending value as
unavailable.
"""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_explorer.models import NodeRecord, TraversalConfig
from opcua_explorer.opcua.attributes import (
    DECODE_ERROR_STATUS,
    AttributeSpec,
    read_attributes_bulk,
    read_values_bulk,
)
from opcua_explorer.opcua.traversal import AddressSpaceDownloader

BAD_MESSAGE = "16777215 is not a valid MessageSecurityMode"


class _ValueSession:
    """Fake session that fails to decode any Read containing a 'poisoned' node."""

    def __init__(self, poison: set[str] | None = None, *, error: BaseException | None = None, truncate: int = 0):
        self.poison = poison or set()
        self.error = error
        self.truncate = truncate
        self.calls: list[list[str]] = []

    async def read(self, params: ua.ReadParameters) -> list[ua.DataValue]:
        identifiers = [read_value.NodeId.to_string() for read_value in params.NodesToRead]
        self.calls.append(identifiers)
        if self.error is not None:
            raise self.error
        if any(identifier in self.poison for identifier in identifiers):
            raise ValueError(BAD_MESSAGE)
        values = [ua.DataValue(ua.Variant(int(identifier.rsplit("i=", 1)[1]), ua.VariantType.Int32)) for identifier in identifiers]
        if self.truncate:
            return values[: self.truncate]
        return values


def _ids(count: int) -> list[str]:
    return [f"ns=2;i={index}" for index in range(1, count + 1)]


async def test_read_values_bulk_isolates_undecodable_value() -> None:
    identifiers = _ids(8)
    session = _ValueSession(poison={"ns=2;i=5"})

    snapshots = await read_values_bulk(session, identifiers, chunk_size=8)

    assert sorted(snapshots) == sorted(identifiers)
    poisoned = snapshots["ns=2;i=5"]
    assert poisoned.encoding == "unavailable"
    assert poisoned.value is None
    assert poisoned.status.startswith(DECODE_ERROR_STATUS)
    assert "MessageSecurityMode" in poisoned.status

    for identifier in identifiers:
        if identifier == "ns=2;i=5":
            continue
        snapshot = snapshots[identifier]
        assert snapshot.encoding == "json"
        assert snapshot.value == int(identifier.rsplit("i=", 1)[1])

    # Bisecting 8 nodes costs a handful of extra requests, never a request per node.
    assert len(session.calls) <= 10


async def test_read_values_bulk_keeps_all_values_for_multiple_bad_nodes() -> None:
    identifiers = _ids(6)
    session = _ValueSession(poison={"ns=2;i=2", "ns=2;i=5"})

    snapshots = await read_values_bulk(session, identifiers, chunk_size=6)

    undecodable = [item for item, snapshot in snapshots.items() if snapshot.encoding == "unavailable"]
    assert undecodable == ["ns=2;i=2", "ns=2;i=5"]
    assert all(
        snapshot.value == int(item.rsplit("i=", 1)[1])
        for item, snapshot in snapshots.items()
        if item not in undecodable
    )


@pytest.mark.parametrize("error", [ConnectionError("link down"), ua.UaError("BadServiceUnsupported")])
async def test_read_values_bulk_does_not_swallow_connection_errors(error: BaseException) -> None:
    session = _ValueSession(error=error)

    with pytest.raises(type(error)):
        await read_values_bulk(session, _ids(4), chunk_size=4)


async def test_read_values_bulk_marks_truncated_response_unavailable() -> None:
    session = _ValueSession(truncate=1)

    snapshots = await read_values_bulk(session, _ids(3), chunk_size=3)

    assert snapshots["ns=2;i=1"].value == 1
    assert snapshots["ns=2;i=2"].status == "BadNoResult"
    assert snapshots["ns=2;i=3"].status == "BadNoResult"


class _AttributeSession:
    """Fake session failing only for the Value attribute of one node."""

    def __init__(self, poison: set[str]):
        self.poison = poison
        self.calls: list[list[tuple[str, ua.AttributeIds]]] = []

    async def read(self, params: ua.ReadParameters) -> list[ua.DataValue]:
        requested = [
            (read_value.NodeId.to_string(), ua.AttributeIds(int(read_value.AttributeId)))
            for read_value in params.NodesToRead
        ]
        self.calls.append(requested)
        if any(node_id in self.poison and attribute == ua.AttributeIds.Value for node_id, attribute in requested):
            raise ValueError(BAD_MESSAGE)
        results: list[ua.DataValue] = []
        for node_id, attribute in requested:
            if attribute == ua.AttributeIds.BrowseName:
                results.append(ua.DataValue(ua.Variant(ua.QualifiedName(node_id, 2), ua.VariantType.QualifiedName)))
            else:
                results.append(ua.DataValue(ua.Variant(ua.NodeId(85), ua.VariantType.NodeId)))
        return results


async def test_read_attributes_bulk_confines_failure_to_the_failing_attribute() -> None:
    session = _AttributeSession(poison={"ns=2;i=1"})
    requests = [
        ("ns=2;i=1", AttributeSpec(ua.AttributeIds.BrowseName, "BrowseName")),
        ("ns=2;i=1", AttributeSpec(ua.AttributeIds.Value, "Value")),
    ]

    results = await read_attributes_bulk(session, requests)

    browse_name = results[("ns=2;i=1", "BrowseName")]
    assert browse_name.ok is True
    assert browse_name.value == ua.QualifiedName("ns=2;i=1", 2)

    value = results[("ns=2;i=1", "Value")]
    assert value.ok is False
    assert value.status.startswith(DECODE_ERROR_STATUS)


def test_undecodable_values_are_reported_as_a_limitation() -> None:
    downloader = AddressSpaceDownloader(
        session=None,
        namespaces=[],
        config=TraversalConfig(include_values=True),
    )
    ok = NodeRecord(node_id="ns=2;i=1", node_class="Variable", display_name="Ok")
    broken = NodeRecord(node_id="ns=2;i=2", node_class="Variable", display_name="Broken")
    broken.unavailable["Value"] = f"{DECODE_ERROR_STATUS}: ValueError: {BAD_MESSAGE}"
    downloader._snapshot.nodes = {ok.node_id: ok, broken.node_id: broken}

    notes = downloader._value_limitations()

    assert any("1 value(s) could not be decoded" in note for note in notes)
    assert any("rest of the download is unaffected" in note for note in notes)
    assert downloader._undecodable_value_ids() == ["ns=2;i=2"]
