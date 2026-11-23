"""Stored-event integration tests against the demo server's event historian."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest_asyncio

from opcua_explorer import cli_events
from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import EventHistoryConfig
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.opcua.event_service import SERVER_OBJECT_NODE_ID


@pytest_asyncio.fixture
async def event_history_service(tmp_path):
    """Demo server whose Server-object events are historized in memory."""
    model = await start_demo_server(update_values=False, event_history=True, event_interval=0.2)
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
    try:
        yield service, model
    finally:
        await service.shutdown()
        await stop_demo_server(model)


async def _wait_for_events(service: OpcUaService, minimum: int = 2, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await service.read_event_history(
            SERVER_OBJECT_NODE_ID, EventHistoryConfig(max_values=50)
        )
        if len(result.events) >= minimum:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("no stored events arrived")


async def test_stored_events_are_read_with_fields_and_status(event_history_service) -> None:
    service, _model = event_history_service
    await _wait_for_events(service)

    result = await service.read_event_history(
        SERVER_OBJECT_NODE_ID, EventHistoryConfig(max_values=50)
    )

    assert result.status == "Good"
    assert result.pages >= 1
    assert result.events
    event = result.events[-1]
    assert event.event_type == "BaseEventType"
    assert event.source_node == SERVER_OBJECT_NODE_ID
    assert "Demo event" in event.message
    assert event.event_id
    assert {"EventId", "EventType", "SourceNode", "Message", "Severity"} <= set(event.fields)
    # The HistoryRead path sorts chronologically, like the value history.
    times = [item.time or "" for item in result.events]
    assert times == sorted(times)


async def test_cli_events_history_prints_the_stored_events(
    event_history_service, tmp_path, capsys
) -> None:
    service, model = event_history_service
    await _wait_for_events(service)

    code = await asyncio.to_thread(
        cli_events.main,
        [
            "--endpoint",
            model.endpoint,
            "--trust-store",
            str(tmp_path / "trust.json"),
            "--history",
            "--json",
            "--quiet",
        ],
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "opcua-event-history"
    assert payload["result"]["status"] == "Good"
    assert payload["result"]["events"]


async def test_a_range_without_stored_events_is_empty_not_an_error(event_history_service) -> None:
    service, _model = event_history_service

    result = await service.read_event_history(
        SERVER_OBJECT_NODE_ID,
        EventHistoryConfig(
            start=datetime(2000, 1, 1, tzinfo=UTC),
            end=datetime(2000, 1, 2, tzinfo=UTC),
            max_values=10,
        ),
    )

    assert result.status == "Good"
    assert result.events == []
    assert "no stored events" in result.detail
