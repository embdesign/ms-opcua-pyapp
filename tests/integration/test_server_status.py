"""Server status/diagnostics integration tests against the demo server."""

from __future__ import annotations

from opcua_explorer.models import MonitorConfig


async def test_server_status_snapshot_is_read_only_and_decoded(service) -> None:
    snapshot = await service.server_status()

    assert snapshot.state == "Running"
    assert snapshot.current_time is not None and snapshot.current_time.endswith("Z")
    assert snapshot.start_time is not None
    assert snapshot.uptime_seconds is not None and snapshot.uptime_seconds >= 0
    assert snapshot.limits["MaxNodesPerRead"] == 10000
    assert snapshot.limits["MaxNodesPerBrowse"] == 10000
    # The asyncua demo server advertises the summary but returns null values; the
    # snapshot must say "not provided" instead of inventing a zero.
    assert "CurrentSessionCount" in snapshot.not_provided
    assert snapshot.counters == {}
    # No subscription was created by refreshing diagnostics.
    assert snapshot.has_monitor is False
    assert snapshot.monitored_items == []


async def test_server_status_includes_this_sessions_monitor(service, demo_model) -> None:
    await service.start_monitor(
        [demo_model.node_ids["temperature"]],
        MonitorConfig(publishing_interval_ms=200, sampling_interval_ms=100),
        emit=lambda _event: None,
    )
    try:
        snapshot = await service.server_status()
        assert snapshot.has_monitor is True
        assert snapshot.monitor_subscription_id is not None
        assert [item.node_id for item in snapshot.monitored_items] == [demo_model.node_ids["temperature"]]
        assert snapshot.monitored_items[0].client_handle is not None
    finally:
        await service.monitor_stop()
