"""Unit tests for the event subscription wrapper (no server involved)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from asyncua import ua
from asyncua.common.subscription import Event

from opcua_explorer.errors import MonitorNotActiveError
from opcua_explorer.models import EventConfig, EventNotification
from opcua_explorer.opcua.event_service import (
    BASE_EVENT_TYPE_NODE_ID,
    LiveEvents,
    event_field_text,
    event_id_bytes,
    event_type_id,
    event_type_name,
)

REJECTED_SOURCE = "ns=2;s=NoEvents"


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
        self.unsubscribed: list[int] = []
        self.deleted = False

    async def subscribe_events(self, node: _FakeNode, event_type: Any, queuesize: int = 0) -> int:
        node_id = node.nodeid.to_string()
        if node_id == REJECTED_SOURCE:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown)
        handle = 200 + len(self.subscribed)
        self.subscribed.append((node_id, event_type, queuesize))
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
        self.nodes: list[str] = []

    def get_node(self, node_id: str) -> _FakeNode:
        self.nodes.append(node_id)
        return _FakeNode(node_id)

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
