"""Live-monitor integration tests against the real demo server."""

from __future__ import annotations

import asyncio

import pytest_asyncio

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import MonitorConfig, MonitorNotification
from opcua_explorer.opcua.client_service import OpcUaService


@pytest_asyncio.fixture
async def monitor_service(tmp_path):
    """Demo server whose Temperature/Speed change every second."""
    model = await start_demo_server(update_values=True)
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
    try:
        yield service, model
    finally:
        await service.shutdown()
        await stop_demo_server(model)


async def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_monitor_streams_notifications_and_can_be_extended(monitor_service) -> None:
    service, model = monitor_service
    received: list[MonitorNotification] = []

    start = await service.start_monitor(
        [model.node_ids["temperature"]],
        MonitorConfig(publishing_interval_ms=200, sampling_interval_ms=100),
        emit=received.append,
    )
    assert start.subscription_id is not None
    assert start.items[0].ok is True, start.items[0].status

    # Add a second node while the subscription is running.
    added = await service.monitor_add([model.node_ids["speed"]])
    assert added[0].ok is True, added[0].status

    assert await _wait_for(lambda: len(received) >= 2), "no notifications arrived"
    first = received[0]
    assert first.node_id in (model.node_ids["temperature"], model.node_ids["speed"])
    assert first.status == "Good"
    assert first.value_text
    assert first.sequence >= 1

    # Remove the second item again; the subscription and the session stay usable.
    await service.monitor_remove([added[0].client_handle])
    assert await service.browse_children("i=85")

    await service.monitor_stop()
    assert service.is_monitoring is False


async def test_rejected_node_is_reported_not_raised(monitor_service) -> None:
    service, _model = monitor_service
    start = await service.start_monitor(
        ["ns=2;s=DoesNotExist"],
        MonitorConfig(),
        emit=lambda _event: None,
    )
    assert start.items[0].ok is False
    assert start.items[0].status != "Good"

    # The subscription exists, so the session is still healthy.
    assert await service.browse_children("i=85")
    await service.monitor_stop()


async def test_monitor_stop_is_safe_without_a_subscription(monitor_service) -> None:
    service, _model = monitor_service
    await service.monitor_stop()
    assert service.is_monitoring is False
