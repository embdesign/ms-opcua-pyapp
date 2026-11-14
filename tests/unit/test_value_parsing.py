"""Unit tests for the guarded-write value parser (no server required)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from asyncua import ua

from opcua_explorer.opcua.value_parsing import (
    ValueParseError,
    describe_data_type,
    parse_scalar,
    parse_value_text,
)


def _dt(identifier: int) -> ua.NodeId:
    return ua.NodeId(identifier)


def test_boolean_accepts_common_spellings():
    for text in ("true", "TRUE", "1", "yes", "on"):
        assert parse_scalar(text, _dt(ua.ObjectIds.Boolean)) is True
    for text in ("false", "0", "no", "off"):
        assert parse_scalar(text, _dt(ua.ObjectIds.Boolean)) is False


def test_boolean_rejects_other_text():
    with pytest.raises(ValueParseError, match="not a boolean"):
        parse_scalar("maybe", _dt(ua.ObjectIds.Boolean))


def test_integers_are_range_checked():
    assert parse_scalar("42", _dt(ua.ObjectIds.Int32)) == 42
    assert parse_scalar("-7", _dt(ua.ObjectIds.Int16)) == -7
    assert parse_scalar("255", _dt(ua.ObjectIds.Byte)) == 255
    with pytest.raises(ValueParseError, match="outside the range"):
        parse_scalar("256", _dt(ua.ObjectIds.Byte))
    with pytest.raises(ValueParseError, match="not an integer"):
        parse_scalar("4.2", _dt(ua.ObjectIds.Int32))


def test_floats_and_strings():
    assert parse_scalar("21.5", _dt(ua.ObjectIds.Double)) == 21.5
    assert parse_scalar("'quoted text'", _dt(ua.ObjectIds.String)) == "quoted text"
    with pytest.raises(ValueParseError, match="not a number"):
        parse_scalar("fast", _dt(ua.ObjectIds.Float))


def test_datetime_accepts_iso_and_zulu_suffix():
    parsed = parse_scalar("2024-05-01T12:00:00Z", _dt(ua.ObjectIds.DateTime))
    assert parsed == datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueParseError, match="ISO 8601"):
        parse_scalar("yesterday", _dt(ua.ObjectIds.DateTime))


def test_bytes_nodeid_and_localized_text():
    assert parse_scalar("aGVsbG8=", _dt(ua.ObjectIds.ByteString)) == b"hello"
    assert parse_scalar("ns=2;s=Motor", _dt(ua.ObjectIds.NodeId)).to_string() == "ns=2;s=Motor"
    assert parse_scalar("Line 1", _dt(ua.ObjectIds.LocalizedText)).Text == "Line 1"
    with pytest.raises(ValueParseError, match="base64"):
        parse_scalar("not base64!", _dt(ua.ObjectIds.ByteString))


def test_arrays_are_parsed_from_json():
    assert parse_value_text("[1, 2, 3]", _dt(ua.ObjectIds.Int32), 1) == [1, 2, 3]
    assert parse_value_text("[]", _dt(ua.ObjectIds.Double), 1) == []
    with pytest.raises(ValueParseError, match="JSON array"):
        parse_value_text("1,2,3", _dt(ua.ObjectIds.Int32), 1)


def test_matrices_are_refused_instead_of_flattened():
    with pytest.raises(ValueParseError, match="matrix"):
        parse_value_text("[[1, 2], [3, 4]]", _dt(ua.ObjectIds.Double), 2)


def test_custom_data_type_is_refused():
    custom = ua.NodeId("MeasurementType", 2)
    with pytest.raises(ValueParseError, match="not a built-in type"):
        parse_scalar("1.0", custom)


def test_unsupported_builtin_is_refused():
    with pytest.raises(ValueParseError, match="not supported"):
        parse_scalar("1", _dt(ua.ObjectIds.DiagnosticInfo))


def test_describe_data_type_uses_variant_names():
    assert describe_data_type(_dt(ua.ObjectIds.Double)) == "Double"
    assert describe_data_type(ua.NodeId("MeasurementType", 2)) == "ns=2;s=MeasurementType"
