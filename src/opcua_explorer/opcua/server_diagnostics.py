"""Read-only ServerStatus and ServerDiagnostics snapshots.

Everything here is a single batched ``Read`` of standard namespace-0 variables.
Nothing is written, no method is called and no subscription is created:
``GetMonitoredItems`` is deliberately *not* invoked (the project has one guarded
method-call path only); the snapshot carries the client's own live-monitor view
instead.

Optional variables are handled explicitly: a Good StatusCode with a null value
means "not provided by this server" (``not_provided``), a Bad StatusCode means
"unavailable" (``unavailable``), and neither is treated as an error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from asyncua import ua

from ..models import ServerStatusSnapshot, iso_utc
from .attributes import AttributeSpec, read_attributes_bulk

#: ServerStatus and ServerDiagnostics values that are not counters or limits.
SERVER_STATUS_NODES: dict[str, int] = {
    "State": ua.ObjectIds.Server_ServerStatus_State,
    "CurrentTime": ua.ObjectIds.Server_ServerStatus_CurrentTime,
    "StartTime": ua.ObjectIds.Server_ServerStatus_StartTime,
    "SecondsTillShutdown": ua.ObjectIds.Server_ServerStatus_SecondsTillShutdown,
    "ShutdownReason": ua.ObjectIds.Server_ServerStatus_ShutdownReason,
    "DiagnosticsEnabled": ua.ObjectIds.Server_ServerDiagnostics_EnabledFlag,
}

#: ServerDiagnosticsSummary counters (current and cumulated).
SERVER_COUNTER_NODES: dict[str, int] = {
    "ServerViewCount": ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_ServerViewCount,
    "CurrentSessionCount": ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CurrentSessionCount,
    "CumulatedSessionCount": ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CumulatedSessionCount,
    "SecurityRejectedSessionCount": (
        ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_SecurityRejectedSessionCount
    ),
    "RejectedSessionCount": ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_RejectedSessionCount,
    "SessionTimeoutCount": ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_SessionTimeoutCount,
    "SessionAbortCount": ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_SessionAbortCount,
    "CurrentSubscriptionCount": (
        ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CurrentSubscriptionCount
    ),
    "CumulatedSubscriptionCount": (
        ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_CumulatedSubscriptionCount
    ),
    "PublishingIntervalCount": (
        ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_PublishingIntervalCount
    ),
    "SecurityRejectedRequestsCount": (
        ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_SecurityRejectedRequestsCount
    ),
    "RejectedRequestsCount": ua.ObjectIds.Server_ServerDiagnostics_ServerDiagnosticsSummary_RejectedRequestsCount,
}

#: ServerCapabilities limits that matter to this client's own operations.
SERVER_LIMIT_NODES: dict[str, int] = {
    "MaxNodesPerRead": ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead,
    "MaxNodesPerBrowse": ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerBrowse,
    "MaxMonitoredItemsPerCall": (
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxMonitoredItemsPerCall
    ),
    "MaxBrowseContinuationPoints": ua.ObjectIds.Server_ServerCapabilities_MaxBrowseContinuationPoints,
    "MaxSessions": ua.ObjectIds.Server_ServerCapabilities_MaxSessions,
    "MaxSubscriptions": ua.ObjectIds.Server_ServerCapabilities_MaxSubscriptions,
    "MinSupportedSampleRate": ua.ObjectIds.Server_ServerCapabilities_MinSupportedSampleRate,
}


def _state_name(value: Any) -> str:
    try:
        return ua.ServerState(int(value)).name
    except Exception:  # pragma: no cover - defensive
        return str(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return iso_utc(value)
    return str(value)


def _localized(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, ua.LocalizedText):
        return value.Text or None
    return str(value)


def _numeric(value: Any) -> int | float | None:
    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


async def read_server_status(session: Any, *, monitor: Any = None) -> ServerStatusSnapshot:
    """Read ServerStatus, ServerDiagnostics and this session's monitor view.

    One Read request carries every variable; a failing variable never aborts the
    snapshot.
    """
    entries: list[tuple[str, str, int]] = []
    entries.extend(("Status", name, node_id) for name, node_id in SERVER_STATUS_NODES.items())
    entries.extend(("Counter", name, node_id) for name, node_id in SERVER_COUNTER_NODES.items())
    entries.extend(("Limit", name, node_id) for name, node_id in SERVER_LIMIT_NODES.items())

    results = await read_attributes_bulk(
        session,
        (
            (ua.NodeId(node_id).to_string(), AttributeSpec(ua.AttributeIds.Value, name))
            for _group, name, node_id in entries
        ),
        chunk_size=len(entries),
    )

    snapshot = ServerStatusSnapshot()
    current: datetime | None = None
    started: datetime | None = None

    for group, name, node_id in entries:
        result = results.get((ua.NodeId(node_id).to_string(), name))
        if result is None:  # pragma: no cover - read_attributes_bulk always returns an entry
            snapshot.unavailable[name] = "Missing"
            continue
        if not result.ok:
            snapshot.unavailable[name] = result.status
            continue
        value = result.value
        if value is None:
            snapshot.not_provided.append(name)
            continue

        if group == "Status":
            if name == "State":
                snapshot.state = _state_name(value)
            elif name == "CurrentTime":
                current = value if isinstance(value, datetime) else None
                snapshot.current_time = _iso(value)
            elif name == "StartTime":
                started = value if isinstance(value, datetime) else None
                snapshot.start_time = _iso(value)
            elif name == "SecondsTillShutdown":
                shutdown = _numeric(value)
                snapshot.seconds_till_shutdown = int(shutdown) if shutdown is not None else None
            elif name == "ShutdownReason":
                snapshot.shutdown_reason = _localized(value)
            elif name == "DiagnosticsEnabled":
                snapshot.diagnostics_enabled = bool(value)
        elif group == "Counter":
            number = _numeric(value)
            if number is not None:
                snapshot.counters[name] = int(number)
        else:
            number = _numeric(value)
            if number is not None:
                snapshot.limits[name] = number

    if current is not None and started is not None:
        snapshot.uptime_seconds = max(0.0, (current - started).total_seconds())

    if monitor is not None and getattr(monitor, "is_active", False):
        snapshot.monitor_subscription_id = monitor.subscription_id
        snapshot.monitored_items = list(monitor.items_snapshot())

    return snapshot


__all__ = [
    "SERVER_COUNTER_NODES",
    "SERVER_LIMIT_NODES",
    "SERVER_STATUS_NODES",
    "read_server_status",
]
