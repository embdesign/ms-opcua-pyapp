"""Unit tests for attribute selection and decoding (no server involved)."""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_explorer.opcua.attributes import (
    decode_access_level,
    decode_access_restrictions,
    decode_attribute,
    decode_event_notifier,
    decode_permissions,
    decode_write_mask,
    read_node_attributes,
    specs_for,
)


def _data_value(value: object, variant_type: ua.VariantType) -> ua.DataValue:
    return ua.DataValue(ua.Variant(value, variant_type))


def _bad_data_value() -> ua.DataValue:
    data_value = ua.DataValue()
    data_value.StatusCode = ua.StatusCode(ua.StatusCodes.BadAttributeIdInvalid)
    return data_value


def _names(node_class: str, *, extended: bool) -> list[str]:
    return [spec.name for spec in specs_for(node_class, extended=extended)]


def test_download_specs_stay_unchanged() -> None:
    # A download must not start reading the details-only attributes: exports stay
    # exactly as before and the read volume stays small.
    assert _names("Object", extended=False) == ["NodeClass", "BrowseName", "DisplayName", "Description"]
    variable = _names("Variable", extended=False)
    assert variable[:4] == ["NodeClass", "BrowseName", "DisplayName", "Description"]
    assert variable[4:] == ["DataType", "ValueRank", "ArrayDimensions", "AccessLevel", "UserAccessLevel"]
    assert _names("Method", extended=False)[4:] == ["Executable", "UserExecutable"]


def test_extended_specs_cover_the_standard_attributes_per_node_class() -> None:
    obj = _names("Object", extended=True)
    assert "EventNotifier" in obj
    assert "WriteMask" in obj and "UserWriteMask" in obj
    assert "MinimumSamplingInterval" not in obj
    assert "IsAbstract" not in obj

    view = _names("View", extended=True)
    assert {"EventNotifier", "ContainsNoLoops"} <= set(view)

    variable = _names("Variable", extended=True)
    assert {"MinimumSamplingInterval", "Historizing"} <= set(variable)
    assert "EventNotifier" not in variable

    reference_type = _names("ReferenceType", extended=True)
    assert {"IsAbstract", "Symmetric", "InverseName"} <= set(reference_type)

    data_type = _names("DataType", extended=True)
    assert {"IsAbstract", "DataTypeDefinition"} <= set(data_type)

    object_type = _names("ObjectType", extended=True)
    assert "IsAbstract" in object_type

    # Optional attributes are requested for every node class.
    assert {"AccessRestrictions", "RolePermissions", "UserRolePermissions"} <= set(obj)


def test_event_notifier_is_decoded_into_its_member_names() -> None:
    result = decode_attribute("EventNotifier", _data_value(1, ua.VariantType.Byte))

    assert result.value == 1
    assert result.display == "1 (SubscribeToEvents)"
    assert decode_event_notifier(0) == []
    assert decode_event_notifier(1) == ["SubscribeToEvents"]
    assert decode_event_notifier(None) == []


def test_access_level_now_has_a_display_string() -> None:
    result = decode_attribute("AccessLevel", _data_value(3, ua.VariantType.Byte))

    assert result.value == 3
    assert result.display == "3 (CurrentRead, CurrentWrite)"
    assert decode_access_level(3) == ["CurrentRead", "CurrentWrite"]


def test_write_mask_and_access_restrictions_use_bit_masks() -> None:
    mask = int(ua.AttributeWriteMask.AccessLevel) | int(ua.AttributeWriteMask.EventNotifier)
    result = decode_attribute("WriteMask", _data_value(mask, ua.VariantType.UInt32))

    assert result.value == mask
    assert "AccessLevel" in (result.display or "")
    assert "EventNotifier" in (result.display or "")
    assert decode_write_mask(mask) == ["AccessLevel", "EventNotifier"]
    assert decode_access_restrictions(3) == ["SigningRequired", "EncryptionRequired"]

    empty = decode_attribute("UserWriteMask", _data_value(0, ua.VariantType.UInt32))
    assert empty.display == "0 (none)"


def test_boolean_and_duration_attributes_decode_to_native_types() -> None:
    historizing = decode_attribute("Historizing", _data_value(False, ua.VariantType.Boolean))
    assert historizing.value is False

    interval = decode_attribute("MinimumSamplingInterval", _data_value(250.5, ua.VariantType.Double))
    assert interval.value == pytest.approx(250.5)
    assert isinstance(interval.value, float)


def test_role_permissions_render_role_and_permission_names() -> None:
    permission = ua.RolePermissionType()
    permission.RoleId = ua.NodeId(15656)
    permission.Permissions = 33  # Browse | Read

    result = decode_attribute(
        "RolePermissions", _data_value([permission], ua.VariantType.ExtensionObject)
    )

    assert result.display == "i=15656: Browse, Read"
    assert decode_permissions(33) == ["Browse", "Read"]
    assert decode_permissions(0) == []


class _AttributeSession:
    def __init__(self, values: dict[ua.AttributeIds, ua.DataValue]) -> None:
        self.values = values
        self.requests: list[ua.AttributeIds] = []

    async def read(self, params: ua.ReadParameters) -> list[ua.DataValue]:
        results: list[ua.DataValue] = []
        for read_value in params.NodesToRead:
            attribute = ua.AttributeIds(int(read_value.AttributeId))
            self.requests.append(attribute)
            results.append(self.values.get(attribute, _data_value(None, ua.VariantType.Null)))
        return results


async def test_extended_read_hides_optional_attributes_the_server_rejects() -> None:
    session = _AttributeSession(
        {
            ua.AttributeIds.EventNotifier: _data_value(1, ua.VariantType.Byte),
            ua.AttributeIds.AccessRestrictions: _bad_data_value(),
            ua.AttributeIds.RolePermissions: _bad_data_value(),
            ua.AttributeIds.UserRolePermissions: _bad_data_value(),
        }
    )

    record, attributes = await read_node_attributes(
        session, "i=2253", node_class="Object", include_value=False, extended=True
    )

    names = [attribute.attribute for attribute in attributes]
    assert "EventNotifier" in names
    assert "WriteMask" in names and "UserWriteMask" in names
    assert "AccessRestrictions" not in names
    assert "RolePermissions" not in names
    assert "UserRolePermissions" not in names
    assert record.unavailable == {}
