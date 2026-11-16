"""Unit tests for server-driven type resolution (enums and structures, no server required)."""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_explorer.opcua.type_support import TypeUnsupported, resolve_value_type
from opcua_explorer.opcua.value_parsing import ValueParseError

ENUM_ID = ua.NodeId(3001, 2)
STRUCT_ID = ua.NodeId(5001, 2)
ENCODING_ID = ua.NodeId(5002, 2)


class FakeNode:
    def __init__(
        self,
        attributes: dict[int, object] | None = None,
        children: dict[str, object] | None = None,
    ) -> None:
        self.attributes = dict(attributes or {})
        self.children = dict(children or {})

    async def read_attribute(self, attribute: int, raise_on_bad_status: bool = True) -> ua.DataValue:
        if attribute in self.attributes:
            return ua.DataValue(ua.Variant(self.attributes[attribute]))
        if not raise_on_bad_status:
            return ua.DataValue(ua.Variant(None))
        raise ua.UaStatusCodeError(ua.StatusCodes.BadAttributeIdInvalid)

    async def get_child(self, path: list[str]) -> object:
        name = path[0]
        if name in self.children:
            return self.children[name]
        raise ua.UaStatusCodeError(ua.StatusCodes.BadNoMatch)


class FakeProperty:
    def __init__(self, value: object) -> None:
        self._value = value

    async def read_value(self) -> object:
        return self._value


class FakeClient:
    def __init__(self, nodes: dict[str, FakeNode]) -> None:
        self.nodes = nodes

    def get_node(self, node_id: object) -> FakeNode:
        return self.nodes[str(node_id)]


def _enum_definition(*values: tuple[int, str]) -> ua.EnumDefinition:
    return ua.EnumDefinition(
        Fields=[
            ua.EnumField(Value=number, DisplayName=ua.LocalizedText(text), Description=ua.LocalizedText(""))
            for number, text in values
        ]
    )


def _structure_field(name: str, data_type: ua.NodeId, value_rank: int = -1, optional: bool = False) -> ua.StructureField:
    return ua.StructureField(
        Name=name,
        Description=ua.LocalizedText(""),
        DataType=data_type,
        ValueRank=value_rank,
        ArrayDimensions=None,
        MaxStringLength=0,
        IsOptional=optional,
    )


def _structure_definition(
    fields: list[ua.StructureField],
    *,
    encoding_id: ua.NodeId = ENCODING_ID,
    structure_type: ua.StructureType | None = None,
) -> ua.StructureDefinition:
    return ua.StructureDefinition(
        DefaultEncodingId=encoding_id,
        BaseDataType=ua.NodeId(22),
        StructureType=structure_type if structure_type is not None else ua.StructureType.Structure,
        Fields=fields,
    )


def _double() -> ua.NodeId:
    return ua.NodeId(ua.ObjectIds.Double)


def _string() -> ua.NodeId:
    return ua.NodeId(ua.ObjectIds.String)


def _int32() -> ua.NodeId:
    return ua.NodeId(ua.ObjectIds.Int32)


async def test_builtin_types_pass_through_to_the_existing_parser():
    value_type = await resolve_value_type(FakeClient({}), _double(), -1)

    assert value_type.kind == "builtin"
    assert value_type.label() == "Double"
    assert value_type.parse("2.5") == 2.5


async def test_enum_from_data_type_definition_accepts_name_number_and_case():
    client = FakeClient({ENUM_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: _enum_definition((0, "Off"), (1, "On"))})})

    value_type = await resolve_value_type(client, ENUM_ID, -1)

    assert value_type.kind == "enum"
    assert value_type.variant_type == ua.VariantType.Int32
    assert value_type.parse("On") == 1
    assert value_type.parse("off") == 0
    assert value_type.parse("1") == 1
    assert "enumeration" in value_type.label()


async def test_enum_falls_back_to_enum_strings_properties():
    node = FakeNode(children={"EnumStrings": FakeProperty([ua.LocalizedText("Low"), ua.LocalizedText("High")])})

    value_type = await resolve_value_type(FakeClient({ENUM_ID.to_string(): node}), ENUM_ID, -1)

    assert value_type.kind == "enum"
    assert value_type.parse("High") == 1
    assert value_type.parse("0") == 0


async def test_unknown_enum_name_lists_the_known_values():
    client = FakeClient({ENUM_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: _enum_definition((0, "Off"), (1, "On"))})})
    value_type = await resolve_value_type(client, ENUM_ID, -1)

    with pytest.raises(ValueParseError, match="known values: Off, On"):
        value_type.parse("Maybe")


async def test_array_of_enums_is_parsed_elementwise():
    client = FakeClient({ENUM_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: _enum_definition((0, "Off"), (1, "On"))})})
    value_type = await resolve_value_type(client, ENUM_ID, 1)

    assert value_type.parse('["On", "Off"]') == [1, 0]
    with pytest.raises(ValueParseError, match="Element 2"):
        value_type.parse('["On", "Nope"]')


