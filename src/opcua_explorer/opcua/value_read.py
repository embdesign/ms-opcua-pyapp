"""One explicit Value read with its options: IndexRange, MaxAge, TimestampsToReturn.

The read is a single ``Read`` request for the Value attribute of one node. The
client validates the NumericRange shape and maps the option names; whether the
server honours a slice, a cache age or a timestamp choice is the server's
decision and is reported, never assumed.
"""

from __future__ import annotations

import re
import time
from typing import Any

from asyncua import ua

from ..errors import OpcUaExplorerError
from ..models import TIMESTAMPS_TO_RETURN, ValueReadOptions, ValueReadResult
from .attributes import decode_data_value

#: One NumericRange dimension: ``n`` or ``n:m``; dimensions are comma separated.
_INDEX_RANGE = re.compile(r"^\d+(?::\d+)?(?:,\d+(?::\d+)?)*$")

_TIMESTAMPS = {
    "Source": ua.TimestampsToReturn.Source,
    "Server": ua.TimestampsToReturn.Server,
    "Both": ua.TimestampsToReturn.Both,
    "Neither": ua.TimestampsToReturn.Neither,
}

__all__ = [
    "TIMESTAMPS_TO_RETURN",
    "build_read_parameters",
    "read_value_with_options",
    "timestamps_to_return",
    "validate_index_range",
]


def validate_index_range(text: str) -> str:
    """Return the trimmed NumericRange or raise before any request is sent."""
    value = (text or "").strip()
    if not value:
        return ""
    if not _INDEX_RANGE.match(value):
        raise OpcUaExplorerError(
            f"{text!r} is not a NumericRange. Use one or more dimensions such as 1, 1:3 or 0,2:4."
        )
    return value


def timestamps_to_return(name: str) -> ua.TimestampsToReturn:
    """Map an option name onto ``ua.TimestampsToReturn`` (default ``Both``)."""
    return _TIMESTAMPS.get(name, ua.TimestampsToReturn.Both)


def build_read_parameters(node_id: str, options: ValueReadOptions) -> ua.ReadParameters:
    """One ``Read`` request for the Value attribute with the given options."""
    read_value = ua.ReadValueId()
    read_value.NodeId = ua.NodeId.from_string(node_id)
    read_value.AttributeId = ua.AttributeIds.Value
    read_value.IndexRange = options.index_range or None
    parameters = ua.ReadParameters()
    parameters.MaxAge = int(options.max_age_ms)
    parameters.TimestampsToReturn = timestamps_to_return(options.timestamps)
    parameters.NodesToRead.append(read_value)
    return parameters


def _detail(options: ValueReadOptions, status: str) -> str:
    if status != "Good":
        return f"Read returned {status}."
    parts = []
    if options.index_range:
        parts.append(f"IndexRange {options.index_range} requested")
    parts.append(f"MaxAge {options.max_age_ms} ms")
    parts.append(f"timestamps {options.timestamps}")
    return "; ".join(parts) + " — the server decides which of these it applies."


async def read_value_with_options(
    session: Any, node_id: str, options: ValueReadOptions
) -> ValueReadResult:
    """Read the Value attribute of one node once; no retry, no subscription."""
    normalised = options.normalised()
    validate_index_range(normalised.index_range)
    parameters = build_read_parameters(node_id, normalised)
    started = time.perf_counter()
    try:
        results = await session.read(parameters)
    except Exception as exc:
        raise OpcUaExplorerError(f"Reading {node_id} failed: {type(exc).__name__}: {exc}") from exc
    if not results:
        raise OpcUaExplorerError(f"Reading {node_id} returned no result.")
    snapshot = decode_data_value(results[0])
    return ValueReadResult(
        node_id=node_id,
        options=normalised,
        snapshot=snapshot,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        detail=_detail(normalised, snapshot.status),
    )
