"""Unit tests for browse-path resolution (TranslateBrowsePathsToNodeIds, no server required)."""

from __future__ import annotations

from typing import Any

import pytest
from asyncua import ua

from opcua_explorer.opcua.path_support import (
    PathRequest,
    PathResolutionError,
    parse_browse_name,
    resolve_path,
    resolve_paths,
)

FULL = 0xFFFFFFFF


class FakeSession:
    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.batch_sizes: list[int] = []
        self.paths: list[ua.BrowsePath] = []

    async def translate_browsepaths_to_nodeids(self, browse_paths: list[ua.BrowsePath]) -> list[Any]:
        self.batch_sizes.append(len(browse_paths))
        self.paths.extend(browse_paths)
        return [self.responder(path) for path in browse_paths]


class FakeClient:
    """A high-level client proxy, shaped like ``asyncua.Client``."""

    def __init__(self, session: Any) -> None:
        self.uaclient = type("UaClientProxy", (), {"session": session})()


def _target(node_id: str, remaining: int = FULL) -> ua.BrowsePathTarget:
    node = ua.NodeId.from_string(node_id)
    return ua.BrowsePathTarget(
        TargetId=ua.ExpandedNodeId(node.Identifier, node.NamespaceIndex, node.NodeIdType),
        RemainingPathIndex=remaining,
    )


def _result(*targets: ua.BrowsePathTarget, status: int = ua.StatusCodes.Good) -> Any:
    return ua.BrowsePathResult(StatusCode=ua.StatusCode(status), Targets=list(targets))


def _name(path: ua.BrowsePath) -> str:
    return "".join(
        f"{element.TargetName.NamespaceIndex}:{element.TargetName.Name}/"
        for element in path.RelativePath.Elements
    )


def test_browse_names_accept_an_optional_namespace_index():
    assert parse_browse_name("Temperature").Name == "Temperature"
    assert parse_browse_name("Temperature").NamespaceIndex == 0
    assert parse_browse_name("2:Temperature").NamespaceIndex == 2
    assert parse_browse_name(" 2:Temperature ").Name == "Temperature"
    with pytest.raises(ValueError, match="must not be empty"):
        parse_browse_name("   ")


async def test_a_path_resolves_to_one_node_id():
    session = FakeSession(lambda path: _result(_target("ns=2;s=Speed")))
    client = FakeClient(session)

    resolutions = await resolve_paths(client, [PathRequest("i=85", ["2:Line1", "2:Motor", "2:Speed"])])

    assert resolutions[0].ok
    assert resolutions[0].node_id == "ns=2;s=Speed"
    assert resolutions[0].status == "Good"
    assert session.batch_sizes == [1]
    element = session.paths[0].RelativePath.Elements[0]
    assert element.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    assert element.IsInverse is False
    assert element.IncludeSubtypes is True


async def test_an_unresolved_path_reports_the_status_code():
    session = FakeSession(lambda path: _result(status=ua.StatusCodes.BadNoMatch))

    resolution = (
        await resolve_paths(FakeClient(session), [PathRequest("i=85", ["2:Nope"])])
    )[0]

    assert not resolution.ok
    assert resolution.status == "BadNoMatch"
    assert "not resolved" in resolution.detail


async def test_an_ambiguous_path_is_reported_not_guessed():
    session = FakeSession(lambda path: _result(_target("ns=2;s=A"), _target("ns=2;s=B")))

    resolution = (
        await resolve_paths(FakeClient(session), [PathRequest("i=85", ["2:Duplicate"])])
    )[0]

    assert not resolution.ok
    assert resolution.ambiguous is True
    assert "matched 2 nodes" in resolution.detail


async def test_a_partial_match_is_not_accepted():
    session = FakeSession(lambda path: _result(_target("ns=2;s=Partial", remaining=1)))

    resolution = (
        await resolve_paths(FakeClient(session), [PathRequest("i=85", ["2:A", "2:B"])])
    )[0]

    assert not resolution.ok
    assert "only part of the path" in resolution.detail


async def test_a_namespace_uri_target_is_refused():
    expanded = ua.ExpandedNodeId(Identifier=5, NamespaceIndex=0, NamespaceUri="urn:demo")
    session = FakeSession(
        lambda path: ua.BrowsePathResult(
            StatusCode=ua.StatusCode(ua.StatusCodes.Good),
            Targets=[ua.BrowsePathTarget(TargetId=expanded, RemainingPathIndex=FULL)],
        )
    )

    resolution = (await resolve_paths(FakeClient(session), [PathRequest("i=85", ["2:A"])]))[0]

    assert not resolution.ok
    assert "namespace URI" in resolution.detail


async def test_an_invalid_start_node_and_an_empty_path_never_reach_the_server():
    session = FakeSession(lambda path: _result(_target("ns=2;s=X")))

    resolutions = await resolve_paths(
        FakeClient(session),
        [PathRequest("not-a-nodeid", ["2:A"]), PathRequest("i=85", [])],
    )

    assert resolutions[0].status == "BadNodeIdInvalid"
    assert resolutions[1].status == "BadBrowseNameInvalid"
    assert session.batch_sizes == []


async def test_requests_are_sent_in_bounded_batches():
    session = FakeSession(lambda path: _result(_target("ns=2;s=X")))
    requests = [PathRequest("i=85", ["2:A"]) for _ in range(25)]

    resolutions = await resolve_paths(FakeClient(session), requests, chunk_size=10)

    assert len(resolutions) == 25
    assert session.batch_sizes == [10, 10, 5]


async def test_a_server_error_is_reported_per_request():
    class FailingSession:
        async def translate_browsepaths_to_nodeids(self, browse_paths: list[Any]) -> list[Any]:
            raise RuntimeError("channel closed")

    resolution = (
        await resolve_paths(FakeClient(FailingSession()), [PathRequest("i=85", ["2:A"])])
    )[0]

    assert resolution.status == "BadCommunicationError"
    assert "channel closed" in resolution.detail


async def test_resolution_requires_a_connected_session():
    with pytest.raises(PathResolutionError, match="connected session"):
        await resolve_path("", "i=85", ["2:A"])


async def test_resolve_path_convenience_returns_one_resolution():
    session = FakeSession(lambda path: _result(_target("ns=2;s=Speed")))

    resolution = await resolve_path(FakeClient(session), "i=85", ["2:Speed"])

    assert resolution.ok and resolution.node_id == "ns=2;s=Speed"
    assert _name(session.paths[0]) == "2:Speed/"
