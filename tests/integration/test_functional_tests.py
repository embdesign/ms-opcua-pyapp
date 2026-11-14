"""Functional test suite against the real demo server."""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import STATUS_PASSED, STATUS_SKIPPED, TestConfig
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.reporting import write_html_report, write_json_report


@pytest_asyncio.fixture
async def changing_server():
    """Demo server that changes Temperature/Speed every second."""
    model = await start_demo_server(update_values=True)
    try:
        yield model
    finally:
        await stop_demo_server(model)


@pytest_asyncio.fixture
async def changing_service(changing_server, tmp_path):
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=changing_server.endpoint))
    try:
        yield service
    finally:
        await service.shutdown()


def _by_id(run) -> dict[str, object]:
    return {check.id: check for check in run.checks}


async def test_suite_passes_on_a_live_server(changing_service, changing_server) -> None:
    run = await changing_service.run_functional_tests(
        TestConfig(variable_node_id=changing_server.node_ids["temperature"], notification_wait_seconds=4.0)
    )

    checks = _by_id(run)
    assert len(run.checks) == 11
    assert run.summary()["failed"] == 0
    assert checks["endpoint_discovery"].status == STATUS_PASSED
    assert checks["session_state"].status == STATUS_PASSED
    assert checks["namespace_array"].status == STATUS_PASSED
    assert checks["browse_root"].status == STATUS_PASSED
    assert checks["browse_target"].status == STATUS_PASSED
    assert checks["attribute_reads"].status == STATUS_PASSED
    assert checks["variable_read"].status == STATUS_PASSED
    assert checks["subscription_create"].status == STATUS_PASSED
    assert checks["latency"].status == STATUS_PASSED

    # Subscription creation and notification delivery are separate results.
    data_change = checks["subscription_data_change"]
    assert data_change.status == STATUS_PASSED
    assert data_change.data["notifications_received"] >= 1
    assert data_change.data["value_changes_observed"] >= 1
    assert data_change.status_codes

    # The disruptive check is skipped unless explicitly enabled.
    assert checks["connection_recovery"].status == STATUS_SKIPPED
    assert "opt-in" in checks["connection_recovery"].detail.lower()

    # Measurements are recorded, not asserted as thresholds.
    latency = checks["latency"].data
    assert latency["samples"] == 10
    assert latency["median_ms"] <= latency["max_ms"]
    assert len(latency["samples_ms"]) == 10

    assert run.coverage_limitations
    assert any("not official OPC UA certification" in item for item in run.coverage_limitations)
    assert run.environment["asyncua"] == "2.1.0"
    assert run.connection is not None


async def test_constant_value_is_not_a_failure(changing_service, changing_server) -> None:
    run = await changing_service.run_functional_tests(
        TestConfig(variable_node_id=changing_server.node_ids["pressure"], notification_wait_seconds=2.5)
    )
    checks = _by_id(run)

    assert checks["subscription_create"].status == STATUS_PASSED
    data_change = checks["subscription_data_change"]
    assert data_change.status == STATUS_PASSED
    assert data_change.data["value_changes_observed"] == 0
    assert data_change.data["notifications_received"] >= 1  # the initial value
    assert "not a failure" in data_change.detail.lower()
    assert run.summary()["failed"] == 0


async def test_variable_is_discovered_when_not_configured(changing_service) -> None:
    run = await changing_service.run_functional_tests(
        TestConfig(variable_node_id=None, notification_wait_seconds=1.0)
    )
    assert any("discovered" in note for note in run.notes)
    assert run.summary()["failed"] == 0
    variable_check = _by_id(run)["variable_read"]
    assert variable_check.status == STATUS_PASSED


async def test_channel_recovery_check_runs_when_enabled(changing_service, changing_server) -> None:
    run = await changing_service.run_functional_tests(
        TestConfig(
            variable_node_id=changing_server.node_ids["temperature"],
            notification_wait_seconds=1.0,
            latency_samples=3,
            include_channel_recovery=True,
        )
    )
    recovery = _by_id(run)["connection_recovery"]
    assert recovery.status == STATUS_PASSED
    assert recovery.data["state_after_recovery"] == "connected"
    assert recovery.data["read_after_recovery"] == "Good"
    assert recovery.data["recovery_ms"] >= 0

    # The session must still work after the interruption check.
    children = await changing_service.browse_children("i=85")
    assert children


async def test_suite_survives_cancellation(changing_service, changing_server) -> None:
    task = asyncio.create_task(
        changing_service.run_functional_tests(
            TestConfig(
                variable_node_id=changing_server.node_ids["temperature"],
                notification_wait_seconds=30.0,
            )
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    run = await task

    assert run.cancelled is True
    assert run.finished_at is not None
    assert isinstance(run.checks, list)
    assert run.summary()["cancelled"] is True

    # Subscription cleanup must not leave the session unusable.
    children = await changing_service.browse_children("i=85")
    assert children


async def test_reports_written_from_a_real_run(changing_service, changing_server, tmp_path) -> None:
    run = await changing_service.run_functional_tests(
        TestConfig(variable_node_id=changing_server.node_ids["temperature"], notification_wait_seconds=2.0)
    )
    json_path = tmp_path / "report.json"
    html_path = tmp_path / "report.html"
    json_result = write_json_report(run, json_path)
    html_result = write_html_report(run, html_path)

    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["format"] == "opcua-functional-test-report"
    assert document["summary"]["total"] == len(run.checks) == json_result.checks
    assert document["server"]["endpoint_url"] == changing_server.endpoint
    assert document["server"]["connection"]["authentication"] == "anonymous"
    assert document["configuration"]["variable_node_id"] == changing_server.node_ids["temperature"]
    assert all("status" in entry for entry in document["results"])

    html = html_path.read_text(encoding="utf-8")
    assert "OPC UA functional test report" in html
    assert "Subscription creation" in html
    assert html_result.checks == len(run.checks)


async def test_run_requires_a_session() -> None:
    from opcua_explorer.errors import NotConnectedError

    service = OpcUaService()
    with pytest.raises(NotConnectedError):
        await service.run_functional_tests(TestConfig())
