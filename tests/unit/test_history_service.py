"""Unit tests for raw-history request building and continuation handling."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from asyncua import ua

from opcua_explorer.errors import OpcUaExplorerError
from opcua_explorer.models import HistoryConfig
from opcua_explorer.opcua.history_service import build_history_request, read_history


def _data_value(moment: datetime, value: float) -> ua.DataValue:
    data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Double))
    data_value.SourceTimestamp = moment
    data_value.ServerTimestamp = moment
    return data_value


def _result(values: list[ua.DataValue], continuation: bytes = b"") -> ua.HistoryReadResult:
    result = ua.HistoryReadResult()
    result.StatusCode = ua.StatusCode(ua.StatusCodes.Good)
    result.ContinuationPoint = continuation
    data = ua.HistoryData()
    data.DataValues = values
    result.HistoryData = data
    return result


def _bad_result() -> ua.HistoryReadResult:
    result = ua.HistoryReadResult()
    result.StatusCode = ua.StatusCode(ua.StatusCodes.BadHistoryOperationUnsupported)
    result.ContinuationPoint = b""
    return result


class _HistorySession:
    def __init__(self, pages: list[ua.HistoryReadResult], *, historizing: bool | None = True) -> None:
        self.pages = pages
        self.historizing = historizing
        self.requests: list[ua.HistoryReadParameters] = []
        self.releases: list[ua.HistoryReadParameters] = []

    async def read(self, params: ua.ReadParameters) -> list[ua.DataValue]:
        results: list[ua.DataValue] = []
        for read_value in params.NodesToRead:
            if int(read_value.AttributeId) == int(ua.AttributeIds.Historizing):
                results.append(ua.DataValue(ua.Variant(self.historizing, ua.VariantType.Boolean)))
            else:
                results.append(ua.DataValue(ua.Variant(None, ua.VariantType.Null)))
        return results

    async def history_read(self, params: ua.HistoryReadParameters) -> list[ua.HistoryReadResult]:
        self.requests.append(params)
        if params.ReleaseContinuationPoints:
            self.releases.append(params)
            return [_result([])]
        return [self.pages.pop(0)]


def test_history_config_is_normalised() -> None:
    assert HistoryConfig(max_values=0).normalised().max_values == 1
    assert HistoryConfig(max_values=99_999).normalised().max_values == 10_000
    assert HistoryConfig(max_values=250).normalised().max_values == 250


def test_aggregate_request_uses_read_processed_details() -> None:
    start = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
    end = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    params = build_history_request(
        "ns=2;s=Temperature",
        HistoryConfig(start=start, end=end, aggregate="average", processing_interval_ms=60_000),
    )

    details = params.HistoryReadDetails
    assert isinstance(details, ua.ReadProcessedDetails)
    assert details.StartTime == start
    assert details.EndTime == end
    assert details.ProcessingInterval == 60_000.0
    assert details.AggregateType == [ua.NodeId(ua.ObjectIds.AggregateFunction_Average)]
    assert details.AggregateConfiguration.UseServerCapabilitiesDefaults is True


def test_aggregate_requires_a_start_and_end() -> None:
    with pytest.raises(OpcUaExplorerError, match="start and an end"):
        build_history_request("ns=2;s=Temperature", HistoryConfig(aggregate="maximum"))


def test_unknown_aggregate_falls_back_to_raw() -> None:
    assert HistoryConfig(aggregate="median").normalised().aggregate is None


async def test_unsupported_aggregate_is_reported_with_a_hint() -> None:
    session = _HistorySession([_bad_result()])
    result = await read_history(
        session,
        "ns=2;s=Temperature",
        HistoryConfig(
            start=datetime(2026, 11, 18, 8, 0, tzinfo=UTC),
            end=datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
            aggregate="average",
        ),
    )

    assert result.status == "BadHistoryOperationUnsupported"
    assert "does not provide aggregate history" in result.detail


def test_request_carries_range_cap_and_continuation() -> None:
    start = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
    end = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    params = build_history_request(
        "ns=2;s=Temperature",
        HistoryConfig(start=start, end=end, max_values=25),
        continuation_point=b"\x01\x02",
        release=True,
    )

    assert params.ReleaseContinuationPoints is True
    assert params.TimestampsToReturn == ua.TimestampsToReturn.Both
    details = params.HistoryReadDetails
    assert isinstance(details, ua.ReadRawModifiedDetails)
    assert details.IsReadModified is False
    assert details.StartTime == start
    assert details.EndTime == end
    assert details.NumValuesPerNode == 25
    assert details.ReturnBounds is False
    read_value = params.NodesToRead[0]
    assert read_value.NodeId.to_string() == "ns=2;s=Temperature"
    assert read_value.ContinuationPoint == b"\x01\x02"


def test_open_range_uses_the_windows_epoch() -> None:
    params = build_history_request("i=2258", HistoryConfig())
    details = params.HistoryReadDetails
    assert details.StartTime == ua.get_win_epoch()
    assert details.EndTime == ua.get_win_epoch()


async def test_values_are_sorted_chronologically_across_pages() -> None:
    newer = datetime(2026, 11, 18, 9, 0, 5, tzinfo=UTC)
    middle = datetime(2026, 11, 18, 9, 0, 3, tzinfo=UTC)
    older = datetime(2026, 11, 18, 9, 0, 1, tzinfo=UTC)
    session = _HistorySession(
        [
            _result([_data_value(newer, 3.0), _data_value(middle, 2.0)], continuation=b"cp"),
            _result([_data_value(older, 1.0)]),
        ]
    )

    result = await read_history(session, "ns=2;s=Temperature", HistoryConfig(max_values=10))

    assert result.status == "Good"
    assert result.historizing is True
    assert result.pages == 2
    assert result.truncated is False
    assert [value.value for value in result.values] == [1.0, 2.0, 3.0]
    assert [value.source_timestamp for value in result.values] == [
        "2026-11-18T09:00:01.000Z",
        "2026-11-18T09:00:03.000Z",
        "2026-11-18T09:00:05.000Z",
    ]


async def test_hitting_the_cap_releases_the_continuation_point() -> None:
    moment = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    session = _HistorySession(
        [_result([_data_value(moment, 1.0), _data_value(moment, 2.0)], continuation=b"cp")]
    )

    result = await read_history(session, "i=1", HistoryConfig(max_values=2))

    assert result.truncated is True
    assert len(result.values) == 2
    assert len(session.releases) == 1
    assert session.releases[0].ReleaseContinuationPoints is True
    assert session.releases[0].NodesToRead[0].ContinuationPoint == b"cp"


async def test_an_over_delivered_page_keeps_the_newest_values() -> None:
    moments = [datetime(2026, 11, 18, 9, 0, minute, tzinfo=UTC) for minute in range(4)]
    session = _HistorySession([_result([_data_value(moment, float(index)) for index, moment in enumerate(moments)])])

    result = await read_history(session, "i=1", HistoryConfig(max_values=2))

    assert result.truncated is True
    assert [value.value for value in result.values] == [2.0, 3.0]
    assert "requested maximum" in result.detail


async def test_a_bad_history_status_is_reported_not_raised() -> None:
    session = _HistorySession([_bad_result()])

    result = await read_history(session, "i=1", HistoryConfig())

    assert result.status == "BadHistoryOperationUnsupported"
    assert result.values == []
    assert "BadHistoryOperationUnsupported" in result.detail


async def test_an_empty_historized_range_is_not_an_error() -> None:
    session = _HistorySession([_result([])], historizing=False)

    result = await read_history(session, "i=1", HistoryConfig())

    assert result.status == "Good"
    assert result.values == []
    assert result.historizing is False
    assert result.detail == "The node reports Historizing = false; the server holds no history for it."


async def test_start_after_end_is_refused_before_any_request() -> None:
    session = _HistorySession([])
    start = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    end = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)

    with pytest.raises(OpcUaExplorerError):
        await read_history(session, "i=1", HistoryConfig(start=start, end=end))
    assert session.requests == []
