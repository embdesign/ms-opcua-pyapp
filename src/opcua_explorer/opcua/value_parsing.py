"""Turn operator-entered text into a typed value for a guarded write or method call.

The DataType and ValueRank always come from the **server** (DataType attribute,
``InputArguments``), never from a guess, so a value that does not fit is rejected
locally instead of being sent as a wrong-typed write.

Supported scalar DataTypes are the built-in ones. Anything else (structures,
enumerations with a custom DataType, custom types) is refused with an explicit
message: a guarded test must not send a value whose encoding it cannot verify.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

from asyncua import ua


class ValueParseError(ValueError):
    """The entered text cannot be converted to the server's DataType."""


_INTEGER_BOUNDS: dict[int, tuple[int, int]] = {
    ua.ObjectIds.SByte: (-128, 127),
    ua.ObjectIds.Byte: (0, 255),
    ua.ObjectIds.Int16: (-32768, 32767),
    ua.ObjectIds.UInt16: (0, 65535),
    ua.ObjectIds.Int32: (-2147483648, 2147483647),
    ua.ObjectIds.UInt32: (0, 4294967295),
    ua.ObjectIds.Int64: (-9223372036854775808, 9223372036854775807),
    ua.ObjectIds.UInt64: (0, 18446744073709551615),
}

_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled"}


def _strip_quotes(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _builtin_identifier(data_type: ua.NodeId) -> int | None:
    """The namespace-0 numeric identifier of a built-in DataType, or ``None``.

    Note: the NodeId type (TwoByte/FourByte/Numeric) is irrelevant here - only the
    identifier matters. Custom DataTypes have a zero identifier only in namespace 0.
    """
    if data_type.NamespaceIndex != 0:
        return None
    identifier = data_type.Identifier
    return identifier if isinstance(identifier, int) else None


def _parse_datetime(text: str) -> datetime:
    raw = _strip_quotes(text)
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueParseError(f"{text!r} is not an ISO 8601 date/time (expected e.g. 2024-05-01T12:00:00Z)") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_scalar(text: str, data_type: ua.NodeId) -> Any:
    """Parse ``text`` as a value of ``data_type`` (a NodeId from the server)."""
    identifier = _builtin_identifier(data_type)
    if identifier is None:
        raise ValueParseError(f"DataType {data_type.to_string()} is not a built-in type; it cannot be written safely")

    if identifier == ua.ObjectIds.Boolean:
        lowered = text.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueParseError(f"{text!r} is not a boolean (use true/false)")

    if identifier in _INTEGER_BOUNDS:
        lowered = _strip_quotes(text).lower()
        try:
            number = int(lowered, 0)
        except ValueError as exc:
            raise ValueParseError(f"{text!r} is not an integer") from exc
        low, high = _INTEGER_BOUNDS[identifier]
        if not low <= number <= high:
            raise ValueParseError(f"{number} is outside the range of {ua.VariantType(identifier).name} ({low}..{high})")
        return number

    if identifier == ua.ObjectIds.Float:
        try:
            return float(text)
        except ValueError as exc:
            raise ValueParseError(f"{text!r} is not a number") from exc

    if identifier == ua.ObjectIds.Double:
        try:
            return float(text)
        except ValueError as exc:
            raise ValueParseError(f"{text!r} is not a number") from exc

    if identifier == ua.ObjectIds.String:
        return _strip_quotes(text)

    if identifier == ua.ObjectIds.DateTime:
        return _parse_datetime(text)

    if identifier == ua.ObjectIds.Guid:
        try:
            return ua.Guid(_strip_quotes(text))
        except Exception as exc:
            raise ValueParseError(f"{text!r} is not a GUID") from exc

    if identifier == ua.ObjectIds.ByteString:
        try:
            return base64.b64decode(_strip_quotes(text), validate=True)
        except Exception as exc:
            raise ValueParseError(f"{text!r} is not valid base64") from exc

    if identifier == ua.ObjectIds.NodeId:
        try:
            return ua.NodeId.from_string(_strip_quotes(text))
        except Exception as exc:
            raise ValueParseError(f"{text!r} is not a NodeId") from exc

    if identifier == ua.ObjectIds.ExpandedNodeId:
        try:
            return ua.ExpandedNodeId.from_string(_strip_quotes(text))
        except Exception as exc:
            raise ValueParseError(f"{text!r} is not an ExpandedNodeId") from exc

    if identifier == ua.ObjectIds.StatusCode:
        try:
            return ua.StatusCode(int(_strip_quotes(text), 0))
        except Exception as exc:
            raise ValueParseError(f"{text!r} is not a StatusCode") from exc

    if identifier == ua.ObjectIds.QualifiedName:
        raw = _strip_quotes(text)
        match = re.fullmatch(r"(?:(\d+):)?(.+)", raw)
        if not match:
            raise ValueParseError(f"{text!r} is not a QualifiedName (use name or ns:name)")
        namespace_index = int(match.group(1) or 0)
        return ua.QualifiedName(match.group(2), namespace_index)

    if identifier == ua.ObjectIds.LocalizedText:
        return ua.LocalizedText(_strip_quotes(text))

    if identifier == ua.ObjectIds.XmlElement:
        return _strip_quotes(text)

    name = ua.VariantType(identifier).name if 0 < identifier <= 25 else str(identifier)
    raise ValueParseError(f"DataType {name} is not supported for guarded test writes")


def parse_value_text(text: str, data_type: ua.NodeId, value_rank: int) -> Any:
    """Parse ``text`` for ``value_rank``: 0/-1 scalar, 1..n a JSON array."""
    if value_rank in (0, -1):
        return parse_scalar(text, data_type)

    raw = _strip_quotes(text)
    try:
        items = json.loads(raw)
    except ValueError as exc:
        raise ValueParseError(f"An array value is expected as a JSON array, for example [1, 2, 3] ({exc})") from exc
    if not isinstance(items, list):
        raise ValueParseError("An array value is expected as a JSON array, for example [1, 2, 3]")
    if value_rank == 1:
        return [parse_scalar(item if isinstance(item, str) else json.dumps(item), data_type) for item in items]
    # Matrix ranks (n > 1) are legal in OPC UA but a flat JSON list would silently
    # lose the dimensions, so this is refused rather than guessed.
    raise ValueParseError(f"ValueRank {value_rank} (matrix) is not supported for guarded test writes")


def describe_data_type(data_type: ua.NodeId) -> str:
    """Short, human-readable DataType label for confirmations and reports."""
    identifier = _builtin_identifier(data_type)
    if identifier is not None and 0 < identifier <= 25:
        return ua.VariantType(identifier).name
    return data_type.to_string()
