"""Unit tests for the live-monitor subscription wrapper (no server involved)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from asyncua import ua

from opcua_explorer.errors import MonitorNotActiveError, OpcUaExplorerError
from opcua_explorer.models import MonitorConfig
from opcua_explorer.opcua.monitor_service import LiveMonitor, build_data_change_filter

REJECTED_NODE = "ns=2;s=Rejected"


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
        self.subscribed: list[tuple[str, dict[str, Any]]] = []
        self.created: list[ua.MonitoredItemCreateRequest] = []
        self.unsubscribed: list[int] = []
        self.deleted = False
        self.modes: list[ua.MonitoringMode] = []
        self.mode_statuses: list[ua.StatusCode] | None = None
        self.modified: list[tuple[int, float, int]] = []
        self.modify_status = ua.StatusCode(ua.StatusCodes.Good)
        self.updated: ua.ModifySubscriptionParameters | None = None
        self.parameters = SimpleNamespace(
            RequestedLifetimeCount=10_000,
            RequestedMaxKeepAliveCount=20,
            MaxNotificationsPerPublish=100,
            Priority=0,
        )

    async def subscribe_data_change(self, nodes: list[_FakeNode], **kwargs: Any) -> list[Any]:
        results: list[Any] = []
        for node in nodes:
            node_id = node.nodeid.to_string()
            if node_id == REJECTED_NODE:
                results.append(ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown))
                continue
            handle = 100 + len(self.subscribed)
            self.subscribed.append((node_id, kwargs))
            results.append(handle)
        return results

    async def create_monitored_items(
        self, requests: list[ua.MonitoredItemCreateRequest]
    ) -> list[int | ua.StatusCode]:
        results: list[int | ua.StatusCode] = []
        for request in requests:
            self.created.append(request)
            node_id = request.ItemToMonitor.NodeId.to_string()
            if node_id == REJECTED_NODE:
                results.append(ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown))
                continue
            results.append(300 + len(self.created))
        return results

    async def set_monitoring_mode(self, mode: ua.MonitoringMode) -> list[ua.StatusCode]:
        self.modes.append(mode)
        return self.mode_statuses or [ua.StatusCode(ua.StatusCodes.Good)]

    async def modify_monitored_item(
        self, handle: int, new_samp_time: ua.Duration, new_queuesize: int = 0, mod_filter_val: int = -1
    ) -> list[Any]:
        self.modified.append((handle, new_samp_time, new_queuesize))
        return [SimpleNamespace(StatusCode=self.modify_status, RevisedSamplingInterval=new_samp_time)]

    async def update(self, params: ua.ModifySubscriptionParameters) -> Any:
        self.updated = params
        return SimpleNamespace(RevisedPublishingInterval=150.0)

    async def unsubscribe(self, handles: list[int]) -> None:
        self.unsubscribed.extend(handles)

    async def delete(self) -> None:
        self.deleted = True


class _FakeClient:
    def __init__(self) -> None:
        self.params: ua.CreateSubscriptionParameters | None = None
        self.handler: Any = None
        self.subscription = _FakeSubscription()

    def get_node(self, node_id: str) -> _FakeNode:
        return _FakeNode(node_id)

    async def create_subscription(
        self, params: ua.CreateSubscriptionParameters, handler: Any
    ) -> _FakeSubscription:
        self.params = params
        self.handler = handler
        return self.subscription


def _data_change(value: Any, *, source: datetime | None = None) -> SimpleNamespace:
    data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Double))
    data_value.SourceTimestamp = source
    return SimpleNamespace(monitored_item=SimpleNamespace(Value=data_value))


async def test_start_subscribes_items_and_reports_rejections() -> None:
    client = _FakeClient()
    emitted: list[Any] = []
    monitor = LiveMonitor(client, emitted.append)

    start = await monitor.start(
        ["ns=2;s=Temperature", REJECTED_NODE, "ns=2;s=Temperature"],
        MonitorConfig(publishing_interval_ms=200, sampling_interval_ms=100),
    )

    assert start.subscription_id == 42
    assert start.requested_publishing_interval_ms == 200
    assert start.sampling_interval_ms == 100
    assert [item.node_id for item in start.items] == ["ns=2;s=Temperature", REJECTED_NODE]
    accepted, rejected = start.items
    assert accepted.ok and accepted.client_handle == 100
    assert rejected.ok is False
    assert rejected.status == "BadNodeIdUnknown"
    assert rejected.detail

    assert client.params is not None
    assert client.params.RequestedPublishingInterval == 200
    assert client.params.PublishingEnabled is True
    # Duplicate NodeIds are subscribed only once.
    assert [node_id for node_id, _kwargs in client.subscription.subscribed] == ["ns=2;s=Temperature"]
    assert client.subscription.subscribed[0][1]["sampling_interval"] == 100

    await monitor.stop()
    assert client.subscription.deleted is True
    assert monitor.is_active is False


async def test_notification_payload_is_encoded_and_sequenced() -> None:
    client = _FakeClient()
    emitted: list[Any] = []
    monitor = LiveMonitor(client, emitted.append)
    await monitor.start(["ns=2;s=Temperature"], MonitorConfig())

    source = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    await client.handler.datachange_notification(
        _FakeNode("ns=2;s=Temperature"), 21.5, _data_change(21.5, source=source)
    )
    await client.handler.datachange_notification(
        _FakeNode("ns=2;s=Temperature"), 22.5, _data_change(22.5)
    )

    first, second = emitted
    assert first.sequence == 1 and second.sequence == 2
    assert first.node_id == "ns=2;s=Temperature"
    assert first.value == 21.5
    assert first.value_text == "21.5"
    assert first.variant_type == "Double"
    assert first.status == "Good"
    assert first.source_timestamp == "2026-01-02T03:04:05.000Z"
    assert first.truncated is False


async def test_large_values_are_truncated_before_reaching_the_gui() -> None:
    client = _FakeClient()
    emitted: list[Any] = []
    monitor = LiveMonitor(client, emitted.append)
    await monitor.start(["ns=2;s=Big"], MonitorConfig())

    payload = "x" * 10_000
    await client.handler.datachange_notification(_FakeNode("ns=2;s=Big"), payload, _data_change(payload))

    notification = emitted[0]
    assert notification.truncated is True
    assert notification.value is None
    assert notification.value_text.endswith("…")
    assert len(notification.value_text) <= 4000


async def test_add_and_remove_while_running() -> None:
    client = _FakeClient()
    monitor = LiveMonitor(client, lambda _event: None)
    await monitor.start(["ns=2;s=A"], MonitorConfig())

    infos = await monitor.add(["ns=2;s=B"])
    assert infos[0].ok and infos[0].client_handle == 101
    assert monitor.item_count() == 2

    await monitor.remove([101])
    assert client.subscription.unsubscribed == [101]
    assert monitor.item_count() == 1


async def test_operations_require_a_running_subscription() -> None:
    monitor = LiveMonitor(_FakeClient(), lambda _event: None)

    with pytest.raises(MonitorNotActiveError):
        await monitor.add(["ns=2;s=A"])
    with pytest.raises(MonitorNotActiveError):
        await monitor.remove([1])
    with pytest.raises(MonitorNotActiveError):
        await monitor.set_monitoring_mode("Disabled")
    with pytest.raises(MonitorNotActiveError):
        await monitor.set_publishing_interval(300)
    with pytest.raises(MonitorNotActiveError):
        await monitor.set_sampling_interval(300)

    # Stopping twice is harmless.
    await monitor.stop()
    await monitor.stop()


def test_filter_config_is_normalised() -> None:
    default = MonitorConfig()
    assert default.needs_filter is False
    assert MonitorConfig(trigger="Status").needs_filter is True
    assert MonitorConfig(deadband_type="Absolute", deadband_value=0.5).needs_filter is True

    fixed = MonitorConfig(trigger="Nope", deadband_type="Nope", deadband_value=-5).normalised()
    assert fixed.trigger == "StatusValue"
    assert fixed.deadband_type == "None"
    assert fixed.deadband_value == 0.0

    percent = MonitorConfig(deadband_type="Percent", deadband_value=150).normalised()
    assert percent.deadband_value == 100.0


def test_data_change_filter_is_built_from_the_config() -> None:
    assert build_data_change_filter(MonitorConfig()) is None

    absolute = build_data_change_filter(
        MonitorConfig(deadband_type="Absolute", deadband_value=0.5)
    )
    assert absolute is not None
    assert absolute.Trigger == ua.DataChangeTrigger.StatusValue
    assert absolute.DeadbandType == ua.DeadbandType.Absolute
    assert absolute.DeadbandValue == 0.5

    triggered = build_data_change_filter(
        MonitorConfig(trigger="StatusValueTimestamp", deadband_type="Percent", deadband_value=5)
    )
    assert triggered is not None
    assert triggered.Trigger == ua.DataChangeTrigger.StatusValueTimestamp
    assert triggered.DeadbandType == ua.DeadbandType.Percent
    assert triggered.DeadbandValue == 5.0

    status_only = build_data_change_filter(MonitorConfig(trigger="Status"))
    assert status_only is not None
    assert status_only.Trigger == ua.DataChangeTrigger.Status
    assert status_only.DeadbandType == ua.DeadbandType.None_


async def test_a_filtered_subscription_creates_monitored_items_directly() -> None:
    client = _FakeClient()
    monitor = LiveMonitor(client, lambda _event: None)

    start = await monitor.start(
        ["ns=2;s=Temperature", REJECTED_NODE],
        MonitorConfig(
            sampling_interval_ms=250,
            queue_size=7,
            deadband_type="Absolute",
            deadband_value=1.0,
        ),
    )

    # subscribe_data_change cannot carry a filter, so nothing went through it.
    assert client.subscription.subscribed == []
    requests = client.subscription.created
    assert len(requests) == 2
    first, second = requests
    assert first.ItemToMonitor.AttributeId == ua.AttributeIds.Value
    assert first.MonitoringMode == ua.MonitoringMode.Reporting
    assert first.RequestedParameters.Filter.DeadbandType == ua.DeadbandType.Absolute
    assert first.RequestedParameters.Filter.DeadbandValue == 1.0
    assert first.RequestedParameters.SamplingInterval == 250
    assert first.RequestedParameters.QueueSize == 7
    assert first.RequestedParameters.DiscardOldest is True
    assert first.RequestedParameters.ClientHandle != second.RequestedParameters.ClientHandle

    assert start.items[0].ok is True and start.items[0].client_handle == 301
    assert start.items[1].ok is False and start.items[1].status == "BadNodeIdUnknown"


async def test_monitoring_mode_is_applied_and_refusals_are_reported() -> None:
    client = _FakeClient()
    monitor = LiveMonitor(client, lambda _event: None)
    await monitor.start(["ns=2;s=A"], MonitorConfig())
    assert monitor.monitoring_mode == "Reporting"

    assert await monitor.set_monitoring_mode("Disabled") == "Disabled"
    assert client.subscription.modes[-1] == ua.MonitoringMode.Disabled
    assert monitor.monitoring_mode == "Disabled"

    client.subscription.mode_statuses = [ua.StatusCode(ua.StatusCodes.BadServiceUnsupported)]
    with pytest.raises(OpcUaExplorerError):
        await monitor.set_monitoring_mode("Reporting")
    assert monitor.monitoring_mode == "Disabled"  # unchanged after a refusal

    with pytest.raises(OpcUaExplorerError):
        await monitor.set_monitoring_mode("NotAMode")

    await monitor.stop()
    assert monitor.monitoring_mode == "Reporting"


async def test_intervals_can_be_modified_while_running() -> None:
    client = _FakeClient()
    monitor = LiveMonitor(client, lambda _event: None)
    await monitor.start(["ns=2;s=A", "ns=2;s=B"], MonitorConfig(publishing_interval_ms=200, sampling_interval_ms=100))

    revised = await monitor.set_publishing_interval(300)
    assert revised == 150.0  # what the fake server revised to
    assert client.subscription.updated is not None
    assert client.subscription.updated.SubscriptionId == 42
    assert client.subscription.updated.RequestedPublishingInterval == 300
    assert client.subscription.updated.RequestedLifetimeCount == 10_000

    assert await monitor.set_sampling_interval(250) == []
    assert client.subscription.modified == [(100, 250.0, 0), (101, 250.0, 0)]

    client.subscription.modify_status = ua.StatusCode(ua.StatusCodes.BadMonitoredItemIdInvalid)
    failures = await monitor.set_sampling_interval(400)
    assert len(failures) == 2
    assert all("BadMonitoredItemIdInvalid" in failure for failure in failures)


async def test_a_dropped_subscription_is_reported_instead_of_encoding_none() -> None:
    """A subscription the server forgot must not reach the ModifySubscription encoder.

    asyncua recreates such a subscription and leaves ``subscription_id`` as ``None``;
    packing the id then raised ``struct.error: required argument is not an integer``
    from inside asyncua, so the operator saw an encoder error instead of the fact that
    the subscription was gone.
    """
    client = _FakeClient()
    monitor = LiveMonitor(client, lambda _event: None)
    await monitor.start(["ns=2;s=A"], MonitorConfig(publishing_interval_ms=200))

    client.subscription.subscription_id = None
    client.subscription.updated = None
    with pytest.raises(MonitorNotActiveError) as raised:
        await monitor.set_publishing_interval(300)
    assert "no longer knows this subscription" in str(raised.value)
    # Nothing was sent with a None id.
    assert client.subscription.updated is None
