"""Unit tests for the ServerStatus/ServerDiagnostics snapshot (no server)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from asyncua import ua

from opcua_explorer.formatting import format_duration
from opcua_explorer.models import MonitoredItemInfo
from opcua_explorer.opcua.server_diagnostics import read_server_status


def _good(value: Any, variant_type: ua.VariantType) -> ua.DataValue:
    return ua.DataValue(ua.Variant(value, variant_type))


def _bad() -> ua.DataValue:
    data_value = ua.DataValue()
    data_value.StatusCode = ua.StatusCode(ua.StatusCodes.BadServiceUnsupported)
    return data_value


class _Session:
    """Returns one DataValue per requested NodeId; unlisted nodes are Good-null."""

    def __init__(self, values: dict[int, ua.DataValue]) -> None:
        self.values = values

    async def read(self, params: ua.ReadParameters) -> list[ua.DataValue]:
        results: list[ua.DataValue] = []
        for read_value in params.NodesToRead:
            identifier = int(read_value.NodeId.Identifier)
            results.append(
                self.values.get(identifier, _good(None, ua.VariantType.Null))
            )
        return results


def test_format_duration_is_readable_and_clamped() -> None:
    assert format_duration(None) == ""
    assert format_duration(0) == "00:00:00"
    assert format_duration(3661) == "01:01:01"
    assert format_duration(90_061) == "1 d 01:01:01"
    assert format_duration(-5) == "00:00:00"


async def test_snapshot_decodes_values_and_records_absent_ones() -> None:
    started = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    current = started + timedelta(seconds=3661)
    session = _Session(
        {
            ua.ObjectIds.Server_ServerStatus_State: _good(0, ua.VariantType.Int32),
            ua.ObjectIds.Server_ServerStatus_CurrentTime: _good(current, ua.VariantType.DateTime),
            ua.ObjectIds.Server_ServerStatus_StartTime: _good(started, ua.VariantType.DateTime),
            ua.ObjectIds.Server_ServerStatus_SecondsTillShutdown: _good(120, ua.VariantType.UInt32),
            ua.ObjectIds.Server_ServerStatus_ShutdownReason: _good(
                ua.LocalizedText("Maintenance"), ua.VariantType.LocalizedText
            ),
            ua.ObjectIds.Server_ServerDiagnostics_EnabledFlag: _good(True, ua.VariantType.Boolean),
            ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CurrentSessionCount: _good(
                3, ua.VariantType.UInt32
            ),
            # Rejected because the server does not implement it.
            ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_RejectedSessionCount: _bad(),
            ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead: _good(
                100, ua.VariantType.UInt32
            ),
            ua.ObjectIds.Server_ServerCapabilities_MinSupportedSampleRate: _good(
                50.0, ua.VariantType.Double
            ),
            # ServerViewCount is simply not listed: Good-null.
        }
    )
    item = MonitoredItemInfo(node_id="ns=2;s=Temperature", client_handle=42, sampling_interval_ms=250)
    monitor = SimpleNamespace(is_active=True, subscription_id=7, items_snapshot=lambda: [item])

    snapshot = await read_server_status(session, monitor=monitor)

    assert snapshot.state == "Running"
    assert snapshot.current_time == "2026-11-17T11:01:01.000Z"
    assert snapshot.start_time == "2026-11-17T10:00:00.000Z"
    assert snapshot.uptime_seconds == 3661
    assert snapshot.seconds_till_shutdown == 120
    assert snapshot.shutdown_reason == "Maintenance"
    assert snapshot.diagnostics_enabled is True
    assert snapshot.counters == {"CurrentSessionCount": 3}
    assert snapshot.limits == {"MaxNodesPerRead": 100, "MinSupportedSampleRate": 50.0}
    assert "ServerViewCount" in snapshot.not_provided
    assert snapshot.unavailable == {"RejectedSessionCount": "BadServiceUnsupported"}
    assert snapshot.monitor_subscription_id == 7
    assert snapshot.monitored_items == [item]
    assert snapshot.has_monitor is True
    payload = snapshot.to_dict()
    assert payload["state"] == "Running"
    assert payload["monitored_items"][0]["node_id"] == "ns=2;s=Temperature"
    assert "received_at_utc" in payload


async def test_snapshot_without_a_monitor_has_no_items() -> None:
    session = _Session({ua.ObjectIds.Server_ServerStatus_State: _good(0, ua.VariantType.Int32)})

    snapshot = await read_server_status(session)

    assert snapshot.has_monitor is False
    assert snapshot.monitored_items == []
    assert snapshot.monitor_subscription_id is None
