"""Browse filters against the demo server."""

from __future__ import annotations

import pytest

from opcua_explorer.errors import InvalidNodeIdError
from opcua_explorer.models import BrowseOptions


async def test_default_browse_returns_the_hierarchical_children(service, demo_model) -> None:
    children = await service.browse_children("i=85")

    names = {child.display_name for child in children}
    assert "Plant" in names
    assert "Server" in names


async def test_an_organizes_filter_drops_components(service, demo_model) -> None:
    children = await service.browse_children("i=85", options=BrowseOptions(reference_type="i=35"))

    assert {child.display_name for child in children} >= {"Plant"}
    # Everything under Objects is organized, so no HasComponent child is listed.
    components = await service.browse_children(
        "i=85", options=BrowseOptions(reference_type="i=47", include_subtypes=False)
    )
    assert components == []


async def test_a_node_class_mask_filters_the_result(service, demo_model) -> None:
    line1 = str(demo_model.node_ids["line1"])

    objects = await service.browse_children(line1, options=BrowseOptions(node_class_mask=1))
    variables = await service.browse_children(line1, options=BrowseOptions(node_class_mask=2))

    assert objects and all(child.node_class == "Object" for child in objects)
    assert variables and all(child.node_class == "Variable" for child in variables)
    assert {child.display_name for child in variables} >= {"Temperature", "Pressure"}


async def test_an_inverse_browse_lists_the_parents(service, demo_model) -> None:
    parents = await service.browse_children(str(demo_model.node_ids["plant"]), options=BrowseOptions(direction="Inverse"))

    names = {child.display_name for child in parents}
    assert "Objects" in names


async def test_a_both_browse_returns_at_least_the_forward_children(service, demo_model) -> None:
    forward = await service.browse_children(str(demo_model.node_ids["plant"]))
    both = await service.browse_children(str(demo_model.node_ids["plant"]), options=BrowseOptions(direction="Both"))

    assert {child.node_id for child in forward} <= {child.node_id for child in both}


async def test_an_unparsable_reference_type_is_refused(service) -> None:
    with pytest.raises(InvalidNodeIdError):
        await service.browse_children("i=85", options=BrowseOptions(reference_type="not-a-node-id"))
