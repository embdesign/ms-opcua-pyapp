"""Browse-path resolution against a real server (TranslateBrowsePathsToNodeIds)."""

from __future__ import annotations

from opcua_explorer.opcua.path_support import PathRequest


async def test_browse_paths_resolve_against_the_demo_server(service, demo_model):
    index = demo_model.namespace_index
    requests = [
        PathRequest("i=85", [f"{index}:Plant", f"{index}:Line1", f"{index}:Motor", f"{index}:Speed"]),
        PathRequest("i=85", [f"{index}:Plant", f"{index}:Line1", f"{index}:Temperature"]),
        PathRequest("i=85", [f"{index}:DoesNotExist"]),
        PathRequest("i=85", []),
    ]

    resolutions = await service.resolve_browse_paths(requests)

    assert len(resolutions) == 4
    assert resolutions[0].ok, resolutions[0].detail
    assert resolutions[0].node_id == demo_model.node_ids["speed"]
    assert resolutions[1].ok, resolutions[1].detail
    assert resolutions[1].node_id == demo_model.node_ids["temperature"]
    assert not resolutions[2].ok
    assert "not resolved" in resolutions[2].detail
    assert not resolutions[3].ok
    assert resolutions[3].status == "BadBrowseNameInvalid"


async def test_a_path_with_the_wrong_namespace_index_does_not_resolve(service, demo_model):
    index = demo_model.namespace_index
    wrong = 0 if index != 0 else 1

    resolution = (
        await service.resolve_browse_paths([PathRequest("i=85", [f"{wrong}:Plant", f"{index}:Line1"])])
    )[0]

    assert not resolution.ok
