"""Value-serialization contract: consistent encodings and explicit flags."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime

import pytest
from asyncua import ua

from opcua_explorer.opcua.serialize import (
    MAX_DEPTH,
    SerializationContext,
    flatten_for_csv,
    serialize_value,
    serialize_value_with_context,
)


def test_scalars_pass_through() -> None:
    assert serialize_value(None) is None
    assert serialize_value(True) is True
    assert serialize_value(42) == 42
    assert serialize_value("text") == "text"
    assert serialize_value(1.5) == 1.5


def test_non_finite_floats_are_tagged() -> None:
    assert serialize_value(math.nan) == {"$type": "Float", "value": "NaN"}
    assert serialize_value(math.inf) == {"$type": "Float", "value": "Infinity"}
    assert serialize_value(-math.inf) == {"$type": "Float", "value": "-Infinity"}


def test_bytestring_becomes_base64() -> None:
    encoded = serialize_value(b"\x00\x01\x02\xff")
    assert encoded["$type"] == "ByteString"
    assert encoded["base64"] == "AAEC/w=="


def test_datetime_is_iso_utc() -> None:
    encoded = serialize_value(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    assert encoded == {"$type": "DateTime", "iso": "2026-01-02T03:04:05.000Z"}


def test_naive_datetime_is_treated_as_utc() -> None:
    encoded = serialize_value(datetime(2026, 1, 2, 3, 4, 5))
    assert encoded["iso"].endswith("Z")


def test_guid() -> None:
    value = uuid.UUID("0f8fad5b-d9cb-469f-a165-70867728950e")
    assert serialize_value(value) == {"$type": "Guid", "value": str(value)}


def test_nodeid_and_expanded_nodeid_are_distinguished() -> None:
    assert serialize_value(ua.NodeId(5, 2)) == {"$type": "NodeId", "value": "ns=2;i=5"}
    # ExpandedNodeId subclasses NodeId, so ordering in the encoder matters.
    assert serialize_value(ua.ExpandedNodeId(5, 2)) == {"$type": "ExpandedNodeId", "value": "ns=2;i=5"}


def test_qualified_name_localized_text_and_status_code() -> None:
    assert serialize_value(ua.QualifiedName("Temp", 2)) == {
        "$type": "QualifiedName",
        "namespace_index": 2,
        "name": "Temp",
    }
    assert serialize_value(ua.LocalizedText("Hello", "en")) == {
        "$type": "LocalizedText",
        "locale": "en",
        "text": "Hello",
    }
    status = serialize_value(ua.StatusCode(ua.StatusCodes.BadTimeout))
    assert status["$type"] == "StatusCode"
    assert status["name"] == "BadTimeout"
    assert status["code"] == int(ua.StatusCodes.BadTimeout)


def test_variant_keeps_type_and_array_shape() -> None:
    encoded = serialize_value(ua.Variant([1, 2, 3], ua.VariantType.Int32))
    assert encoded["$type"] == "Variant"
    assert encoded["variant_type"] == "Int32"
    assert encoded["is_array"] is True
    assert encoded["value"] == [1, 2, 3]


def test_data_value_keeps_status_and_timestamps() -> None:
    data_value = ua.DataValue()
    data_value.Value = ua.Variant(7, ua.VariantType.Int16)
    data_value.SourceTimestamp = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)
    encoded = serialize_value(data_value)
    assert encoded["$type"] == "DataValue"
    assert encoded["status"] == "Good"
    assert encoded["value"]["value"] == 7
    assert encoded["source_timestamp"].startswith("2026-05-06T07:08:09")


def test_decoded_extension_object_is_structurally_encoded() -> None:
    obj = ua.ExtensionObject(
        TypeId=ua.NodeId(884),  # Range
        Body=ua.Range(Low=0.0, High=100.0),
    )
    encoded = serialize_value(obj)
    assert encoded["$type"] == "ExtensionObject"
    assert encoded["body_encoding"] == "decoded"
    assert encoded["encoding"] == 0
    assert encoded["body"]["Low"] == 0.0
    assert encoded["body"]["High"] == 100.0


def test_binary_extension_object_body_is_flagged_not_silently_dropped() -> None:
    obj = ua.ExtensionObject(
        TypeId=ua.NodeId(1234),
        Body=b"\x01\x02",
    )
    encoded, unsupported = serialize_value_with_context(obj)
    assert encoded["encoding"] == 1  # binary ByteString body
    assert encoded["body_encoding"] == "binary-base64"
    assert encoded["body"] == "AQI="
    assert any(item["kind"] == "UndecodedExtensionObject" for item in unsupported)


def test_arrays_and_nesting_preserve_order() -> None:
    assert serialize_value([1, [2.5, "x"], None]) == [1, [2.5, "x"], None]


def test_containers_with_cycles_are_detected() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    encoded, unsupported = serialize_value_with_context(cyclic)
    assert encoded == [{"$type": "Cycle"}]
    assert any(item["kind"] == "Cycle" for item in unsupported)


def test_depth_limit_is_enforced() -> None:
    value: object = 1
    for _ in range(MAX_DEPTH + 5):
        value = [value]
    encoded, unsupported = serialize_value_with_context(value)
    text = json.dumps(encoded)
    assert "MaxDepthExceeded" in text
    assert any(item["kind"] == "MaxDepthExceeded" for item in unsupported)


def test_unsupported_python_object_is_flagged() -> None:
    marker = object()
    encoded, unsupported = serialize_value_with_context(marker)
    assert encoded["$type"] == "Unsupported"
    assert encoded["python_type"] == "builtins.object"
    assert unsupported and unsupported[0]["kind"] == "UnsupportedPythonType"


def test_context_collects_multiple_flags() -> None:
    context = SerializationContext()
    serialize_value([object(), object()], context)
    # Entries are de-duplicated by (kind, detail); the first path is kept.
    assert len(context.unsupported) == 1
    assert context.unsupported[0]["path"] == "[0]"
    assert context.has_unsupported


def test_flatten_for_csv_is_single_line_and_compact() -> None:
    assert flatten_for_csv(None) == ""
    assert flatten_for_csv("plain") == "plain"
    assert flatten_for_csv({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


@pytest.mark.parametrize("value", [b"", b"\xff\xfe"])
def test_empty_and_binary_bytestrings(value: bytes) -> None:
    encoded = serialize_value(value)
    assert encoded["$type"] == "ByteString"
