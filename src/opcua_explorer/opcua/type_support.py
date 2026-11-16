"""Resolve a server DataType into a value that can actually be written.

A write must never guess an encoding: the value's type comes from the server. Three cases
exist and all three are resolved here:

* a **built-in** DataType (namespace 0, identifier 1..25) is parsed by ``value_parsing``;
* an **Enumeration** is parsed from its ``DataTypeDefinition`` (``EnumDefinition``) or, on
  servers that predate it, from its ``EnumStrings`` / ``EnumValues`` property. Enums travel as
  ``Int32`` and may be entered by name or by number;
* a **Structure** is parsed from its ``DataTypeDefinition`` (``StructureDefinition``), encoded
  with the structure's own ``DefaultEncodingId`` and sent as an ``ExtensionObject``.

Anything else (union, optional-field structure, matrix, unreadable definition) is refused with
an operator-facing reason instead of being encoded on a guess.

Verified against asyncua 2.1.0 (see ``docs/api-verification.md``): ``ua.AttributeIds.
DataTypeDefinition``, ``ua.EnumDefinition``/``ua.StructureDefinition``/``ua.StructureField``,
``ua_binary.struct_to_binary`` and ``ua.ExtensionObject(TypeId, Encoding, Body=bytes)``.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from asyncua import ua
from asyncua.ua import ua_binary

from .value_parsing import ARRAY_RANKS, SCALAR_OR_ARRAY_RANKS, ValueParseError, looks_like_array, parse_value_text

#: Depth and size guards: a malicious or broken server must not make the client recurse forever.
MAX_TYPE_DEPTH = 6
MAX_STRUCT_FIELDS = 200

_BUILTIN_MAX_ID = 25


class TypeUnsupported(ValueError):
    """The server DataType cannot be encoded safely; nothing was sent."""


def _unquote(text: str) -> str:
    value = (text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _builtin_identifier(data_type: ua.NodeId) -> int | None:
    """Namespace-0 numeric identifier of a built-in DataType, or ``None`` for anything else."""
    if data_type.NamespaceIndex != 0:
        return None
    identifier = data_type.Identifier
    return identifier if isinstance(identifier, int) else None


def _is_builtin(data_type: ua.NodeId) -> bool:
    identifier = _builtin_identifier(data_type)
    return identifier is not None and 0 < identifier <= _BUILTIN_MAX_ID


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer")
    return value


def _to_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    return float(value)


def _to_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected true or false")
    return value


def _to_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        raise ValueError("expected an ISO 8601 date/time string")
    raw = value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not an ISO 8601 date/time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _to_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise ValueError("expected base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError(f"{value!r} is not valid base64") from exc


def _to_node_id(value: Any) -> ua.NodeId:
    if isinstance(value, ua.NodeId):
        return value
    if not isinstance(value, str):
        raise ValueError("expected a NodeId string")
    try:
        return ua.NodeId.from_string(value)
    except Exception as exc:
        raise ValueError(f"{value!r} is not a NodeId") from exc


def _to_localized_text(value: Any) -> ua.LocalizedText:
    if isinstance(value, ua.LocalizedText):
        return value
    if isinstance(value, str):
        return ua.LocalizedText(value)
    if isinstance(value, dict):
        locale = value.get("locale")
        return ua.LocalizedText(str(value.get("text", "")), str(locale) if locale is not None else None)
    raise ValueError('expected text or {"text": ..., "locale": ...}')


def _to_qualified_name(value: Any) -> ua.QualifiedName:
    if isinstance(value, ua.QualifiedName):
        return value
    if isinstance(value, dict):
        return ua.QualifiedName(str(value.get("name", "")), int(value.get("namespace_index", 0)))
    if isinstance(value, str):
        return ua.QualifiedName(value, 0)
    raise ValueError('expected a name or {"name": ..., "namespace_index": ...}')


def _to_status_code(value: Any) -> ua.StatusCode:
    if isinstance(value, ua.StatusCode):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return ua.StatusCode(value)
    if isinstance(value, str):
        try:
            return ua.StatusCode(int(value, 0))
        except Exception as exc:
            raise ValueError(f"{value!r} is not a StatusCode") from exc
    raise ValueError("expected a StatusCode number")


#: Built-in scalar DataTypes that can appear as a structure field or as a written value.
_SCALARS: dict[int, tuple[Any, Callable[[Any], Any], str]] = {
    ua.ObjectIds.Boolean: (ua.Boolean, _to_bool, "Boolean"),
    ua.ObjectIds.SByte: (ua.SByte, _to_int, "SByte"),
    ua.ObjectIds.Byte: (ua.Byte, _to_int, "Byte"),
    ua.ObjectIds.Int16: (ua.Int16, _to_int, "Int16"),
    ua.ObjectIds.UInt16: (ua.UInt16, _to_int, "UInt16"),
    ua.ObjectIds.Int32: (ua.Int32, _to_int, "Int32"),
    ua.ObjectIds.UInt32: (ua.UInt32, _to_int, "UInt32"),
    ua.ObjectIds.Int64: (ua.Int64, _to_int, "Int64"),
    ua.ObjectIds.UInt64: (ua.UInt64, _to_int, "UInt64"),
    ua.ObjectIds.Float: (ua.Float, _to_float, "Float"),
    ua.ObjectIds.Double: (ua.Double, _to_float, "Double"),
    ua.ObjectIds.String: (ua.String, _to_str, "String"),
    ua.ObjectIds.DateTime: (ua.DateTime, _to_datetime, "DateTime"),
    ua.ObjectIds.ByteString: (ua.ByteString, _to_bytes, "ByteString"),
    ua.ObjectIds.StatusCode: (ua.StatusCode, _to_status_code, "StatusCode"),
    ua.ObjectIds.QualifiedName: (ua.QualifiedName, _to_qualified_name, "QualifiedName"),
    ua.ObjectIds.LocalizedText: (ua.LocalizedText, _to_localized_text, "LocalizedText"),
    ua.ObjectIds.NodeId: (ua.NodeId, _to_node_id, "NodeId"),
}


@dataclasses.dataclass(slots=True)
class EnumType:
    """An enumeration resolved from the server, with names when the server provides them."""

    data_type: ua.NodeId
    name: str
    by_name: dict[str, int] = dataclasses.field(default_factory=dict)
    by_value: dict[int, str] = dataclasses.field(default_factory=dict)

    def value_for(self, value: Any) -> int:
        """Accept a name (case-insensitive) or a number; reject anything else."""
        if isinstance(value, bool):
            raise ValueError("expected an enum name or number")
        if isinstance(value, int):
            return value
        if not isinstance(value, str):
            raise ValueError("expected an enum name or number")
        text = _unquote(value)
        if text in self.by_name:
            return self.by_name[text]
        lowered = {name.lower(): number for name, number in self.by_name.items()}
        if text.lower() in lowered:
            return lowered[text.lower()]
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        known = ", ".join(sorted(self.by_name)) or "none reported by the server"
        raise ValueError(f"{text!r} is not a value of {self.name}; known values: {known}")

    def label(self) -> str:
        return f"enumeration {self.name} ({len(self.by_name)} named value(s))"


@dataclasses.dataclass(slots=True)
class FieldSpec:
    """One structure field: its name, its annotation and how to convert JSON into it."""

    name: str
    data_type: ua.NodeId
    value_rank: int
    annotation: Any
    convert: Callable[[Any], Any]
    description: str = ""


@dataclasses.dataclass(slots=True)
class StructureType:
    """A structure resolved from the server, ready to build instances and encode them."""

    data_type: ua.NodeId
    name: str
    encoding_id: ua.NodeId
    python_type: type
    fields: list[FieldSpec] = dataclasses.field(default_factory=list)

    def build(self, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueParseError(
                f"A {self.name} value is expected as a JSON object, for example "
                + json.dumps({field.name: "..." for field in self.fields[:2]})
            )
        known = {field.name: field for field in self.fields}
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise ValueParseError(
                f"Unknown field(s) {', '.join(unknown)} for {self.name}; "
                f"expected {', '.join(known) or 'none'}"
            )
        missing = [name for name in known if name not in data]
        if missing:
            raise ValueParseError(f"Missing field(s) {', '.join(missing)} for {self.name}")
        values: dict[str, Any] = {}
        for name, field in known.items():
            try:
                values[name] = field.convert(data[name])
            except (ValueError, TypeUnsupported) as exc:
                raise ValueParseError(f"Field {name}: {exc}") from exc
        return self.python_type(**values)

    def encode(self, data: Any) -> ua.ExtensionObject:
        instance = self.build(data)
        try:
            body = ua_binary.struct_to_binary(instance)
        except Exception as exc:
            raise ValueParseError(f"{self.name} could not be encoded: {type(exc).__name__}: {exc}") from exc
        return ua.ExtensionObject(TypeId=self.encoding_id, Body=body)

    def label(self) -> str:
        return f"structure {self.name} ({len(self.fields)} field(s))"


@dataclasses.dataclass(slots=True)
class ValueType:
    """The resolved wire type of a value plus its text parser."""

    kind: str  # "builtin" | "enum" | "structure"
    data_type: ua.NodeId
    variant_type: ua.VariantType
    value_rank: int = -1
    enum: EnumType | None = None
    structure: StructureType | None = None

    def label(self) -> str:
        if self.kind == "enum" and self.enum is not None:
            return self.enum.label()
        if self.kind == "structure" and self.structure is not None:
            return self.structure.label()
        return ua.VariantType(self.variant_type).name

    def parse(self, text: str) -> Any:
        """Parse operator text for this type; ``ValueParseError`` means nothing may be sent."""
        if self.kind == "builtin":
            return parse_value_text(text, self.data_type, self.value_rank)
        if self.kind == "enum" and self.enum is not None:
            return self._parse_enum(text)
        if self.kind == "structure" and self.structure is not None:
            return self._parse_structure(text)
        raise ValueParseError(f"{self.label()} is not supported for writes")  # pragma: no cover - defensive

    def element_annotation(self) -> Any:
        """Annotation for one element when this type is a structure field (never a list)."""
        if self.kind == "builtin":
            return _scalar_annotation(self.data_type)[0]
        if self.kind == "enum":
            return ua.Int32
        if self.structure is not None:
            return self.structure.python_type
        raise TypeUnsupported(f"{self.label()} is not supported inside a structure")

    def element_converter(self) -> Callable[[Any], Any]:
        if self.kind == "builtin":
            scalar = _scalar_annotation(self.data_type)[1]
        elif self.enum is not None:
            enum = self.enum

            def scalar(value: Any) -> Any:
                return enum.value_for(value)

        elif self.structure is not None:
            structure = self.structure

            def scalar(value: Any) -> Any:
                return structure.build(value)

        else:  # pragma: no cover - defensive
            raise TypeUnsupported(f"{self.label()} is not supported inside a structure")

        if self.value_rank not in ARRAY_RANKS and self.value_rank not in SCALAR_OR_ARRAY_RANKS:
            return scalar

        def converted(value: Any) -> Any:
            if not isinstance(value, list):
                if self.value_rank in SCALAR_OR_ARRAY_RANKS:
                    return scalar(value)
                raise ValueError("expected a JSON array")
            return [scalar(item) for item in value]

        return converted

    def _expects_array(self, text: str) -> bool:
        """Whether ``text`` must be read as a list for this ValueRank."""
        if self.value_rank in SCALAR_OR_ARRAY_RANKS:
            return looks_like_array(text)
        return self.value_rank in ARRAY_RANKS

    def _parse_enum(self, text: str) -> Any:
        assert self.enum is not None
        if not self._expects_array(text):
            try:
                return self.enum.value_for(text)
            except ValueError as exc:
                raise ValueParseError(str(exc)) from exc
        items = _load_json_array(text)
        parsed: list[int] = []
        for index, item in enumerate(items, start=1):
            try:
                parsed.append(self.enum.value_for(item))
            except ValueError as exc:
                raise ValueParseError(f"Element {index}: {exc}") from exc
        return parsed

    def _parse_structure(self, text: str) -> Any:
        assert self.structure is not None
        if not self._expects_array(text):
            return self.structure.encode(_load_json_object(text, self.structure.name))
        items = _load_json_array(text)
        return [self.structure.encode(item) for item in items]


def _load_json_object(text: str, name: str) -> dict[str, Any]:
    try:
        data = json.loads(_unquote(text))
    except ValueError as exc:
        raise ValueParseError(f"A {name} value is expected as a JSON object ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueParseError(f"A {name} value is expected as a JSON object")
    return data


def _load_json_array(text: str) -> list[Any]:
    try:
        data = json.loads(_unquote(text))
    except ValueError as exc:
        raise ValueParseError(f"An array value is expected as a JSON array ({exc})") from exc
    if not isinstance(data, list):
        raise ValueParseError("An array value is expected as a JSON array")
    return data


async def _read_attribute_value(node: Any, attribute: int) -> Any:
    try:
        result = await node.read_attribute(attribute, raise_on_bad_status=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if result is None or result.Value is None or result.Value.Value is None:
        return None
    if not result.StatusCode.is_good():
        return None
    return result.Value.Value


def _unwrap_definition(definition: Any) -> Any:
    """Accept a decoded definition or an ``ExtensionObject`` that still wraps one."""
    if isinstance(definition, ua.ExtensionObject):
        body = definition.Body
        return body if isinstance(body, (ua.EnumDefinition, ua.StructureDefinition)) else None
    return definition


def _enum_from_definition(data_type: ua.NodeId, name: str, definition: ua.EnumDefinition) -> EnumType:
    by_name: dict[str, int] = {}
    by_value: dict[int, str] = {}
    for field in definition.Fields or []:
        value = int(field.Value)
        label = ""
        display = getattr(field, "DisplayName", None)
        if display is not None:
            label = str(getattr(display, "Text", "") or "")
        if label:
            by_name[label] = value
            by_value[value] = label
    return EnumType(data_type=data_type, name=name, by_name=by_name, by_value=by_value)


async def _enum_from_properties(node: Any, data_type: ua.NodeId, name: str) -> EnumType | None:
    by_name: dict[str, int] = {}
    by_value: dict[int, str] = {}
    for property_name in ("EnumStrings", "EnumValues"):
        try:
            prop = await node.get_child([property_name])
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        try:
            values = await prop.read_value()
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        if not values:
            continue
        for index, item in enumerate(values):
            if property_name == "EnumStrings":
                label = str(getattr(item, "Text", item) or "")
                number = index
            else:
                number = int(getattr(item, "Value", index))
                display = getattr(item, "DisplayName", None)
                label = str(getattr(display, "Text", "") or "")
            if label:
                by_name[label] = number
                by_value[number] = label
        if by_name:
            return EnumType(data_type=data_type, name=name, by_name=by_name, by_value=by_value)
    return None


async def _field_spec(
    client: Any,
    field: ua.StructureField,
    depth: int,
    seen: frozenset[str],
) -> FieldSpec:
    name = str(field.Name or "")
    if not isinstance(field.DataType, ua.NodeId):
        raise TypeUnsupported(f"field {name}: the definition names no DataType")
    value_rank = int(field.ValueRank if field.ValueRank is not None else -1)
    if value_rank > 1:
        raise TypeUnsupported(f"field {name}: ValueRank {value_rank} (matrix) is not supported")
    if value_rank in SCALAR_OR_ARRAY_RANKS:
        raise TypeUnsupported(
            f"field {name}: ValueRank {value_rank} (scalar or array) cannot be annotated for encoding"
        )
    field_type = await _resolve_type(client, field.DataType, value_rank, depth + 1, seen)
    element = field_type.element_annotation()
    annotation = list[element] if value_rank in ARRAY_RANKS else element
    convert = field_type.element_converter()
    return FieldSpec(
        name=name,
        data_type=field.DataType,
        value_rank=value_rank,
        annotation=annotation,
        convert=convert,
        description=field_type.label(),
    )


async def _structure_from_definition(
    client: Any,
    data_type: ua.NodeId,
    name: str,
    definition: ua.StructureDefinition,
    depth: int,
    seen: frozenset[str],
) -> StructureType:
    encoding_id = definition.DefaultEncodingId
    if not isinstance(encoding_id, ua.NodeId) or encoding_id.is_null():
        raise TypeUnsupported(f"{name}: the structure has no DefaultEncodingId, so it cannot be sent")
    structure_kind = getattr(definition, "StructureType", None)
    if structure_kind not in (None, ua.StructureType.Structure):
        raise TypeUnsupported(
            f"{name}: StructureType {structure_kind} (union or optional fields) is not supported"
        )
    fields_in = list(definition.Fields or [])
    if not fields_in:
        raise TypeUnsupported(f"{name}: the structure defines no fields")
    if len(fields_in) > MAX_STRUCT_FIELDS:
        raise TypeUnsupported(f"{name}: {len(fields_in)} fields exceeds the {MAX_STRUCT_FIELDS} field limit")
    specs = [await _field_spec(client, field, depth, seen) for field in fields_in]
    class_name = "UaStruct_" + re.sub(r"\W+", "_", data_type.to_string())
    try:
        python_type = dataclasses.make_dataclass(class_name, [(spec.name, spec.annotation) for spec in specs])
    except Exception as exc:  # duplicate names, unsupported annotation
        raise TypeUnsupported(f"{name}: the structure could not be prepared: {type(exc).__name__}: {exc}") from exc
    return StructureType(
        data_type=data_type,
        name=name,
        encoding_id=encoding_id,
        python_type=python_type,
        fields=list(specs),
    )


async def _resolve_type(
    client: Any,
    data_type: ua.NodeId,
    value_rank: int,
    depth: int,
    seen: frozenset[str],
) -> ValueType:
    if _is_builtin(data_type):
        return ValueType(
            kind="builtin",
            data_type=data_type,
            variant_type=ua.datatype_to_varianttype(data_type),
            value_rank=value_rank,
        )
    key = data_type.to_string()
    if key in seen:
        raise TypeUnsupported(f"{key} contains itself; the type definition is recursive")
    if depth > MAX_TYPE_DEPTH:
        raise TypeUnsupported(f"{key}: type nesting exceeds {MAX_TYPE_DEPTH} levels")
    seen = seen | {key}

    node = client.get_node(key)
    name = key
    definition = _unwrap_definition(await _read_attribute_value(node, ua.AttributeIds.DataTypeDefinition))

    if isinstance(definition, ua.EnumDefinition):
        enum = _enum_from_definition(data_type, name, definition)
        return ValueType(
            kind="enum", data_type=data_type, variant_type=ua.VariantType.Int32, value_rank=value_rank, enum=enum
        )
    if isinstance(definition, ua.StructureDefinition):
        structure = await _structure_from_definition(client, data_type, name, definition, depth, seen)
        return ValueType(
            kind="structure",
            data_type=data_type,
            variant_type=ua.VariantType.ExtensionObject,
            value_rank=value_rank,
            structure=structure,
        )

    enum = await _enum_from_properties(node, data_type, name)
    if enum is not None:
        return ValueType(
            kind="enum", data_type=data_type, variant_type=ua.VariantType.Int32, value_rank=value_rank, enum=enum
        )
    raise TypeUnsupported(
        f"{key} is not a built-in DataType and exposes no EnumDefinition, EnumStrings/EnumValues or "
        "StructureDefinition; it cannot be written safely"
    )


async def resolve_value_type(client: Any, data_type: ua.NodeId, value_rank: int) -> ValueType:
    """Resolve the wire type of ``data_type`` from the server; raise ``TypeUnsupported``."""
    return await _resolve_type(client, data_type, value_rank, 0, frozenset())


def _scalar_annotation(data_type: ua.NodeId) -> tuple[Any, Callable[[Any], Any], str]:
    identifier = _builtin_identifier(data_type)
    spec = _SCALARS.get(identifier) if identifier is not None else None
    if spec is None:
        raise TypeUnsupported(f"DataType {data_type.to_string()} is not supported inside a structure")
    return spec


__all__ = [
    "EnumType",
    "FieldSpec",
    "MAX_STRUCT_FIELDS",
    "MAX_TYPE_DEPTH",
    "StructureType",
    "TypeUnsupported",
    "ValueType",
    "resolve_value_type",
]
