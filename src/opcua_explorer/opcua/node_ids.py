"""NodeId normalisation shared by the service and its collaborators."""

from __future__ import annotations

from typing import Any

from asyncua import ua

from ..errors import InvalidNodeIdError
from .browse import parse_node_id


def normalise_node_id(value: Any) -> str:
    """Canonical ``ns=…;…`` string for a NodeId, an integer or a NodeId string."""
    if isinstance(value, str):
        return parse_node_id(value).to_string()
    if isinstance(value, ua.NodeId):
        return value.to_string()
    if isinstance(value, int):
        return ua.NodeId(value).to_string()
    raise InvalidNodeIdError(f"Cannot interpret {type(value).__name__} as a NodeId")
