"""Unit tests for the live-monitor subscription wrapper (no server involved)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from asyncua import ua

from opcua_explorer.errors import MonitorNotActiveError
from opcua_explorer.models import MonitorConfig
from opcua_explorer.opcua.monitor_service import LiveMonitor

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
        self.unsubscribed: list[int] = []
        self.deleted = False

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

    # Stopping twice is harmless.
    await monitor.stop()
    await monitor.stop()
