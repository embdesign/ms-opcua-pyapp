"""HistoryUpdate against the demo server: the refusal is reported, never raised."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import (
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    HistoryUpdateConfig,
    HistoryUpdateEntry,
)
from opcua_explorer.opcua.client_service import OpcUaService

START = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def history_service(tmp_path):
    model = await start_demo_server(update_values=False, history=True)
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
    try:
        yield service, model
    finally:
        await service.shutdown()
        await stop_demo_server(model)


async def test_a_refused_history_update_is_reported_with_its_status_code(history_service) -> None:
    service, model = history_service
    config = HistoryUpdateConfig(
        action="insert",
        node_id=model.node_ids["temperature"],
        entries=[HistoryUpdateEntry(source_timestamp=START, value_text="21.5")],
    ).normalised()

    inspection = await service.history_update_preview(config)
    assert inspection.ok is True, inspection.detail

    outcome = await service.write_history(config, reference="integration test")

    # asyncua's demo server refuses HistoryUpdate for a User role; the failure is
    # the server's answer, reported with its StatusCode and never raised.
    assert outcome.status in (STATUS_FAILED, STATUS_OUTCOME_UNKNOWN)
    assert outcome.sent is True
    assert outcome.status_code
    assert outcome.status_code != "Good"
