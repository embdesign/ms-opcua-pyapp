"""Unit tests for the stored-event HistoryRead (request shape and decoding)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from asyncua import ua

from opcua_explorer.errors import OpcUaExplorerError
from opcua_explorer.models import EventHistoryConfig
from opcua_explorer.opcua.event_service import (
    build_event_history_request,
    read_event_history,
)

START = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
END = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)


def _field_list(select_clauses, values: dict[str, object]) -> ua.HistoryEventFieldList:
    fields = []
    for clause in select_clauses:
        name = clause.BrowsePath[0].Name
        value = values.get(name)
        if name in ("EventType", "SourceNode"):
            fields.append(ua.Variant(value, ua.VariantType.NodeId))
        elif name == "EventId":
            fields.append(ua.Variant(value, ua.VariantType.ByteString))
        elif name == "Severity":
            fields.append(ua.Variant(value, ua.VariantType.UInt16))
        elif name in ("Time", "ReceiveTime"):
            fields.append(ua.Variant(value, ua.VariantType.DateTime))
        elif name == "Message":
            fields.append(ua.Variant(ua.LocalizedText(str(value or "")), ua.VariantType.LocalizedText))
        else:
            fields.append(ua.Variant(value, ua.VariantType.String))
    entry = ua.HistoryEventFieldList()
    entry.EventFields = fields
    return entry


def _page(field_lists, continuation: bytes = b"") -> ua.HistoryReadResult:
    result = ua.HistoryReadResult()
    result.StatusCode = ua.StatusCode(ua.StatusCodes.Good)
    result.ContinuationPoint = continuation
    data = ua.HistoryEvent()
    data.Events = list(field_lists)
    result.HistoryData = data
    return result


class _Session:
    def __init__(self, pages: list[ua.HistoryReadResult]) -> None:
        self.pages = pages
        self.requests: list[ua.HistoryReadParameters] = []

    async def history_read(self, params: ua.HistoryReadParameters):
        self.requests.append(params)
        if params.ReleaseContinuationPoints:
            return [_page([])]
        return [self.pages.pop(0)]


def test_history_request_always_carries_a_filter_and_the_range() -> None:
    config = EventHistoryConfig(start=START, end=END, max_values=25)
    params = build_event_history_request("i=2253", config)

    details = params.HistoryReadDetails
    assert isinstance(details, ua.ReadEventDetails)
    assert details.StartTime == START
    assert details.EndTime == END
    assert details.NumValuesPerNode == 25
    names = [clause.BrowsePath[0].Name for clause in details.Filter.SelectClauses]
    assert {"EventId", "EventType", "SourceNode", "Message", "Severity"} <= set(names)


def test_extra_fields_and_severity_reach_the_history_filter() -> None:
    config = EventHistoryConfig(
        event_type_node_id="i=2782",
        select_fields=["ConditionId", "AckedState", "ConditionId"],
        min_severity=500,
    )

    params = build_event_history_request("i=2253", config)

    details = params.HistoryReadDetails
    names = [clause.BrowsePath[0].Name for clause in details.Filter.SelectClauses]
    assert names.count("ConditionId") == 1
    assert "AckedState" in names
    assert details.Filter.WhereClause.Elements[0].FilterOperator == ua.FilterOperator.GreaterThanOrEqual


def test_open_range_uses_the_windows_epoch_and_empty_type_means_base_event() -> None:
    params = build_event_history_request("i=2253", EventHistoryConfig())

    details = params.HistoryReadDetails
    assert details.StartTime == ua.get_win_epoch()
    assert details.EndTime == ua.get_win_epoch()
    assert details.Filter.SelectClauses[0].TypeDefinitionId == ua.NodeId(ua.ObjectIds.BaseEventType)


async def test_condition_id_selects_the_condition_identity() -> None:
    config = EventHistoryConfig(event_type_node_id="i=2782", select_fields=["ConditionId"])
    params = build_event_history_request("i=2253", config)
    clauses = params.HistoryReadDetails.Filter.SelectClauses
    session = _Session(
        [
            _page(
                [
                    _field_list(
                        clauses,
                        {
                            "EventId": b"\x01\x02",
                            "EventType": ua.NodeId(ua.ObjectIds.ConditionType),
                            "SourceNode": ua.NodeId(2253),
                            "ConditionId": "ns=2;s=PumpCondition",
                            "Severity": 500,
                            "Message": "Pump stopped",
                            "Time": START,
                            "ReceiveTime": START,
                        },
                    )
                ]
            )
        ]
    )

    result = await read_event_history(session, "i=2253", config)

    assert result.status == "Good"
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == "ConditionType"
    assert event.condition_node_id == "ns=2;s=PumpCondition"
    assert event.message == "Pump stopped"
    assert event.severity == 500
    assert event.event_id == "0102"


async def test_pages_are_followed_and_sorted_oldest_first() -> None:
    config = EventHistoryConfig(max_values=10)
    params = build_event_history_request("i=2253", config)
    clauses = params.HistoryReadDetails.Filter.SelectClauses
    newer = _field_list(
        clauses,
        {
            "EventId": b"\x02",
            "EventType": ua.NodeId(ua.ObjectIds.BaseEventType),
            "SourceNode": ua.NodeId(2253),
            "Severity": 1,
            "Message": "newer",
            "Time": END,
            "ReceiveTime": END,
        },
    )
    older = _field_list(
        clauses,
        {
            "EventId": b"\x01",
            "EventType": ua.NodeId(ua.ObjectIds.BaseEventType),
            "SourceNode": ua.NodeId(2253),
            "Severity": 1,
            "Message": "older",
            "Time": START,
            "ReceiveTime": START,
        },
    )
    session = _Session([_page([newer], continuation=b"cp"), _page([older])])

    result = await read_event_history(session, "i=2253", config)

    assert result.pages == 2
    assert [event.message for event in result.events] == ["older", "newer"]


async def test_a_reversed_range_is_refused() -> None:
    with pytest.raises(OpcUaExplorerError, match="start time must not be after"):
        await read_event_history(_Session([]), "i=2253", EventHistoryConfig(start=END, end=START))
