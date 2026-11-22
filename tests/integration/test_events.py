"""Event-subscription integration tests against the real demo server."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from asyncua import ua

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.errors import MonitorNotActiveError, OpcUaExplorerError
from opcua_explorer.models import (
    AUTHORITY_OPERATOR_DIALOG,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    EventConfig,
    EventNotification,
)
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.opcua.event_service import SERVER_OBJECT_NODE_ID


@pytest_asyncio.fixture
async def events_service(tmp_path):
    """Demo server that publishes one event every 300 ms on the Server object."""
    model = await start_demo_server(update_values=False, event_interval=0.3)
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
    try:
        yield service, model
    finally:
        await service.shutdown()
        await stop_demo_server(model)


async def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_event_subscription_delivers_events(events_service) -> None:
    service, _model = events_service
    received: list[EventNotification] = []

    start = await service.start_event_monitor(
        [SERVER_OBJECT_NODE_ID],
        EventConfig(publishing_interval_ms=200),
        emit=received.append,
    )
    assert start.subscription_id is not None
    assert start.sources[0].ok is True, start.sources[0].status
    assert service.is_monitoring_events is True

    assert await _wait_for(lambda: len(received) >= 1), "no event notification arrived"
    event = received[0]
    assert event.sequence >= 1
    assert event.event_type == "BaseEventType"
    assert event.event_type_id == "i=2041"
    assert event.source_node == SERVER_OBJECT_NODE_ID
    assert event.source_name == "Server"
    assert "Demo event" in event.message
    assert event.severity == 1
    assert event.event_id
    assert event.time and event.time.endswith("Z")
    # Every field the subscription selected is kept, not only the summary columns.
    assert {"EventId", "EventType", "SourceNode", "Message", "Severity", "Time"} <= set(event.fields)

    await service.event_monitor_stop()
    assert service.is_monitoring_events is False
    # The session stays usable after the subscription is deleted.
    assert await service.browse_children("i=85")


async def test_min_severity_filter_is_applied_by_the_server(events_service) -> None:
    service, _model = events_service
    received: list[EventNotification] = []
    await service.start_event_monitor(
        [SERVER_OBJECT_NODE_ID],
        EventConfig(publishing_interval_ms=200, min_severity=10),
        emit=received.append,
    )
    try:
        # The demo event has Severity 1, so a >=10 WhereClause must suppress it.
        assert not await _wait_for(lambda: bool(received), timeout=2.0), "a severity-1 event passed a >=10 filter"
    finally:
        await service.event_monitor_stop()


async def test_extra_fields_are_requested_with_the_standard_ones(events_service) -> None:
    service, _model = events_service
    received: list[EventNotification] = []
    await service.start_event_monitor(
        [SERVER_OBJECT_NODE_ID],
        EventConfig(publishing_interval_ms=200, select_fields=["ConditionClassId"]),
        emit=received.append,
    )
    try:
        assert await _wait_for(lambda: bool(received)), "no event notification arrived"
    finally:
        await service.event_monitor_stop()

    fields = received[0].fields
    assert {"EventId", "EventType", "SourceNode", "Message", "Severity"} <= set(fields)
    assert "ConditionClassId" in fields


async def test_a_source_that_cannot_publish_events_is_reported(events_service) -> None:
    service, model = events_service

    start = await service.start_event_monitor(
        [model.node_ids["plant"], "ns=2;s=DoesNotExist"],
        EventConfig(),
        emit=lambda _event: None,
    )

    # Reported per source with a StatusCode, never raised; the subscription exists.
    assert [source.ok for source in start.sources] == [False, False]
    assert all(source.status != "Good" for source in start.sources)
    assert all(source.detail for source in start.sources)
    assert service.is_monitoring_events is True
    assert await service.browse_children("i=85")

    await service.event_monitor_stop()
    assert service.is_monitoring_events is False


async def _add_condition(
    model, calls: list[tuple[object, object]], method_name: str = "Acknowledge"
) -> object:
    """Create a condition-like object with one real method of a standard browse name."""
    objects = model.server.get_objects_node()
    condition = await objects.add_object(
        ua.NodeId(f"Condition{method_name}", 2), ua.QualifiedName(f"Condition{method_name}", 2)
    )

    async def method(parent, event_id, comment):
        calls.append((event_id, comment))
        return []

    await condition.add_method(
        ua.NodeId(f"Condition{method_name}Method", 2),
        ua.QualifiedName(method_name),  # standard methods live in namespace 0
        method,
        [ua.VariantType.ByteString, ua.VariantType.LocalizedText],
        [],
    )
    return condition


async def test_acknowledge_calls_the_condition_method_once(events_service) -> None:
    service, model = events_service
    calls: list[tuple[object, object]] = []
    condition = await _add_condition(model, calls)

    outcome = await service.condition_action(
        condition.nodeid.to_string(),
        "0102ff",
        "checked by operator",
        action="acknowledge",
        reference="test-ack",
    )

    assert outcome.status == STATUS_PASSED
    assert outcome.sent is True
    assert outcome.authorization is not None
    assert outcome.authorization.source == AUTHORITY_OPERATOR_DIALOG
    assert outcome.authorization.reference == "test-ack"
    # Exactly one call, with the EventId and Comment as typed arguments.
    assert len(calls) == 1
    event_id, comment = calls[0]
    assert event_id.Value == b"\x01\x02\xff"
    assert comment.Value.Text == "checked by operator"


@pytest.mark.parametrize(
    "action, method_name",
    [
        ("acknowledge", "Acknowledge"),
        ("confirm", "Confirm"),
        ("enable", "Enable"),
        ("disable", "Disable"),
        ("comment", "AddComment"),
    ],
)
async def test_each_condition_action_calls_its_own_method_once(
    events_service, action: str, method_name: str
) -> None:
    """One action -> one standard method, resolved below the condition, never retried."""
    service, model = events_service
    calls: list[tuple[object, object]] = []
    condition = await _add_condition(model, calls, method_name)

    outcome = await service.condition_action(
        condition.nodeid.to_string(), "01", "operator text", action=action, reference=f"test-{action}"
    )

    assert outcome.status == STATUS_PASSED, outcome.detail
    assert outcome.sent is True
    assert outcome.method_node_id
    assert len(calls) == 1
    event_id, comment = calls[0]
    assert event_id.Value == b"\x01"
    assert comment.Value.Text == "operator text"
    assert outcome.authorization is not None
    assert outcome.authorization.reference == f"test-{action}"


async def test_a_condition_without_the_method_is_refused(events_service) -> None:
    """A ConditionType has no Confirm; the client reports that and sends nothing."""
    service, model = events_service
    calls: list[tuple[object, object]] = []
    condition = await _add_condition(model, calls, "Acknowledge")

    outcome = await service.condition_action(
        condition.nodeid.to_string(), "01", "", action="confirm"
    )

    assert outcome.status == STATUS_SKIPPED
    assert outcome.sent is False
    assert "Confirm" in outcome.detail
    assert calls == []


async def test_an_unknown_condition_action_sends_nothing(events_service) -> None:
    service, model = events_service
    calls: list[tuple[object, object]] = []
    condition = await _add_condition(model, calls)

    outcome = await service.condition_action(
        condition.nodeid.to_string(), "01", "", action="restart"
    )

    assert outcome.status == STATUS_SKIPPED
    assert outcome.sent is False
    assert "not a condition action" in outcome.detail
    assert calls == []


async def test_acknowledge_refuses_a_method_with_the_wrong_signature(events_service) -> None:
    service, model = events_service
    calls: list[tuple[object, object]] = []

    objects = model.server.get_objects_node()
    wrong = await objects.add_object(ua.NodeId("WrongCondition", 2), ua.QualifiedName("WrongCondition", 2))

    async def acknowledge(parent, text):
        calls.append((text, text))
        return []

    await wrong.add_method(
        ua.NodeId("WrongAcknowledge", 2),
        ua.QualifiedName("Acknowledge"),
        acknowledge,
        [ua.VariantType.String],
        [],
    )

    outcome = await service.condition_action(
        wrong.nodeid.to_string(), "0102", "", action="acknowledge"
    )

    assert outcome.status == STATUS_SKIPPED
    assert outcome.sent is False
    assert "not called" in outcome.detail
    assert calls == []


async def test_acknowledge_refuses_an_event_without_an_event_id(events_service) -> None:
    service, _model = events_service

    with pytest.raises(OpcUaExplorerError):
        await service.condition_action(SERVER_OBJECT_NODE_ID, "", "", action="acknowledge")


async def test_a_bad_method_output_is_not_success(events_service) -> None:
    """The A&C methods return a StatusCode as output; a bad one is not success."""
    service, model = events_service
    objects = model.server.get_objects_node()
    condition = await objects.add_object(
        ua.NodeId("BadResultCondition", 2), ua.QualifiedName("BadResultCondition", 2)
    )

    async def confirm(parent, event_id, comment):
        return [ua.Variant(ua.StatusCode(ua.StatusCodes.BadEventIdUnknown), ua.VariantType.StatusCode)]

    await condition.add_method(
        ua.NodeId("BadResultConfirm", 2),
        ua.QualifiedName("Confirm"),
        confirm,
        [ua.VariantType.ByteString, ua.VariantType.LocalizedText],
        [ua.VariantType.StatusCode],
    )

    outcome = await service.condition_action(
        condition.nodeid.to_string(), "0102", "", action="confirm"
    )

    assert outcome.sent is True
    assert outcome.status == STATUS_FAILED
    assert outcome.status_code == "BadEventIdUnknown"
    assert "not accepted" in outcome.detail
    assert "Confirm" in outcome.detail


async def test_node_details_report_the_event_notifier(events_service) -> None:
    """The address-space panel shows why the Server object can be an event source."""
    service, model = events_service

    server_object = await service.read_node_details(SERVER_OBJECT_NODE_ID)
    server_attributes = {attribute.attribute: attribute for attribute in server_object.attributes}
    assert server_attributes["EventNotifier"].value == 1
    assert "SubscribeToEvents" in (server_attributes["EventNotifier"].display or "")

    plant = await service.read_node_details(model.node_ids["plant"])
    plant_attributes = {attribute.attribute: attribute for attribute in plant.attributes}
    assert plant_attributes["EventNotifier"].value == 0
    assert plant_attributes["EventNotifier"].display == "0 (none)"


async def test_condition_refresh_publishes_the_refresh_events(events_service) -> None:
    """ConditionRefresh is one call, and the server's RefreshStart/RefreshEnd prove it arrived."""
    service, _model = events_service
    received: list[EventNotification] = []
    start = await service.start_event_monitor(
        [SERVER_OBJECT_NODE_ID], EventConfig(publishing_interval_ms=200), emit=received.append
    )
    try:
        result = await service.refresh_event_conditions(reference="test-refresh")

        assert result.status == STATUS_PASSED, result.detail
        assert result.sent is True
        assert result.subscription_id == start.subscription_id
        assert result.object_node_id == "i=2782"
        assert result.method_node_id == "i=3875"
        assert result.authorization is not None
        assert result.authorization.reference == "test-refresh"
        # The refresh events are the only evidence the request was accepted.
        assert result.refresh_started is True, result.detail
        assert result.refresh_ended is True, result.detail
        assert [event.event_type for event in received if event.event_type_id == "i=2787"] == [
            "RefreshStartEventType"
        ]
        # Refreshing changes nothing: no variable was written and the session stays usable.
        assert await service.browse_children("i=85")
    finally:
        await service.event_monitor_stop()


async def test_condition_refresh_without_a_subscription_is_refused(events_service) -> None:
    service, _model = events_service
    with pytest.raises(MonitorNotActiveError):
        await service.refresh_event_conditions()


async def test_condition_refresh_reports_a_hidden_refresh_event(events_service) -> None:
    """A server-side severity filter can hide RefreshStart; that is reported, not claimed."""
    service, _model = events_service
    received: list[EventNotification] = []
    await service.start_event_monitor(
        [SERVER_OBJECT_NODE_ID],
        EventConfig(publishing_interval_ms=200, min_severity=10),
        emit=received.append,
    )
    try:
        result = await service.refresh_event_conditions()

        assert result.status == STATUS_PASSED, result.detail
        assert result.refresh_started is False
        assert "no RefreshStart event" in result.detail
        assert received == []
    finally:
        await service.event_monitor_stop()
