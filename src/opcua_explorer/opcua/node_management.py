"""One AddNodes / DeleteNodes request: the only structural change this client sends.

The request is deliberately narrow: one node below one parent with a
server-assigned NodeId, or one node deleted (target references only). Nothing is
invented locally, nothing is batched, nothing is cascaded and nothing is
retried. Every send states its authority (``models.WriteAuthority``), and the
effect is verified by reading the address space again.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from asyncua import ua

from ..errors import InvalidNodeIdError, OpcUaExplorerError
from ..models import (
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    NodeCreateOutcome,
    NodeCreateRequest,
    NodeDeleteOutcome,
    WriteAuthority,
)
from . import type_support
from .browse import parse_node_id, to_node_id
from .check_support import preview_value
from .ua_names import status_name
from .value_parsing import ValueParseError

NODE_CLASSES = {
    "Object": ua.NodeClass.Object,
    "Variable": ua.NodeClass.Variable,
}

#: Reference type a created child is attached with, per node class.
REFERENCE_TYPES = {
    "Object": ua.ObjectIds.Organizes,
    "Variable": ua.ObjectIds.HasComponent,
}

__all__ = [
    "build_add_node_item",
    "create_node",
    "delete_node",
]


def _parent_namespace_index(parent: ua.NodeId, requested: int) -> int:
    return requested if requested >= 0 else parent.NamespaceIndex


async def build_add_node_item(client: Any, request: NodeCreateRequest) -> ua.AddNodesItem:
    """Turn a validated request into one ``AddNodesItem`` (raises ``PrepareError``-style errors)."""
    if not request.browse_name:
        raise OpcUaExplorerError("A BrowseName is required for the new node.")
    node_class = NODE_CLASSES.get(request.node_class)
    if node_class is None:
        allowed = ", ".join(NODE_CLASSES)
        raise OpcUaExplorerError(f"{request.node_class!r} cannot be created here ({allowed}).")
    parent = parse_node_id(request.parent_node_id) if request.parent_node_id else None
    if parent is None:
        raise OpcUaExplorerError("A parent NodeId is required for the new node.")
    namespace_index = _parent_namespace_index(parent, request.namespace_index)

    item = ua.AddNodesItem()
    item.ParentNodeId = parent
    item.ReferenceTypeId = ua.NodeId(REFERENCE_TYPES[request.node_class])
    # A null identifier asks the server to assign an id in that namespace; this
    # client never fabricates one.
    item.RequestedNewNodeId = ua.NodeId(0, namespace_index)
    item.BrowseName = ua.QualifiedName(request.browse_name, namespace_index)
    item.NodeClass = node_class
    # TypeDefinition stays at its default (a null NodeId): the client does not
    # invent a type; a server that needs one answers with a StatusCode.

    display_name = ua.LocalizedText(Text=request.display_name or request.browse_name)
    if request.node_class == "Object":
        attributes = ua.ObjectAttributes()
        attributes.DisplayName = display_name
        attributes.EventNotifier = 0
    else:
        data_type = parse_node_id(request.data_type)
        try:
            value_type = await type_support.resolve_value_type(client, data_type, -1)
            parsed = value_type.parse(request.value_text)
        except type_support.TypeUnsupported as exc:
            raise OpcUaExplorerError(
                f"The value for the new variable was not accepted: {exc}. Nothing was created."
            ) from exc
        except ValueParseError as exc:
            raise OpcUaExplorerError(
                f"The value {request.value_text!r} does not match {request.data_type}: {exc}"
            ) from exc
        attributes = ua.VariableAttributes()
        attributes.DisplayName = display_name
        attributes.DataType = data_type
        attributes.ValueRank = -1
        attributes.AccessLevel = ua.AccessLevel.CurrentRead
        attributes.UserAccessLevel = ua.AccessLevel.CurrentRead
        attributes.Historizing = False
        attributes.Value = ua.Variant(parsed, value_type.variant_type)
        attributes.MinimumSamplingInterval = 0.0
    item.NodeAttributes = attributes
    return item


async def create_node(
    client: Any,
    request: NodeCreateRequest,
    *,
    authorization: WriteAuthority,
) -> NodeCreateOutcome:
    """Send exactly one AddNodes request and read the new node back."""
    normalised = request.normalised()
    outcome = NodeCreateOutcome(request=normalised, authorization=authorization)
    started = time.perf_counter()
    try:
        item = await build_add_node_item(client, normalised)
    except (OpcUaExplorerError, InvalidNodeIdError) as exc:
        outcome.status = STATUS_SKIPPED
        outcome.detail = str(exc)
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    session = client.uaclient
    try:
        results = await session.add_nodes([item])
    except asyncio.CancelledError:
        outcome.sent = True
        raise
    except ua.UaStatusCodeError as exc:
        outcome.sent = True
        outcome.status_code = status_name(exc.code)
        outcome.detail = f"AddNodes returned {outcome.status_code}."
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    except Exception as exc:
        outcome.sent = True
        outcome.detail = f"AddNodes failed: {type(exc).__name__}: {exc}"
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    outcome.sent = True
    outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    if not results:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = "AddNodes returned no result; the server may still have created the node."
        return outcome

    result = results[0]
    outcome.status_code = status_name(result.StatusCode)
    if not result.StatusCode.is_good():
        outcome.status = STATUS_FAILED
        outcome.detail = f"AddNodes returned {outcome.status_code}."
        return outcome
    outcome.node_id = result.AddedNodeId.to_string()

    node = client.get_node(result.AddedNodeId)
    try:
        read_back = await node.read_attribute(ua.AttributeIds.DisplayName, raise_on_bad_status=False)
        good = read_back.StatusCode is None or read_back.StatusCode.is_good()
        variant = read_back.Value
        raw = variant.Value if isinstance(variant, ua.Variant) else variant
        outcome.read_back_name = raw.Text if isinstance(raw, ua.LocalizedText) and good else ""
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            f"The server accepted the new node as {outcome.node_id} but it could not be read back: "
            f"{type(exc).__name__}: {exc}."
        )
        return outcome

    expected = normalised.display_name or normalised.browse_name
    outcome.verified = outcome.read_back_name == expected
    if outcome.verified:
        outcome.status = STATUS_PASSED
        outcome.detail = (
            f"Created {normalised.node_class} {outcome.node_id} below "
            f"{normalised.parent_node_id} and read it back."
        )
    else:
        outcome.status = STATUS_FAILED
        outcome.detail = (
            f"The node was created as {outcome.node_id} but its DisplayName reads "
            f"{outcome.read_back_name!r}, not {expected!r}."
        )
    return outcome


async def delete_node(
    client: Any,
    node_id: str,
    *,
    authorization: WriteAuthority,
    delete_target_references: bool = True,
) -> NodeDeleteOutcome:
    """Send exactly one DeleteNodes request and verify the node is gone."""
    outcome = NodeDeleteOutcome(node_id=node_id, authorization=authorization)
    started = time.perf_counter()
    try:
        target = parse_node_id(node_id)
    except InvalidNodeIdError as exc:
        outcome.status = STATUS_SKIPPED
        outcome.detail = str(exc)
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    delete_item = ua.DeleteNodesItem()
    delete_item.NodeId = to_node_id(target)
    delete_item.DeleteTargetReferences = bool(delete_target_references)
    params = ua.DeleteNodesParameters()
    params.NodesToDelete = [delete_item]

    session = client.uaclient
    try:
        codes = await session.delete_nodes(params)
    except asyncio.CancelledError:
        outcome.sent = True
        raise
    except ua.UaStatusCodeError as exc:
        outcome.sent = True
        outcome.status_code = status_name(exc.code)
        outcome.detail = f"DeleteNodes returned {outcome.status_code}."
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    except Exception as exc:
        outcome.sent = True
        outcome.detail = f"DeleteNodes failed: {type(exc).__name__}: {exc}"
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    outcome.sent = True
    outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    if not codes:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = "DeleteNodes returned no result; the node may still exist."
        return outcome

    outcome.status_code = status_name(codes[0])
    if not codes[0].is_good():
        outcome.status = STATUS_FAILED
        outcome.detail = f"DeleteNodes returned {outcome.status_code}."
        return outcome

    node = client.get_node(target)
    try:
        await node.read_attribute(ua.AttributeIds.DisplayName, raise_on_bad_status=True)
    except asyncio.CancelledError:
        raise
    except ua.UaStatusCodeError as exc:
        outcome.verified = status_name(exc.code) in ("BadNodeIdUnknown", "BadNodeIdInvalid")
        if outcome.verified:
            outcome.status = STATUS_PASSED
            outcome.detail = f"Deleted {node_id} and confirmed it no longer exists."
        else:
            outcome.status = STATUS_FAILED
            outcome.detail = f"The node was deleted but reading it afterwards returned {status_name(exc.code)}."
        return outcome
    except Exception as exc:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            f"The delete was acknowledged but the node could not be read afterwards: "
            f"{type(exc).__name__}: {exc}."
        )
        return outcome

    outcome.verified = False
    outcome.status = STATUS_FAILED
    outcome.detail = f"DeleteNodes returned Good but {node_id} is still readable."
    return outcome


def preview_value_text(value_text: str) -> str:
    """Render an operator-entered value for the confirmation text."""
    return preview_value(value_text)
