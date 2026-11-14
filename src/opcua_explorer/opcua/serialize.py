"""Consistent, JSON-safe encoding of OPC UA values.

Export policy
-------------
* Supported scalar types are encoded with an explicit ``$type`` marker where the
  plain JSON type would be ambiguous (ByteString, DateTime, Guid, NodeId,
  QualifiedName, LocalizedText, StatusCode, Variant, DataValue, ExtensionObject).
* Arrays keep their order and become JSON arrays; nesting is preserved.
* Values that cannot be represented faithfully are *flagged*, never silently
  replaced by a lossy string: the encoder emits ``{"$type": "Unsupported", ...}``
  and records an entry in :class:`SerializationContext.unsupported`, which the
  export summary reports.
* Cyclic structures and excessive depth are detected instead of recursing.

The encoder never raises for value content - a broken value must not abort an
export of thousands of nodes.
"""

from __future__ import annotations

import base64
import dataclasses
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from asyncua import ua

from ..formatting import flatten_for_csv as flatten_for_csv
from ..formatting import safe_repr
from ..models import iso_utc
from .ua_names import status_name, variant_type_name

MAX_DEPTH = 24
MAX_UNSUPPORTED_ENTRIES = 200


@dataclass(slots=True)
class SerializationContext:
    """Collects value encodings that could not be represented faithfully."""

    unsupported: list[dict[str, Any]] = field(default_factory=list)

    def note(self, kind: str, detail: str = "", path: str = "") -> None:
        """Record an encoding problem, de-duplicated by kind and detail.

        The path of the first occurrence is kept so the summary stays small for
        large exports while still pointing at an example.
        """
        if len(self.unsupported) >= MAX_UNSUPPORTED_ENTRIES:
            return
        if any(item.get("kind") == kind and item.get("detail") == detail for item in self.unsupported):
            return
        entry: dict[str, Any] = {"kind": kind, "detail": detail}
        if path:
            entry["path"] = path
        self.unsupported.append(entry)

    @property
    def has_unsupported(self) -> bool:
        return bool(self.unsupported)


def serialize_value(
    value: Any,
    ctx: SerializationContext | None = None,
    *,
    _depth: int = 0,
    _path: str = "",
    _seen: set[int] | None = None,
) -> Any:
    """Encode ``value`` as JSON-safe data.

    ``ctx`` accumulates unsupported encodings across one export.
    """
    ctx = ctx if ctx is not None else SerializationContext()
    seen = _seen if _seen is not None else set()

    if _depth > MAX_DEPTH:
        ctx.note("MaxDepthExceeded", f"value nested deeper than {MAX_DEPTH} levels", _path)
        return {"$type": "MaxDepthExceeded"}

    if value is None or isinstance(value, (bool, int, str)):
        return value

    if isinstance(value, float):
        if math.isnan(value):
            return {"$type": "Float", "value": "NaN"}
        if math.isinf(value):
            return {"$type": "Float", "value": "Infinity" if value > 0 else "-Infinity"}
        return value

    if isinstance(value, Decimal):
        return {"$type": "Decimal", "value": str(value)}

    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$type": "ByteString", "base64": base64.b64encode(bytes(value)).decode("ascii")}

    if isinstance(value, datetime):
        return {"$type": "DateTime", "iso": iso_utc(value)}

    if isinstance(value, uuid.UUID):
        return {"$type": "Guid", "value": str(value)}

    # --- asyncua value objects -------------------------------------------------
    # ExpandedNodeId subclasses NodeId, so it must be tested first.
    if isinstance(value, ua.ExpandedNodeId):
        return {"$type": "ExpandedNodeId", "value": value.to_string()}
    if isinstance(value, ua.NodeId):
        return {"$type": "NodeId", "value": value.to_string()}
    if isinstance(value, ua.QualifiedName):
        return {
            "$type": "QualifiedName",
            "namespace_index": int(value.NamespaceIndex),
            "name": value.Name,
        }
    if isinstance(value, ua.LocalizedText):
        return {"$type": "LocalizedText", "locale": value.Locale, "text": value.Text}
    if isinstance(value, ua.StatusCode):
        code = int(getattr(value, "value", value))
        return {"$type": "StatusCode", "code": code, "name": value.name}
    if isinstance(value, ua.ExtensionObject):
        return _serialize_extension_object(value, ctx, _depth, _path, seen)
    if isinstance(value, ua.Variant):
        return _serialize_variant(value, ctx, _depth, _path, seen)
    if isinstance(value, ua.DataValue):
        return {
            "$type": "DataValue",
            "value": serialize_value(value.Value, ctx, _depth=_depth + 1, _path=f"{_path}.Value", _seen=seen),
            "status": status_name(value.StatusCode),
            "source_timestamp": iso_utc(value.SourceTimestamp),
            "server_timestamp": iso_utc(value.ServerTimestamp),
        }

    # --- containers ------------------------------------------------------------
    if isinstance(value, dict):
        if id(value) in seen:
            ctx.note("Cycle", "cyclic dictionary", _path)
            return {"$type": "Cycle"}
        seen.add(id(value))
        try:
            result = {}
            for key, item in value.items():
                child_path = f"{_path}.{key}" if _path else str(key)
                result[str(key)] = serialize_value(item, ctx, _depth=_depth + 1, _path=child_path, _seen=seen)
            return result
        finally:
            seen.discard(id(value))

    if isinstance(value, (list, tuple, set, frozenset)):
        if id(value) in seen:
            ctx.note("Cycle", "cyclic array", _path)
            return {"$type": "Cycle"}
        seen.add(id(value))
        try:
            return [
                serialize_value(item, ctx, _depth=_depth + 1, _path=f"{_path}[{index}]", _seen=seen)
                for index, item in enumerate(value)
            ]
        finally:
            seen.discard(id(value))

    # --- structured values (built-in and server-defined structures) -----------
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if id(value) in seen:
            ctx.note("Cycle", "cyclic structure", _path)
            return {"$type": "Cycle"}
        seen.add(id(value))
        try:
            result = {
                "$type": "Structure",
                "structure_type": f"{type(value).__module__}.{type(value).__qualname__}",
            }
            for item in dataclasses.fields(value):
                if item.name.startswith("_"):
                    continue
                child_path = f"{_path}.{item.name}" if _path else item.name
                result[item.name] = serialize_value(
                    getattr(value, item.name, None), ctx, _depth=_depth + 1, _path=child_path, _seen=seen
                )
            return result
        finally:
            seen.discard(id(value))

    attributes = getattr(value, "__dict__", None)
    if attributes:
        if id(value) in seen:
            ctx.note("Cycle", "cyclic object", _path)
            return {"$type": "Cycle"}
        seen.add(id(value))
        try:
            result = {
                "$type": "Structure",
                "structure_type": f"{type(value).__module__}.{type(value).__qualname__}",
            }
            for key, item in attributes.items():
                if key.startswith("_"):
                    continue
                result[key] = serialize_value(item, ctx, _depth=_depth + 1, _path=f"{_path}.{key}", _seen=seen)
            return result
        finally:
            seen.discard(id(value))

    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    ctx.note("UnsupportedPythonType", type_name, _path)
    return {"$type": "Unsupported", "python_type": type_name, "repr": safe_repr(value)}


