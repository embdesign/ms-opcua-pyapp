"""Unit tests for the Value read options (IndexRange, MaxAge, TimestampsToReturn)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from asyncua import ua

from opcua_explorer.errors import OpcUaExplorerError
from opcua_explorer.models import ValueReadOptions, ValueReadResult, ValueSnapshot
from opcua_explorer.opcua.value_read import (
    build_read_parameters,
    read_value_with_options,
    timestamps_to_return,
    validate_index_range,
)


def _data_value(value: float, status: int = ua.StatusCodes.Good) -> ua.DataValue:
    data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Double))
    data_value.StatusCode = ua.StatusCode(status)
    data_value.SourceTimestamp = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    data_value.ServerTimestamp = datetime(2026, 11, 18, 9, 0, 1, tzinfo=UTC)
    return data_value


class _FakeSession:
    def __init__(self, results: list[ua.DataValue] | None = None, error: Exception | None = None) -> None:
        self.results = results if results is not None else [_data_value(1.0)]
        self.error = error
        self.requests: list[ua.ReadParameters] = []

    async def read(self, parameters: ua.ReadParameters) -> list[ua.DataValue]:
        self.requests.append(parameters)
        if self.error is not None:
            raise self.error
        return self.results


@pytest.mark.parametrize("text", ["0", "1:3", "0,2:4", " 5 ", "10,0:1"])
def test_valid_numeric_ranges_are_accepted(text: str) -> None:
    assert validate_index_range(text) == text.strip()


@pytest.mark.parametrize("text", ["abc", "1:", ":3", "-1", "1,", "1:2:3", "1 2"])
def test_invalid_numeric_ranges_are_refused(text: str) -> None:
    with pytest.raises(OpcUaExplorerError):
        validate_index_range(text)


def test_empty_index_range_means_the_whole_value() -> None:
    assert validate_index_range("   ") == ""


def test_options_are_normalised() -> None:
    assert ValueReadOptions(index_range=" 1:3 ", max_age_ms=-5, timestamps="Nonsense").normalised() == ValueReadOptions(
        index_range="1:3", max_age_ms=0, timestamps="Both"
    )
    assert ValueReadOptions(max_age_ms=10**12).normalised().max_age_ms == 4_294_967_295


def test_option_labels_mention_what_was_asked() -> None:
    assert ValueReadOptions().label() == "timestamps Both, MaxAge 0 ms"
    assert ValueReadOptions(index_range="1:3", max_age_ms=250).label() == (
        "IndexRange 1:3, timestamps Both, MaxAge 250 ms"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Source", ua.TimestampsToReturn.Source),
        ("Server", ua.TimestampsToReturn.Server),
        ("Both", ua.TimestampsToReturn.Both),
        ("Neither", ua.TimestampsToReturn.Neither),
        ("unknown", ua.TimestampsToReturn.Both),
    ],
)
def test_timestamp_names_map_onto_enum(name: str, expected: ua.TimestampsToReturn) -> None:
    assert timestamps_to_return(name) is expected


def test_request_carries_the_options() -> None:
    parameters = build_read_parameters(
        "ns=2;s=Samples", ValueReadOptions(index_range="1:3", max_age_ms=500, timestamps="Neither")
    )

    assert parameters.MaxAge == 500
    assert parameters.TimestampsToReturn == ua.TimestampsToReturn.Neither
    read_value = parameters.NodesToRead[0]
    assert read_value.NodeId.to_string() == "ns=2;s=Samples"
    assert read_value.AttributeId == ua.AttributeIds.Value
    assert read_value.IndexRange == "1:3"


def test_an_empty_index_range_is_sent_as_none() -> None:
    parameters = build_read_parameters("i=1", ValueReadOptions())
    assert parameters.NodesToRead[0].IndexRange is None


async def test_a_good_read_is_decoded_and_reports_the_options() -> None:
    session = _FakeSession([_data_value(42.5)])

    result = await read_value_with_options(session, "ns=2;s=Temperature", ValueReadOptions(max_age_ms=1000))

    assert result.snapshot.value == 42.5
    assert result.snapshot.status == "Good"
    assert result.snapshot.source_timestamp == "2026-11-18T09:00:00.000Z"
    assert "MaxAge 1000 ms" in result.detail
    assert "the server decides" in result.detail
    assert len(session.requests) == 1


async def test_a_bad_status_is_reported_with_the_options() -> None:
    session = _FakeSession([_data_value(0.0, ua.StatusCodes.BadNotReadable)])

    result = await read_value_with_options(session, "i=1", ValueReadOptions(index_range="1:3"))

    assert result.snapshot.status == "BadNotReadable"
    assert result.detail == "Read returned BadNotReadable."


async def test_an_invalid_range_never_reaches_the_server() -> None:
    session = _FakeSession()

    with pytest.raises(OpcUaExplorerError):
        await read_value_with_options(session, "i=1", ValueReadOptions(index_range="nope"))
    assert session.requests == []


async def test_a_failed_read_is_wrapped() -> None:
    session = _FakeSession(error=ua.UaError("BadSessionClosed"))

    with pytest.raises(OpcUaExplorerError) as excinfo:
        await read_value_with_options(session, "i=1", ValueReadOptions())
    assert "BadSessionClosed" in str(excinfo.value)


async def test_no_result_is_an_error() -> None:
    session = _FakeSession([])

    with pytest.raises(OpcUaExplorerError):
        await read_value_with_options(session, "i=1", ValueReadOptions())


def test_result_is_serialisable() -> None:
    result = ValueReadResult(
        node_id="i=1",
        options=ValueReadOptions(index_range="1:3"),
        snapshot=ValueSnapshot(value=1.0, variant_type="Double"),
        duration_ms=1.23456,
        detail="IndexRange 1:3 requested",
    )

    document = result.to_dict()

    assert document["options"] == {"index_range": "1:3", "max_age_ms": 0, "timestamps": "Both"}
    assert document["value"]["variant_type"] == "Double"
    assert document["duration_ms"] == 1.235
    assert document["received_at"].endswith("Z")
    assert result.summary_text().startswith("Value of i=1 [Good]")
