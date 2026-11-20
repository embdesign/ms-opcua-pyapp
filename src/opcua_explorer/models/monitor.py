"""Live-monitoring subscription configuration and notification records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import DEFAULT_PUBLISHING_INTERVAL_MS, DEFAULT_SAMPLING_INTERVAL_MS
from .common import iso_utc, utc_now

#: A notification value longer than this is truncated before it is sent to the
#: GUI, so a burst of large arrays cannot exhaust memory.
MONITOR_VALUE_CHAR_LIMIT = 4000

#: Data change triggers the client can request (``DataChangeTrigger`` values).
DATA_CHANGE_TRIGGERS = ("Status", "StatusValue", "StatusValueTimestamp")
#: Deadband types the client can request (``DeadbandType`` values).
DEADBAND_TYPES = ("None", "Absolute", "Percent")


@dataclass(slots=True)
class MonitorConfig:
    """Timing and filter for one live-monitoring subscription.

    The server may revise both intervals; the values here are the requested
    ones. Sampling interval ``0`` means "fastest the server supports".

    ``trigger`` and ``deadband_type`` map to the OPC UA ``DataChangeFilter``.
    The default pair (``StatusValue`` / ``None``) is exactly the server's
    default behaviour, so no filter is sent unless the operator changes one.
    """

    publishing_interval_ms: float = DEFAULT_PUBLISHING_INTERVAL_MS
    sampling_interval_ms: float = DEFAULT_SAMPLING_INTERVAL_MS
    #: Server-side queue size per monitored item; ``0`` uses the server default.
    queue_size: int = 0
    trigger: str = "StatusValue"
    deadband_type: str = "None"
    deadband_value: float = 0.0

    def normalised(self) -> MonitorConfig:
        trigger = self.trigger if self.trigger in DATA_CHANGE_TRIGGERS else "StatusValue"
        deadband_type = self.deadband_type if self.deadband_type in DEADBAND_TYPES else "None"
        deadband_value = max(0.0, float(self.deadband_value))
        if deadband_type == "Percent":
            deadband_value = min(100.0, deadband_value)
        return MonitorConfig(
            publishing_interval_ms=min(60_000.0, max(50.0, float(self.publishing_interval_ms))),
            sampling_interval_ms=min(60_000.0, max(0.0, float(self.sampling_interval_ms))),
            queue_size=min(1000, max(0, int(self.queue_size))),
            trigger=trigger,
            deadband_type=deadband_type,
            deadband_value=deadband_value,
        )

    @property
    def needs_filter(self) -> bool:
        """True when the requested trigger/deadband differs from the UA default."""
        config = self.normalised()
        return config.trigger != "StatusValue" or config.deadband_type != "None"

    def to_dict(self) -> dict[str, Any]:
        return {
            "publishing_interval_ms": self.publishing_interval_ms,
            "sampling_interval_ms": self.sampling_interval_ms,
            "queue_size": self.queue_size,
            "trigger": self.trigger,
            "deadband_type": self.deadband_type,
            "deadband_value": self.deadband_value,
        }


@dataclass(slots=True)
class MonitoredItemInfo:
    """One requested monitored item and what the server made of it."""

    node_id: str
    status: str = "pending"
    ok: bool = True
    client_handle: int | None = None
    sampling_interval_ms: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "ok": self.ok,
            "client_handle": self.client_handle,
            "sampling_interval_ms": self.sampling_interval_ms,
            "detail": self.detail,
        }


@dataclass(slots=True)
class MonitorNotification:
    """One data-change notification, already JSON-safe for display."""

    sequence: int
    node_id: str
    value: Any = None
    value_text: str = ""
    status: str = "Good"
    variant_type: str | None = None
    source_timestamp: str | None = None
    server_timestamp: str | None = None
    received_at: datetime = field(default_factory=utc_now)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "node_id": self.node_id,
            "value": self.value,
            "status": self.status,
            "variant_type": self.variant_type,
            "source_timestamp": self.source_timestamp,
            "server_timestamp": self.server_timestamp,
            "received_at_utc": iso_utc(self.received_at),
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class MonitorStatusEvent:
    """A subscription status-change notification from the server."""

    status: str
    detail: str = ""
    received_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class MonitorStart:
    """Result of creating a live-monitoring subscription."""

    subscription_id: int | None
    requested_publishing_interval_ms: float
    sampling_interval_ms: float
    items: list[MonitoredItemInfo] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        accepted = sum(1 for item in self.items if item.ok)
        return (
            f"Subscription {self.subscription_id}: {accepted} of {len(self.items)} monitored item(s) active; "
            f"requested publishing interval {self.requested_publishing_interval_ms:.0f} ms, "
            f"sampling interval {self.sampling_interval_ms:.0f} ms."
        )
