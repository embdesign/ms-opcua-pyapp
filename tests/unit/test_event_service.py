"""Unit tests for the event subscription wrapper (no server involved)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from asyncua import ua
from asyncua.common.subscription import Event

from opcua_explorer.errors import MonitorNotActiveError
from opcua_explorer.models import AUTHORITY_OPERATOR_DIALOG, EventConfig, EventNotification, WriteAuthority
from opcua_explorer.opcua.event_service import (
    BASE_EVENT_TYPE_NODE_ID,
    LiveEvents,
    event_field_text,
    event_id_bytes,
    event_type_id,
    event_type_name,
)

REJECTED_SOURCE = "ns=2;s=NoEvents"

_AUTHORITY = WriteAuthority(AUTHORITY_OPERATOR_DIALOG, "events-tab refresh conditions")


class _FakeNodeId:
    def __init__(self, text: str) -> None:
        self._text = text

    def to_string(self) -> str:
        return self._text


class _FakeNode:
    def __init__(self, node_id: str) -> None:
        self.nodeid = _FakeNodeId(node_id)


class _FakeSubscription:
    def __init__(self) -> None:
        self.subscription_id = 42
        self.subscribed: list[tuple[str, Any, int]] = []
        self.event_filters: list[Any] = []
        self.unsubscribed: list[int] = []
        self.deleted = False

    async def subscribe_events(
        self, node: _FakeNode, event_type: Any, evfilter: Any = None, queuesize: int = 0
    ) -> int:
        node_id = node.nodeid.to_string()
        if node_id == REJECTED_SOURCE:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown)
        handle = 200 + len(self.subscribed)
        self.subscribed.append((node_id, event_type, queuesize))
        self.event_filters.append(evfilter)
        return handle

    async def unsubscribe(self, handles: list[int]) -> None:
        self.unsubscribed.extend(handles)

    async def delete(self) -> None:
        self.deleted = True


class _FakeClient:
    def __init__(self) -> None:
        self.params: ua.CreateSubscriptionParameters | None = None
        self.handler: Any = None
        self.subscription = _FakeSubscription()
        self.nodes: list[Any] = []
        #: Returned by ``get_node`` when set, so a ConditionRefresh call lands on it.
        self.method_node: Any | None = None

    def get_node(self, node_id: Any) -> Any:
        self.nodes.append(node_id)
        return self.method_node if self.method_node is not None else _FakeNode(node_id)

    async def create_subscription(
        self, params: ua.CreateSubscriptionParameters, handler: Any
    ) -> _FakeSubscription:
        self.params = params
        self.handler = handler
        return self.subscription


def _event(**properties: Any) -> Event:
    """Build an asyncua Event with the fields a BaseEventType subscription selects."""
    event = Event()
    fields = {
        "EventId": (b"\x01\x02\xfe", ua.VariantType.ByteString),
        "EventType": (ua.NodeId(ua.ObjectIds.BaseEventType), ua.VariantType.NodeId),
        "SourceNode": (ua.NodeId(ua.ObjectIds.Server), ua.VariantType.NodeId),
        "SourceName": ("Server", ua.VariantType.String),
        "Time": (datetime(2026, 11, 16, 12, 0, tzinfo=UTC), ua.VariantType.DateTime),
        "ReceiveTime": (datetime(2026, 11, 16, 12, 0, 1, tzinfo=UTC), ua.VariantType.DateTime),
        "LocalTime": (ua.TimeZoneDataType(Offset=60, DaylightSavingInOffset=True), ua.VariantType.ExtensionObject),
        "Message": (ua.LocalizedText("Pump stopped"), ua.VariantType.LocalizedText),
        "Severity": (500, ua.VariantType.UInt16),
    }
    fields.update(properties)
    for name, (value, variant_type) in fields.items():
        event.add_property(name, value, variant_type)
    return event


def test_event_field_text_formats_opcua_values() -> None:
    assert event_field_text(None) == ""
    assert event_field_text(ua.LocalizedText("Pump stopped")) == "Pump stopped"
    assert event_field_text(ua.NodeId(85)) == "i=85"
    assert event_field_text(ua.QualifiedName("Temperature", 2)) == "2:Temperature"
    assert event_field_text(b"\x01\xff") == "01ff"
    assert event_field_text(ua.TimeZoneDataType(Offset=60, DaylightSavingInOffset=True)) == (
        "offset=60 min, daylight=True"
    )
    assert event_field_text([1, 2, 3]) == "[1,2,3]"
    assert event_field_text(datetime(2026, 11, 16, 12, 0, tzinfo=UTC)) == "2026-11-16T12:00:00.000Z"
    assert event_field_text("x" * 30, limit=10) == "xxxxxxxxx…"


def test_event_type_name_prefers_the_standard_name() -> None:
    assert event_type_name(ua.NodeId(ua.ObjectIds.BaseEventType)) == "BaseEventType"
    assert event_type_id(ua.NodeId(ua.ObjectIds.BaseEventType)) == BASE_EVENT_TYPE_NODE_ID
    assert event_type_name(ua.NodeId("CustomEvent", 2)) == "ns=2;s=CustomEvent"
    assert event_type_name(None) == ""


async def test_start_builds_one_subscription_and_subscribes_each_source() -> None:
    client = _FakeClient()
    emitted: list[Any] = []
    events = LiveEvents(client, emitted.append)

    start = await events.start(["i=2253", "ns=2;s=Plant"], EventConfig(publishing_interval_ms=250))

    assert start.subscription_id == 42
    assert [source.ok for source in start.sources] == [True, True]
    assert client.params is not None
    assert client.params.RequestedPublishingInterval == 250
    assert client.params.PublishingEnabled is True
    assert [entry[0] for entry in client.subscription.subscribed] == ["i=2253", "ns=2;s=Plant"]
    assert all(entry[1] == ua.NodeId(ua.ObjectIds.BaseEventType) for entry in client.subscription.subscribed)
    assert events.is_active is True
    assert events.source_count() == 2

    await events.stop()
    assert client.subscription.deleted is True
    assert events.is_active is False


async def test_a_rejected_source_is_reported_without_aborting_the_others() -> None:
    client = _FakeClient()
    events = LiveEvents(client, lambda _event: None)

    start = await events.start([REJECTED_SOURCE, "i=2253"], EventConfig())

    rejected, accepted = start.sources
    assert rejected.ok is False
    assert rejected.status == "BadNodeIdUnknown"
    assert rejected.detail
    assert accepted.ok is True
    assert accepted.client_handle == 200
    assert events.source_count() == 1


async def test_add_and_remove_require_a_running_subscription() -> None:
    events = LiveEvents(_FakeClient(), lambda _event: None)
    with pytest.raises(MonitorNotActiveError):
        await events.add(["i=2253"])
    with pytest.raises(MonitorNotActiveError):
        await events.remove([1])

    client = _FakeClient()
    events = LiveEvents(client, lambda _event: None)
    await events.start(["i=2253"], EventConfig())
    added = await events.add(["ns=2;s=Plant"])
    assert added[0].ok is True
    await events.remove([added[0].client_handle])
    assert client.subscription.unsubscribed == [added[0].client_handle]
    assert events.source_count() == 1


async def test_configured_filter_is_forwarded_to_each_source() -> None:
    client = _FakeClient()
    events = LiveEvents(client, lambda _event: None)
    await events.start(["i=2253", "ns=2;s=Plant"], EventConfig(min_severity=10))
    assert all(event_filter is not None for event_filter in client.subscription.event_filters)


async def test_custom_event_type_is_forwarded_to_the_server() -> None:
    client = _FakeClient()
    events = LiveEvents(client, lambda _event: None)
    await events.start(["i=2253"], EventConfig(event_type_node_id="ns=2;s=CustomEvent"))
    assert client.subscription.subscribed[0][1] == ua.NodeId.from_string("ns=2;s=CustomEvent")


async def test_notification_reduces_every_selected_field() -> None:
    emitted: list[Any] = []
    events = LiveEvents(_FakeClient(), emitted.append)
    await events.start(["i=2253"], EventConfig())

    events._handle_event(_event())

    assert len(emitted) == 1
    notification = emitted[0]
    assert isinstance(notification, EventNotification)
    assert notification.sequence == 1
    assert notification.event_type == "BaseEventType"
    assert notification.event_type_id == "i=2041"
    assert notification.source_node == "i=2253"
    assert notification.source_name == "Server"
    assert notification.severity == 500
    assert notification.message == "Pump stopped"
    assert notification.event_id == "0102fe"
    assert notification.time == "2026-11-16T12:00:00.000Z"
    assert notification.receive_time == "2026-11-16T12:00:01.000Z"
    assert notification.fields["LocalTime"] == "offset=60 min, daylight=True"

    payload = notification.to_dict()
    assert payload["message"] == "Pump stopped"
    assert payload["fields"]["Severity"] == "500"
    assert "received_at_utc" in payload


async def test_a_broken_event_does_not_stop_the_subscription() -> None:
    emitted: list[Any] = []
    events = LiveEvents(_FakeClient(), emitted.append)
    await events.start(["i=2253"], EventConfig())

    class _ExplodingEvent:
        data_types = {"Boom": ua.VariantType.String}

        @property
        def Boom(self) -> Any:
            raise RuntimeError("field cannot be read")

    events._handle_event(_ExplodingEvent())
    assert emitted == []

    events._handle_event(_event())
    assert len(emitted) == 1


def test_event_id_bytes_decodes_only_usable_hex() -> None:
    assert event_id_bytes("0102fe") == b"\x01\x02\xfe"
    assert event_id_bytes(" 0102FE ") == b"\x01\x02\xfe"
    assert event_id_bytes("") is None
    assert event_id_bytes("abc") is None  # odd length
    assert event_id_bytes("zz") is None  # not hex


def _acked_state_clause() -> ua.SimpleAttributeOperand:
    clause = ua.SimpleAttributeOperand()
    clause.TypeDefinitionId = ua.NodeId(ua.ObjectIds.ConditionType)
    clause.BrowsePath = [ua.QualifiedName("AckedState")]
    clause.AttributeId = ua.AttributeIds.Value
    return clause


async def test_a_condition_subscription_reports_the_condition_node() -> None:
    emitted: list[Any] = []
    events = LiveEvents(_FakeClient(), emitted.append)
    await events.start(["i=2253"], EventConfig())

    event = _event()
    event.select_clauses = [_acked_state_clause()]
    event.add_property("NodeId", ua.NodeId("Condition", 2), ua.VariantType.NodeId)
    events._handle_event(event)

    assert emitted[0].condition_node_id == "ns=2;s=Condition"


async def test_a_plain_event_has_no_condition_identity() -> None:
    emitted: list[Any] = []
    events = LiveEvents(_FakeClient(), emitted.append)
    await events.start(["i=2253"], EventConfig())

    events._handle_event(_event())
    assert emitted[0].condition_node_id == ""


async def test_a_condition_event_without_identity_falls_back_to_the_source() -> None:
    emitted: list[Any] = []
    events = LiveEvents(_FakeClient(), emitted.append)
    await events.start(["i=2253"], EventConfig())

    event = _event()
    event.select_clauses = [_acked_state_clause()]
    events._handle_event(event)

    # No ConditionId/NodeId field: the emitting node is the condition object.
    assert emitted[0].condition_node_id == "i=2253"


class _ConditionRefreshNode(_FakeNode):
    """The node the refresh method call lands on; records the method and arguments."""

    def __init__(self, node_id: str, *, error: Exception | None = None) -> None:
        super().__init__(node_id)
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []
        self._error = error

    async def call_method(self, method: Any, *args: Any) -> Any:
        self.calls.append((method, args))
        if self._error is not None:
            raise self._error
        return None


def _refresh_events(count: int = 1) -> list[Event]:
    """The RefreshStart/RefreshEnd pair a server publishes around a refresh."""
    events = []
    for _ in range(count):
        events.append(
            _event(EventType=(ua.NodeId(ua.ObjectIds.RefreshStartEventType), ua.VariantType.NodeId))
        )
        events.append(
            _event(EventType=(ua.NodeId(ua.ObjectIds.RefreshEndEventType), ua.VariantType.NodeId))
        )
    return events


async def test_refresh_conditions_calls_once_and_reports_the_refresh_events() -> None:
    client = _FakeClient()
    node = _ConditionRefreshNode("i=2782")
    client.method_node = node
    events = LiveEvents(client, lambda _event: None)
    await events.start(["i=2253"], EventConfig(publishing_interval_ms=200))

    async def publish() -> None:
        await asyncio.sleep(0.02)
        for event in _refresh_events():
            events._handle_event(event)

    feeder = asyncio.create_task(publish())
    result = await events.refresh_conditions(authorization=_AUTHORITY, observation_timeout_s=1.0)
    await feeder

    # Exactly one method call, with this subscription's id.
    assert len(node.calls) == 1
    method, arguments = node.calls[0]
    assert method == ua.NodeId(ua.ObjectIds.ConditionType_ConditionRefresh)
    assert arguments[0].Value == 42
    assert result.status == "passed"
    assert result.sent is True
    assert result.subscription_id == 42
    assert result.object_node_id == "i=2782"
    assert result.refresh_started is True
    assert result.refresh_ended is True
    await events.stop()


async def test_refresh_conditions_without_a_subscription_is_refused() -> None:
    events = LiveEvents(_FakeClient(), lambda _event: None)

    with pytest.raises(MonitorNotActiveError):
        await events.refresh_conditions(authorization=_AUTHORITY)


async def test_refresh_conditions_reports_a_missing_refresh_event() -> None:
    client = _FakeClient()
    node = _ConditionRefreshNode("i=2782")
    client.method_node = node
    events = LiveEvents(client, lambda _event: None)
    await events.start(["i=2253"], EventConfig())

    result = await events.refresh_conditions(authorization=_AUTHORITY, observation_timeout_s=0.1)

    assert result.status == "passed"
    assert result.refresh_started is False
    assert result.refresh_ended is False
    assert "no RefreshStart event" in result.detail
    await events.stop()


async def test_refresh_conditions_reports_a_refused_call() -> None:
    client = _FakeClient()
    node = _ConditionRefreshNode("i=2782", error=ua.UaStatusCodeError(ua.StatusCodes.BadSubscriptionIdInvalid))
    client.method_node = node
    events = LiveEvents(client, lambda _event: None)
    await events.start(["i=2253"], EventConfig())

    result = await events.refresh_conditions(authorization=_AUTHORITY, observation_timeout_s=0.1)

    assert result.status == "failed"
    assert result.status_code == "BadSubscriptionIdInvalid"
    assert result.sent is True
    assert result.refresh_started is False
    await events.stop()
