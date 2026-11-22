"""Unit tests for the AddNodes/DeleteNodes request handling (no server)."""

from __future__ import annotations

from typing import Any

import pytest
from asyncua import ua

from opcua_explorer.errors import OpcUaExplorerError
from opcua_explorer.models import NodeCreateRequest, WriteAuthority
from opcua_explorer.opcua.node_management import (
    build_add_node_item,
    create_node,
    delete_node,
)

AUTHORITY = WriteAuthority("operator-dialog", "create Object")


class _FakeNode:
    def __init__(self, *, name: str = "New", gone: bool = False) -> None:
        self._name = name
        self._gone = gone

    async def read_attribute(
        self, attribute: ua.AttributeIds, indexrange: str | None = None, raise_on_bad_status: bool = False
    ) -> ua.DataValue:
        if self._gone:
            if raise_on_bad_status:
                raise ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown)
            data_value = ua.DataValue(ua.Variant(None, ua.VariantType.Null))
            data_value.StatusCode = ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown)
            return data_value
        data_value = ua.DataValue(ua.Variant(ua.LocalizedText(Text=self._name), ua.VariantType.LocalizedText))
        data_value.StatusCode = ua.StatusCode(ua.StatusCodes.Good)
        return data_value


class _FakeUaClient:
    def __init__(self, *, add_status: int = ua.StatusCodes.Good, delete_status: int = ua.StatusCodes.Good) -> None:
        self.add_status = add_status
        self.delete_status = delete_status
        self.added: list[ua.AddNodesItem] = []
        self.deleted: list[ua.DeleteNodesParameters] = []

    async def add_nodes(self, items: list[ua.AddNodesItem]) -> list[ua.AddNodesResult]:
        self.added.extend(items)
        result = ua.AddNodesResult()
        result.StatusCode = ua.StatusCode(self.add_status)
        result.AddedNodeId = ua.NodeId(7, 2) if self.add_status == ua.StatusCodes.Good else ua.NodeId()
        return [result]

    async def delete_nodes(self, params: ua.DeleteNodesParameters) -> list[ua.StatusCode]:
        self.deleted.append(params)
        return [ua.StatusCode(self.delete_status)]


class _FakeClient:
    def __init__(self, uaclient: _FakeUaClient, node: _FakeNode | None = None) -> None:
        self.uaclient = uaclient
        self._node = node or _FakeNode()

    def get_node(self, node_id: Any) -> _FakeNode:
        return self._node


async def test_an_object_item_is_built_for_the_parent_namespace() -> None:
    request = NodeCreateRequest(parent_node_id="ns=2;s=Plant", browse_name="NewObj").normalised()

    item = await build_add_node_item(_FakeClient(_FakeUaClient()), request)

    assert item.ParentNodeId.to_string() == "ns=2;s=Plant"
    assert item.ReferenceTypeId == ua.NodeId(ua.ObjectIds.Organizes)
    assert item.NodeClass == ua.NodeClass.Object
    assert item.BrowseName == ua.QualifiedName("NewObj", 2)
    assert item.RequestedNewNodeId.NamespaceIndex == 2
    assert item.RequestedNewNodeId.has_null_identifier() is True
    assert isinstance(item.NodeAttributes, ua.ObjectAttributes)
    assert item.NodeAttributes.DisplayName.Text == "NewObj"


async def test_a_variable_item_carries_type_rank_and_value() -> None:
    request = NodeCreateRequest(
        parent_node_id="ns=2;s=Plant", browse_name="NewVar", node_class="Variable",
        data_type="i=11", value_text="42.5",
    ).normalised()

    item = await build_add_node_item(_FakeClient(_FakeUaClient()), request)

    attributes = item.NodeAttributes
    assert isinstance(attributes, ua.VariableAttributes)
    assert item.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasComponent)
    assert attributes.DataType == ua.NodeId(ua.ObjectIds.Double)
    assert attributes.ValueRank == -1
    assert attributes.Value.Value == 42.5
    assert attributes.AccessLevel == ua.AccessLevel.CurrentRead


async def test_an_explicit_namespace_wins() -> None:
    request = NodeCreateRequest(parent_node_id="ns=2;s=Plant", browse_name="X", namespace_index=5).normalised()

    item = await build_add_node_item(_FakeClient(_FakeUaClient()), request)

    assert item.BrowseName.NamespaceIndex == 5
    assert item.RequestedNewNodeId.NamespaceIndex == 5


