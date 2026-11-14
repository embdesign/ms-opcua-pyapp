"""Unit tests for the shared asyncua name conversions."""

from __future__ import annotations

from asyncua import ua

from opcua_explorer.opcua.ua_names import node_class_name, status_name, variant_type_name


class _HasValue:
    def __init__(self, value: int) -> None:
        self.value = value


def test_node_class_name_accepts_enum_and_int() -> None:
    assert node_class_name(ua.NodeClass.Variable) == "Variable"
    assert node_class_name(int(ua.NodeClass.Object)) == "Object"


def test_node_class_name_is_defensive() -> None:
    assert node_class_name("not-a-node-class") == "not-a-node-class"


def test_status_name_accepts_status_code_and_value_holder() -> None:
    code = ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
    assert status_name(code) == "BadUserAccessDenied"
    assert status_name(_HasValue(int(ua.StatusCodes.BadUserAccessDenied))) == "BadUserAccessDenied"


def test_status_name_is_defensive() -> None:
    marker = object()
    assert status_name(marker) == str(marker)


def test_variant_type_name_accepts_enum_and_int() -> None:
    assert variant_type_name(ua.VariantType.Double) == "Double"
    assert variant_type_name(int(ua.VariantType.String)) == "String"


def test_variant_type_name_is_defensive() -> None:
    assert variant_type_name("custom") == "custom"