def _serialize_variant(
    variant: ua.Variant,
    ctx: SerializationContext,
    depth: int,
    path: str,
    seen: set[int],
) -> dict[str, Any]:
    variant_type = variant_type_name(variant.VariantType)
    encoded = serialize_value(variant.Value, ctx, _depth=depth + 1, _path=path, _seen=seen)
    result: dict[str, Any] = {
        "$type": "Variant",
        "variant_type": variant_type,
        "is_array": bool(getattr(variant, "is_array", False)),
        "value": encoded,
    }
    dimensions = getattr(variant, "Dimensions", None)
    if dimensions:
        result["dimensions"] = list(dimensions)
    return result


def _serialize_extension_object(
    obj: ua.ExtensionObject,
    ctx: SerializationContext,
    depth: int,
    path: str,
    seen: set[int],
) -> dict[str, Any]:
    type_id = obj.TypeId.to_string() if isinstance(obj.TypeId, ua.NodeId) else None
    body = obj.Body
    # asyncua 2.1.0 keeps ExtensionObject.Encoding at its default (0) even for a
    # binary body, so the code is derived from the body type instead:
    # 0 = no/default encoding, 1 = ByteString (binary), 2 = XmlElement.
    encoding = 1 if isinstance(body, (bytes, bytearray, memoryview)) else int(getattr(obj, "Encoding", 0) or 0)
    result: dict[str, Any] = {
        "$type": "ExtensionObject",
        "type_id": type_id,
        "encoding": encoding,
    }
    if body is None:
        result["body"] = None
        result["body_encoding"] = "none"
        return result
    if isinstance(body, (bytes, bytearray, memoryview)):
        # A binary body cannot be decoded without the server type dictionary.
        result["body"] = base64.b64encode(bytes(body)).decode("ascii")
        result["body_encoding"] = "binary-base64"
        ctx.note(
            "UndecodedExtensionObject",
            f"binary ExtensionObject body for type {type_id or 'unknown'} was not decoded",
            path,
        )
        return result
    result["body"] = serialize_value(body, ctx, _depth=depth + 1, _path=f"{path}.Body", _seen=seen)
    result["body_encoding"] = "decoded"
    return result


def serialize_value_with_context(value: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Convenience wrapper returning the encoded value and unsupported entries."""
    ctx = SerializationContext()
    encoded = serialize_value(value, ctx)
    return encoded, ctx.unsupported


