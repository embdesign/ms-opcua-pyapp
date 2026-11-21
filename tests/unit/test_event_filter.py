"""Server-side event filter construction (select clauses and severity WhereClause)."""

from __future__ import annotations

from asyncua import ua

from opcua_explorer.models import EventConfig
from opcua_explorer.opcua.event_service import DEFAULT_EVENT_FIELDS, build_event_filter

_EVENT_TYPE = ua.NodeId(ua.ObjectIds.BaseEventType)


def _selected(event_filter: ua.EventFilter) -> list[tuple[str, int]]:
    return [(clause.BrowsePath[0].Name, clause.BrowsePath[0].NamespaceIndex) for clause in event_filter.SelectClauses]


def test_default_event_config_keeps_the_asyncua_default_filter() -> None:
    assert build_event_filter(_EVENT_TYPE, EventConfig()) is None


def test_extra_fields_are_added_to_the_standard_select_clauses() -> None:
    event_filter = build_event_filter(
        _EVENT_TYPE,
        EventConfig(select_fields=["ConditionClassId", "2:Temperature", "2:Temperature"]),
    )
    assert event_filter is not None
    selected = _selected(event_filter)
    for name in DEFAULT_EVENT_FIELDS:
        assert (name, 0) in selected
    assert ("ConditionClassId", 0) in selected
    assert selected.count(("Temperature", 2)) == 1


def test_min_severity_becomes_a_server_side_where_clause() -> None:
    event_filter = build_event_filter(_EVENT_TYPE, EventConfig(min_severity=500))
    assert event_filter is not None
    elements = event_filter.WhereClause.Elements
    assert len(elements) == 1
    assert elements[0].FilterOperator == ua.FilterOperator.GreaterThanOrEqual
    severity, literal = elements[0].FilterOperands
    assert severity.BrowsePath[0].Name == "Severity"
    assert literal.Value.Value == 500
    assert literal.Value.VariantType == ua.VariantType.UInt16


def test_event_config_normalises_fields_and_clamps_severity() -> None:
    config = EventConfig(select_fields=["  Message  ", "", "Message"], min_severity=99_999).normalised()
    assert config.select_fields == ["Message"]
    assert config.min_severity == 65_535
