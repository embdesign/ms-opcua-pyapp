"""Attribute reading and decoding.

``Node.read_attributes`` issues one Read service call per node, which is far too
chatty for downloading thousands of nodes. This module batches many
``ReadValueId`` entries into a handful of ``Read`` requests and decodes the
returned ``DataValue`` objects into typed model fields.

Bad status codes are expected and normal: not every attribute applies to every
NodeClass (for example ``DataType`` on an Object). They are recorded in
``NodeRecord.unavailable`` instead of being treated as failures.

A response that cannot be *decoded* at all is a different problem: some servers
put values into structures that the client's generated decoders reject (for
example an enumeration field carrying ``0xFFFFFF``, which is not a member of
``MessageSecurityMode``). Because asyncua decodes a whole Read response at once,
one such value would otherwise abort an entire chunk - and with it the download
of thousands of nodes. :func:`_read_tolerant` isolates the offending node(s) by
bisecting the chunk, so a broken value costs exactly that value.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from asyncua import ua

from ..models import AttributeResult, NodeRecord, ValueSnapshot, iso_utc
from .browse import node_id_to_string, to_node_id
from .serialize import SerializationContext, serialize_value
from .ua_names import status_name

_logger = logging.getLogger(__name__)

VARIABLE_NODE_CLASSES = frozenset({"Variable", "VariableType"})
METHOD_NODE_CLASSES = frozenset({"Method"})

#: Status prefix recorded for a value the client could not decode.
DECODE_ERROR_STATUS = "DecodeError"

#: Failures that mean the *connection* is unusable and must not be "recovered"
#: by splitting the request: they are re-raised so the caller can report them.
_FATAL_READ_ERRORS: tuple[type[BaseException], ...] = (OSError, ua.UaError)

#: Longest status text recorded for a decode failure (keeps reports readable).
_MAX_FAILURE_TEXT = 200


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    attribute_id: ua.AttributeIds
    name: str


_BASE_SPECS: tuple[AttributeSpec, ...] = (
    AttributeSpec(ua.AttributeIds.NodeClass, "NodeClass"),
    AttributeSpec(ua.AttributeIds.BrowseName, "BrowseName"),
    AttributeSpec(ua.AttributeIds.DisplayName, "DisplayName"),
    AttributeSpec(ua.AttributeIds.Description, "Description"),
)

_VARIABLE_SPECS: tuple[AttributeSpec, ...] = (
    AttributeSpec(ua.AttributeIds.DataType, "DataType"),
    AttributeSpec(ua.AttributeIds.ValueRank, "ValueRank"),
    AttributeSpec(ua.AttributeIds.ArrayDimensions, "ArrayDimensions"),
    AttributeSpec(ua.AttributeIds.AccessLevel, "AccessLevel"),
    AttributeSpec(ua.AttributeIds.UserAccessLevel, "UserAccessLevel"),
)

_METHOD_SPECS: tuple[AttributeSpec, ...] = (
    AttributeSpec(ua.AttributeIds.Executable, "Executable"),
    AttributeSpec(ua.AttributeIds.UserExecutable, "UserExecutable"),
)


def specs_for(node_class: str) -> list[AttributeSpec]:
    """Attributes worth reading for a given NodeClass.

    The Value attribute is deliberately *not* included: values are read through
    :func:`read_values_bulk` so that StatusCode/timestamps/variant type are
    preserved, and so that "values included" stays an explicit opt-in.
    """
    specs = list(_BASE_SPECS)
    if node_class in VARIABLE_NODE_CLASSES:
        specs.extend(_VARIABLE_SPECS)
    elif node_class in METHOD_NODE_CLASSES:
        specs.extend(_METHOD_SPECS)
    return specs


def decode_attribute(name: str, data_value: ua.DataValue) -> AttributeResult:
    """Decode one DataValue into a display/export-friendly AttributeResult.

    Values stay *native* (NodeId, QualifiedName, LocalizedText, int, ...); use
    :func:`format_attribute_value` for strings.
    """
    status = status_name(data_value.StatusCode)
    if not data_value.StatusCode.is_good():
        return AttributeResult(attribute=name, value=None, status=status, ok=False)
    if data_value.Value is None:
        return AttributeResult(attribute=name, value=None, status=status, ok=True)

    raw = data_value.Value.Value if isinstance(data_value.Value, ua.Variant) else data_value.Value
    if name == "NodeClass":
        try:
            value: Any = ua.NodeClass(int(raw)).name
        except Exception:  # pragma: no cover - defensive
            value = str(raw)
    elif name in ("AccessLevel", "UserAccessLevel"):
        value = int(raw)
    elif name in ("Executable", "UserExecutable"):
        value = bool(raw)
    elif name in ("ValueRank", "MinimumSamplingInterval"):
        value = int(raw)
    elif name == "ArrayDimensions":
        value = [int(item) for item in raw] if raw else []
    else:
        value = raw
    return AttributeResult(attribute=name, value=value, status=status, ok=True)


def decode_access_level(value: int | None) -> list[str]:
    """Decode the AccessLevel bit field into readable names.

    asyncua models AccessLevel as bit *positions* (CurrentRead=0,
    CurrentWrite=1, ...), so ``parse_bitfield`` is the correct helper.
    """
    if value is None:
        return []
    try:
        members = ua.AccessLevel.parse_bitfield(int(value))
    except Exception:  # pragma: no cover - defensive
        return []
    return [member.name for member in sorted(members, key=lambda member: int(member.value))]


def format_attribute_value(value: Any) -> str:
    """Format a decoded attribute value for tables and CSV cells."""
    if value is None:
        return ""
    if isinstance(value, ua.NodeId):
        return value.to_string()
    if isinstance(value, ua.QualifiedName):
        return f"{value.NamespaceIndex}:{value.Name}"
    if isinstance(value, ua.LocalizedText):
        return value.Text or ""
    if isinstance(value, ua.ExpandedNodeId):
        return value.to_string()
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (list, tuple)):
        return ", ".join(format_attribute_value(item) for item in value)
    return str(value)


async def _read_tolerant(
    session: Any,
    node_ids: list[str],
    attribute_id: ua.AttributeIds,
    failures: dict[tuple[str, ua.AttributeIds], str],
) -> list[ua.DataValue | None]:
    """Read one attribute of many nodes, isolating values the client cannot decode.

    ``failures`` receives ``(node_id, attribute_id) -> status text`` for nodes
    whose response could not be decoded; those positions return ``None``.
    """
    if not node_ids:
        return []

    params = ua.ReadParameters()
    for node_id in node_ids:
        read_value_id = ua.ReadValueId()
        read_value_id.NodeId = to_node_id(node_id)
        read_value_id.AttributeId = attribute_id
        params.NodesToRead.append(read_value_id)

    try:
        values = list(await session.read(params))
    except _FATAL_READ_ERRORS:
        raise
    except Exception as exc:  # noqa: BLE001 - a decode error must not abort a download
        if len(node_ids) == 1:
            node_id = node_ids[0]
            text = f"{DECODE_ERROR_STATUS}: {type(exc).__name__}: {exc}"
            failures[(node_id, attribute_id)] = text[:_MAX_FAILURE_TEXT]
            _logger.warning(
                "%s of %s could not be decoded (%s: %s); the value is recorded as unavailable",
                _attribute_name(attribute_id),
                node_id,
                type(exc).__name__,
                exc,
            )
            return [None]
        # Bisect: the response is already fully read from the socket, so asking
        # again for the halves is safe and isolates the offending node(s).
        middle = len(node_ids) // 2
        left = await _read_tolerant(session, node_ids[:middle], attribute_id, failures)
        right = await _read_tolerant(session, node_ids[middle:], attribute_id, failures)
        return left + right

    if len(values) != len(node_ids):
        _logger.warning(
            "Read returned %d result(s) for %d request(s); missing results are marked failed",
            len(values),
            len(node_ids),
        )
    return values


def _attribute_name(attribute_id: ua.AttributeIds) -> str:
    try:
        return str(ua.AttributeIds(int(attribute_id)).name)
    except Exception:  # pragma: no cover - defensive
        return str(attribute_id)


async def read_attributes_bulk(
    session: Any,
    requests: Iterable[tuple[str, AttributeSpec]],
    *,
    chunk_size: int = 400,
) -> dict[tuple[str, str], AttributeResult]:
    """Read ``(node_id, spec)`` pairs with a bounded number of Read calls.

    Returns a mapping ``(node_id, attribute_name) -> AttributeResult``.
    """
    pending = [(node_id_to_string(node_id), spec) for node_id, spec in requests]
    results: dict[tuple[str, str], AttributeResult] = {}
    size = max(1, int(chunk_size))
    failures: dict[tuple[str, ua.AttributeIds], str] = {}

    for start in range(0, len(pending), size):
        chunk = pending[start : start + size]
        # One Read per attribute id, so a decode failure is confined to that
        # attribute instead of discarding every attribute of the chunk.
        by_attribute: dict[ua.AttributeIds, list[str]] = {}
        for node_id, spec in chunk:
            by_attribute.setdefault(spec.attribute_id, []).append(node_id)

        decoded: dict[tuple[str, ua.AttributeIds], ua.DataValue | None] = {}
        for attribute_id, node_ids in by_attribute.items():
            values = await _read_tolerant(session, node_ids, attribute_id, failures)
            for index, node_id in enumerate(node_ids):
                decoded[(node_id, attribute_id)] = values[index] if index < len(values) else None

        for node_id, spec in chunk:
            failure = failures.get((node_id, spec.attribute_id))
            if failure is not None:
                results[(node_id, spec.name)] = AttributeResult(
                    attribute=spec.name, value=None, status=failure, ok=False
                )
                continue
            data_value = decoded.get((node_id, spec.attribute_id))
            if data_value is None:
                results[(node_id, spec.name)] = AttributeResult(
                    attribute=spec.name, value=None, status="BadNoResult", ok=False
                )
            else:
                results[(node_id, spec.name)] = decode_attribute(spec.name, data_value)
    return results


async def read_values_bulk(
    session: Any,
    node_ids: Iterable[Any],
    *,
    chunk_size: int = 200,
) -> dict[str, ValueSnapshot]:
    """Read the Value attribute of many variables and serialize the results."""
    identifiers = [node_id_to_string(node_id) for node_id in node_ids]
    snapshots: dict[str, ValueSnapshot] = {}
    size = max(1, int(chunk_size))
    failures: dict[tuple[str, ua.AttributeIds], str] = {}
    attribute_id = ua.AttributeIds.Value

    for start in range(0, len(identifiers), size):
        chunk = identifiers[start : start + size]
        data_values = await _read_tolerant(session, chunk, attribute_id, failures)
        for index, node_id in enumerate(chunk):
            failure = failures.get((node_id, attribute_id))
            if failure is not None:
                snapshots[node_id] = ValueSnapshot(value=None, status=failure, encoding="unavailable")
                continue
            if index >= len(data_values) or data_values[index] is None:
                snapshots[node_id] = ValueSnapshot(value=None, status="BadNoResult", encoding="unavailable")
                continue
            snapshots[node_id] = decode_data_value(data_values[index])
    return snapshots


def decode_data_value(data_value: ua.DataValue) -> ValueSnapshot:
    """Convert a DataValue into a ValueSnapshot with a JSON-safe value."""
    status = status_name(data_value.StatusCode)
    source_timestamp = iso_utc(data_value.SourceTimestamp)
    server_timestamp = iso_utc(data_value.ServerTimestamp)

    if not data_value.StatusCode.is_good():
        return ValueSnapshot(
            value=None,
            status=status,
            source_timestamp=source_timestamp,
            server_timestamp=server_timestamp,
            encoding="unavailable",
        )
    if data_value.Value is None:
        return ValueSnapshot(
            value=None,
            status=status,
            source_timestamp=source_timestamp,
            server_timestamp=server_timestamp,
        )

    variant = data_value.Value
    variant_type_name: str | None = None
    raw: Any = variant
    if isinstance(variant, ua.Variant):
        raw = variant.Value
        try:
            variant_type_name = ua.VariantType(int(variant.VariantType)).name
        except Exception:  # pragma: no cover - defensive
            variant_type_name = str(variant.VariantType)

    context = SerializationContext()
    encoded = serialize_value(raw, context)
    return ValueSnapshot(
        value=encoded,
        status=status,
        variant_type=variant_type_name,
        source_timestamp=source_timestamp,
        server_timestamp=server_timestamp,
        encoding="unsupported" if context.has_unsupported else "json",
        unsupported=context.unsupported,
    )


def apply_attributes_to_record(
    record: NodeRecord,
    attributes: dict[str, AttributeResult],
    *,
    namespace_uri_of: Any,
) -> NodeRecord:
    """Copy decoded attributes into a NodeRecord.

    ``namespace_uri_of`` maps a namespace index to its URI (or returns None).
    """
    for name, result in attributes.items():
        if not result.ok:
            record.unavailable[name] = result.status
            continue
        value = result.value
        if name == "NodeClass":
            record.node_class = str(value)
        elif name == "BrowseName":
            if isinstance(value, ua.QualifiedName):
                record.browse_name = value.Name or ""
                record.browse_name_namespace_index = int(value.NamespaceIndex)
            else:
                record.browse_name = format_attribute_value(value)
        elif name == "DisplayName":
            record.display_name = format_attribute_value(value)
        elif name == "Description":
            record.description = format_attribute_value(value) or None
        elif name == "DataType":
            if isinstance(value, ua.NodeId):
                record.data_type = value.to_string()
            else:
                record.data_type = format_attribute_value(value) or None
        elif name == "ValueRank":
            record.value_rank = int(value) if value is not None else None
        elif name == "ArrayDimensions":
            record.array_dimensions = list(value) if value else []
        elif name == "AccessLevel":
            record.access_level = int(value) if value is not None else None
            record.access_level_names = decode_access_level(record.access_level)
        elif name == "UserAccessLevel":
            record.user_access_level = int(value) if value is not None else None
            record.user_access_level_names = decode_access_level(record.user_access_level)
    if record.namespace_uri is None:
        try:
            index = to_node_id(record.node_id).NamespaceIndex
        except Exception:  # pragma: no cover - node ids come from the library
            index = 0
        record.namespace_uri = namespace_uri_of(index)
    return record


async def read_node_attributes(
    session: Any,
    node_id: Any,
    *,
    node_class: str,
    include_value: bool = True,
    namespace_uri_of: Any = lambda index: None,
) -> tuple[NodeRecord, list[AttributeResult]]:
    """Read every applicable attribute of one node (used by the details panel)."""
    node_id_string = node_id_to_string(node_id)
    specs = specs_for(node_class)
    results = await read_attributes_bulk(session, ((node_id_string, spec) for spec in specs), chunk_size=64)

    attributes = [results[(node_id_string, spec.name)] for spec in specs]
    record = NodeRecord(node_id=node_id_string)
    apply_attributes_to_record(record, {result.attribute: result for result in attributes}, namespace_uri_of=namespace_uri_of)

    if include_value and record.node_class in VARIABLE_NODE_CLASSES:
        snapshot = (await read_values_bulk(session, [node_id_string])).get(node_id_string)
        if snapshot is not None:
            record.value = snapshot
            attributes.append(
                AttributeResult(
                    attribute="Value",
                    value=snapshot.value,
                    status=snapshot.status,
                    ok=snapshot.encoding != "unavailable",
                )
            )
    return record, attributes
