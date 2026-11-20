"""Server-supplied metadata for a variable or a method.

The guarded test checks and the universal sequence runner must read the same facts the same
way: the DataType and ValueRank of a variable, the ``InputArguments`` of a method, and
``Executable``. All of them come from the **server**, never from a guess, so a value that does
not fit is rejected locally instead of being sent as a wrong-typed write.

``MetadataUnavailable`` carries the operator-facing reason; callers translate it into their own
control flow (a skipped check, a failed step) without re-implementing the reads.
"""

from __future__ import annotations

import asyncio
from typing import Any

from asyncua import ua

from ..errors import OpcUaExplorerError
from .ua_names import status_name


class MetadataUnavailable(OpcUaExplorerError):
    """The server did not provide a fact the caller needs; nothing has been sent."""


async def read_node_data_type(client: Any, node_id: str) -> tuple[ua.NodeId, int]:
    """DataType NodeId and ValueRank of a variable, as reported by the server."""
    node = client.get_node(node_id)
    try:
        data_type = await node.read_data_type()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise MetadataUnavailable(f"DataType of {node_id} could not be read: {exc}") from exc
    try:
        rank_result = await node.read_attribute(ua.AttributeIds.ValueRank)
        value_rank = int(rank_result.Value.Value)
    except asyncio.CancelledError:
        raise
    except Exception:
        # ValueRank is optional metadata. -3 ("scalar or one dimension") is the only
        # honest assumption when it cannot be read: it lets either text form through
        # instead of claiming a shape the server never reported.
        value_rank = -3
    return data_type, value_rank


async def read_input_arguments(client: Any, method_node_id: str) -> list[Any]:
    """``InputArguments`` of a method as ``ua.Argument``-like objects.

    asyncua 2.1.0 returns ``uaprotocol_auto.Argument`` for a decoded value while ``ua.Argument``
    is the hand-written class, so this checks the fields instead of the class identity.
    """
    method = client.get_node(method_node_id)
    try:
        argument_property = await method.get_child(["InputArguments"])
    except asyncio.CancelledError:
        raise
    except Exception:
        # A method without an InputArguments property takes no arguments.
        return []
    try:
        value = await argument_property.read_value()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise MetadataUnavailable(f"InputArguments of {method_node_id} could not be read: {exc}") from exc
    if not value:
        return []
    return [
        argument
        for argument in value
        if all(hasattr(argument, field) for field in ("Name", "DataType", "ValueRank"))
    ]


async def read_executable(client: Any, method_node_id: str) -> tuple[bool | None, str]:
    """``Executable`` of a method: ``(value, StatusCode name)``; ``None`` when unavailable."""
    method = client.get_node(method_node_id)
    try:
        result = await method.read_attribute(ua.AttributeIds.Executable)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None, "unavailable"
    status = status_name(result.StatusCode)
    if not result.StatusCode.is_good():
        return None, status
    return bool(result.Value.Value), status