async def test_custom_type_without_a_definition_is_refused():
    client = FakeClient({ENUM_ID.to_string(): FakeNode()})

    with pytest.raises(TypeUnsupported, match="cannot be written safely"):
        await resolve_value_type(client, ENUM_ID, -1)


async def test_structure_is_built_and_encoded_as_an_extension_object():
    definition = _structure_definition(
        [
            _structure_field("X", _double()),
            _structure_field("Name", _string()),
            _structure_field("Count", _int32()),
            _structure_field("Tags", _string(), value_rank=1),
        ]
    )
    client = FakeClient({STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: definition})})

    value_type = await resolve_value_type(client, STRUCT_ID, -1)
    encoded = value_type.parse('{"X": 1.5, "Name": "a", "Count": 2, "Tags": ["x", "y"]}')

    assert value_type.kind == "structure"
    assert value_type.variant_type == ua.VariantType.ExtensionObject
    assert isinstance(encoded, ua.ExtensionObject)
    assert encoded.TypeId.to_string() == ENCODING_ID.to_string()
    assert encoded.Body


async def test_structure_rejects_unknown_and_missing_fields():
    definition = _structure_definition([_structure_field("X", _double())])
    client = FakeClient({STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: definition})})
    value_type = await resolve_value_type(client, STRUCT_ID, -1)

    with pytest.raises(ValueParseError, match="Unknown field"):
        value_type.parse('{"Y": 1}')
    with pytest.raises(ValueParseError, match="Missing field"):
        value_type.parse("{}")
    with pytest.raises(ValueParseError, match="JSON object"):
        value_type.parse("[1, 2]")


async def test_structure_field_type_errors_name_the_field():
    definition = _structure_definition([_structure_field("X", _double())])
    client = FakeClient({STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: definition})})
    value_type = await resolve_value_type(client, STRUCT_ID, -1)

    with pytest.raises(ValueParseError, match="Field X: expected a number"):
        value_type.parse('{"X": "fast"}')


async def test_structure_with_an_enum_field_accepts_the_enum_name():
    enum_definition = _enum_definition((0, "Off"), (1, "On"))
    structure = _structure_definition([_structure_field("Mode", ENUM_ID)])
    client = FakeClient(
        {
            STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: structure}),
            ENUM_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: enum_definition}),
        }
    )

    value_type = await resolve_value_type(client, STRUCT_ID, -1)
    encoded = value_type.parse('{"Mode": "On"}')

    assert isinstance(encoded, ua.ExtensionObject) and encoded.Body


async def test_nested_structure_is_encoded_recursively():
    inner = _structure_definition([_structure_field("Value", _double())], encoding_id=ua.NodeId(6002, 2))
    outer = _structure_definition([_structure_field("Inner", ua.NodeId(6001, 2)), _structure_field("Name", _string())])
    client = FakeClient(
        {
            STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: outer}),
            "ns=2;i=6001": FakeNode({ua.AttributeIds.DataTypeDefinition: inner}),
        }
    )

    value_type = await resolve_value_type(client, STRUCT_ID, -1)
    encoded = value_type.parse('{"Inner": {"Value": 3.5}, "Name": "outer"}')

    assert isinstance(encoded, ua.ExtensionObject) and encoded.Body


async def test_structure_without_encoding_id_is_refused():
    definition = _structure_definition([_structure_field("X", _double())], encoding_id=ua.NodeId())
    client = FakeClient({STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: definition})})

    with pytest.raises(TypeUnsupported, match="DefaultEncodingId"):
        await resolve_value_type(client, STRUCT_ID, -1)


async def test_union_and_optional_field_structures_are_refused():
    definition = _structure_definition(
        [_structure_field("X", _double())], structure_type=ua.StructureType.Union
    )
    client = FakeClient({STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: definition})})

    with pytest.raises(TypeUnsupported, match="not supported"):
        await resolve_value_type(client, STRUCT_ID, -1)


async def test_matrix_field_is_refused():
    definition = _structure_definition([_structure_field("Grid", _double(), value_rank=2)])
    client = FakeClient({STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: definition})})

    with pytest.raises(TypeUnsupported, match="matrix"):
        await resolve_value_type(client, STRUCT_ID, -1)


async def test_recursive_structure_is_refused_instead_of_recursing_forever():
    definition = _structure_definition([_structure_field("Self", STRUCT_ID)])
    client = FakeClient({STRUCT_ID.to_string(): FakeNode({ua.AttributeIds.DataTypeDefinition: definition})})

    with pytest.raises(TypeUnsupported, match="contains itself"):
        await resolve_value_type(client, STRUCT_ID, -1)