async def test_a_value_that_does_not_match_the_type_is_refused() -> None:
    request = NodeCreateRequest(
        parent_node_id="ns=2;s=Plant", browse_name="X", node_class="Variable",
        data_type="i=11", value_text="not-a-number",
    ).normalised()

    with pytest.raises(OpcUaExplorerError):
        await build_add_node_item(_FakeClient(_FakeUaClient()), request)


async def test_an_unknown_node_class_is_refused() -> None:
    request = NodeCreateRequest(parent_node_id="i=85", browse_name="X", node_class="Method").normalised()

    with pytest.raises(OpcUaExplorerError):
        await build_add_node_item(_FakeClient(_FakeUaClient()), request)


async def test_a_missing_browse_name_is_refused() -> None:
    request = NodeCreateRequest(parent_node_id="i=85", browse_name="   ").normalised()

    with pytest.raises(OpcUaExplorerError):
        await build_add_node_item(_FakeClient(_FakeUaClient()), request)


async def test_create_sends_one_request_and_verifies_the_read_back() -> None:
    uaclient = _FakeUaClient()
    client = _FakeClient(uaclient, _FakeNode(name="NewObj"))

    outcome = await create_node(
        client, NodeCreateRequest(parent_node_id="ns=2;s=Plant", browse_name="NewObj"), authorization=AUTHORITY
    )

    assert outcome.status == "passed"
    assert outcome.sent is True
    assert outcome.node_id == "ns=2;i=7"
    assert outcome.verified is True
    assert len(uaclient.added) == 1
    assert outcome.authorization is AUTHORITY


async def test_a_refused_create_is_reported() -> None:
    uaclient = _FakeUaClient(add_status=ua.StatusCodes.BadUserAccessDenied)

    outcome = await create_node(
        _FakeClient(uaclient), NodeCreateRequest(parent_node_id="i=85", browse_name="X"), authorization=AUTHORITY
    )

    assert outcome.status == "failed"
    assert outcome.sent is True
    assert outcome.status_code == "BadUserAccessDenied"
    assert outcome.node_id == ""


async def test_a_different_read_back_name_fails_the_create() -> None:
    client = _FakeClient(_FakeUaClient(), _FakeNode(name="Something else"))

    outcome = await create_node(
        client,
        NodeCreateRequest(parent_node_id="i=85", browse_name="Wanted", display_name="Wanted"),
        authorization=AUTHORITY,
    )

    assert outcome.status == "failed"
    assert "Wanted" in outcome.detail


async def test_an_invalid_request_sends_nothing() -> None:
    uaclient = _FakeUaClient()

    outcome = await create_node(
        _FakeClient(uaclient), NodeCreateRequest(parent_node_id="", browse_name="X"), authorization=AUTHORITY
    )

    assert outcome.status == "skipped"
    assert outcome.sent is False

    outcome = await create_node(
        _FakeClient(uaclient), NodeCreateRequest(parent_node_id="not-a-node", browse_name="X"), authorization=AUTHORITY
    )
    assert outcome.status == "skipped"
    assert uaclient.added == []


async def test_delete_sends_one_item_with_reference_cleanup() -> None:
    uaclient = _FakeUaClient()
    client = _FakeClient(uaclient, _FakeNode(gone=True))

    outcome = await delete_node(client, "ns=2;i=7", authorization=AUTHORITY)

    assert outcome.status == "passed"
    assert outcome.verified is True
    assert len(uaclient.deleted) == 1
    items = uaclient.deleted[0].NodesToDelete
    assert len(items) == 1
    assert items[0].NodeId == ua.NodeId(7, 2)
    assert items[0].DeleteTargetReferences is True


async def test_a_refused_delete_is_reported() -> None:
    uaclient = _FakeUaClient(delete_status=ua.StatusCodes.BadUserAccessDenied)

    outcome = await delete_node(_FakeClient(uaclient), "i=85", authorization=AUTHORITY)

    assert outcome.status == "failed"
    assert outcome.status_code == "BadUserAccessDenied"


async def test_a_node_that_is_still_readable_fails_the_delete() -> None:
    client = _FakeClient(_FakeUaClient(), _FakeNode(name="Still here"))

    outcome = await delete_node(client, "ns=2;i=7", authorization=AUTHORITY)

    assert outcome.status == "failed"
    assert outcome.verified is False
    assert "still readable" in outcome.detail


async def test_an_invalid_delete_node_id_sends_nothing() -> None:
    uaclient = _FakeUaClient()

    outcome = await delete_node(_FakeClient(uaclient), "nonsense", authorization=AUTHORITY)

    assert outcome.status == "skipped"
    assert uaclient.deleted == []
