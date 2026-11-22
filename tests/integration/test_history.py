"""Raw HistoryRead integration tests against the demo server's historian."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.errors import OpcUaExplorerError
from opcua_explorer.models import HistoryConfig
from opcua_explorer.opcua.client_service import OpcUaService


@pytest_asyncio.fixture
async def history_service(tmp_path):
    """Demo server with Temperature/Speed historized in memory."""
    model = await start_demo_server(update_values=False, history=True)
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
    try:
        yield service, model
    finally:
        await service.shutdown()
        await stop_demo_server(model)


async def _write_values(node, values: list[float]) -> None:
    for value in values:
        await node.write_value(value)
        await asyncio.sleep(0.05)


async def test_raw_history_returns_values_oldest_first(history_service) -> None:
    service, model = history_service
    await _write_values(model.temperature, [21.5, 22.0, 22.5])

    result = await service.read_history(model.node_ids["temperature"], HistoryConfig(max_values=100))

    assert result.status == "Good"
    assert result.historizing is True
    assert result.pages >= 1
    assert result.truncated is False
    assert [value.value for value in result.values] == [21.5, 22.0, 22.5]
    timestamps = [value.source_timestamp for value in result.values]
    assert timestamps == sorted(timestamps)  # type: ignore[type-var]
    assert all(timestamp and timestamp.endswith("Z") for timestamp in timestamps)
    assert "3 value(s)" in result.summary_text()


async def test_a_node_without_history_is_empty_not_an_error(history_service) -> None:
    service, model = history_service

    result = await service.read_history(model.node_ids["pressure"], HistoryConfig(max_values=10))

    assert result.status == "Good"
    assert result.values == []
    assert result.historizing is False
    assert "Historizing = false" in result.detail


async def test_the_value_cap_is_applied(history_service) -> None:
    service, model = history_service
    await _write_values(model.temperature, [1.0, 2.0, 3.0, 4.0])

    result = await service.read_history(model.node_ids["temperature"], HistoryConfig(max_values=2))

    assert result.status == "Good"
    assert len(result.values) <= 2
    assert result.requested_max_values == 2


async def test_a_reversed_range_is_refused(history_service) -> None:
    service, model = history_service
    start = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)

    with pytest.raises(OpcUaExplorerError):
        await service.read_history(
            model.node_ids["temperature"],
            HistoryConfig(start=start, end=start - timedelta(hours=1)),
        )
