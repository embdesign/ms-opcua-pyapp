"""Read-only server-status and diagnostics snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .common import iso_utc, utc_now
from .monitor import MonitoredItemInfo


@dataclass(slots=True)
class ServerStatusSnapshot:
    """One read-only look at ServerStatus, ServerDiagnostics and this session.

    Optional standard variables that a server reports as Good-but-null are in
    ``not_provided``; values with a Bad StatusCode are in ``unavailable``. The
    monitored items are the client's own view of its live-monitor subscription
    (``GetMonitoredItems`` is not called).
    """

    state: str = "Unknown"
    current_time: str | None = None
    start_time: str | None = None
    uptime_seconds: float | None = None
    seconds_till_shutdown: int | None = None
    shutdown_reason: str | None = None
    diagnostics_enabled: bool | None = None
    counters: dict[str, int] = field(default_factory=dict)
    limits: dict[str, int | float] = field(default_factory=dict)
    not_provided: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)
    monitor_subscription_id: int | None = None
    monitored_items: list[MonitoredItemInfo] = field(default_factory=list)
    received_at: datetime = field(default_factory=utc_now)

    @property
    def has_monitor(self) -> bool:
        return self.monitor_subscription_id is not None

    def summary_text(self) -> str:
        parts = [f"Server {self.state}"]
        if self.uptime_seconds is not None:
            parts.append(f"uptime {self.uptime_seconds:.0f} s")
        parts.append(f"{len(self.counters)} diagnostic value(s)")
        if self.not_provided:
            parts.append(f"{len(self.not_provided)} not provided")
        if self.unavailable:
            parts.append(f"{len(self.unavailable)} unavailable")
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "current_time": self.current_time,
            "start_time": self.start_time,
            "uptime_seconds": self.uptime_seconds,
            "seconds_till_shutdown": self.seconds_till_shutdown,
            "shutdown_reason": self.shutdown_reason,
            "diagnostics_enabled": self.diagnostics_enabled,
            "counters": dict(self.counters),
            "limits": dict(self.limits),
            "not_provided": list(self.not_provided),
            "unavailable": dict(self.unavailable),
            "monitor_subscription_id": self.monitor_subscription_id,
            "monitored_items": [item.to_dict() for item in self.monitored_items],
            "received_at_utc": iso_utc(self.received_at),
        }
