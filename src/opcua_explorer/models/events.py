"""Event-subscription configuration and notification records.

Condition events additionally carry the identity needed for one guarded
Acknowledge call; this data alone never writes anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import (
    DEFAULT_EVENT_PUBLISHING_INTERVAL_MS,
    MAX_PUBLISHING_INTERVAL_MS,
    MIN_EVENT_PUBLISHING_INTERVAL_MS,
)
from .common import iso_utc, utc_now

#: One event field longer than this is shortened before it reaches the GUI or a
#: log; event payloads are server-defined and may contain large structures.
EVENT_FIELD_CHAR_LIMIT = 2000


@dataclass(slots=True)
class EventConfig:
    """Timing and scope of one event subscription.

    ``event_type_node_id`` selects the event type whose fields are requested.
    Empty means the standard ``BaseEventType``: every event the source emits,
    with the standard fields.
    """

    publishing_interval_ms: float = DEFAULT_EVENT_PUBLISHING_INTERVAL_MS
    #: Server-side queue size per monitored item; ``0`` uses the server default.
    queue_size: int = 0
    event_type_node_id: str = ""
    #: Extra EventType properties requested in addition to the standard fields.
    #: ``"index:Name"`` selects a property outside namespace 0.
    select_fields: list[str] = field(default_factory=list)
    #: Server-side ``WhereClause``: only events with at least this Severity.
    min_severity: int | None = None

    def normalised(self) -> EventConfig:
        fields: list[str] = []
        for name in self.select_fields:
            cleaned = str(name).strip()
            if cleaned and cleaned not in fields:
                fields.append(cleaned)
        severity = None if self.min_severity is None else max(0, min(int(self.min_severity), 65_535))
        return EventConfig(
            publishing_interval_ms=min(
                MAX_PUBLISHING_INTERVAL_MS,
                max(MIN_EVENT_PUBLISHING_INTERVAL_MS, float(self.publishing_interval_ms)),
            ),
            queue_size=min(1000, max(0, int(self.queue_size))),
            event_type_node_id=str(self.event_type_node_id or "").strip(),
            select_fields=fields,
            min_severity=severity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "publishing_interval_ms": self.publishing_interval_ms,
            "queue_size": self.queue_size,
            "event_type_node_id": self.event_type_node_id,
            "select_fields": list(self.select_fields),
            "min_severity": self.min_severity,
        }


@dataclass(slots=True)
class EventSourceInfo:
    """One node whose events were requested, and what the server made of it."""

    node_id: str
    status: str = "pending"
    ok: bool = True
    client_handle: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "ok": self.ok,
            "client_handle": self.client_handle,
            "detail": self.detail,
        }


@dataclass(slots=True)
class EventNotification:
    """One event notification, already reduced to display-safe text.

    ``fields`` keeps every field the subscription selected, keyed by the name
    the server's event type gave it, so custom event types stay inspectable.
    """

    sequence: int
    event_type: str = ""
    event_type_id: str = ""
    source_node: str = ""
    source_name: str = ""
    severity: int | None = None
    message: str = ""
    time: str | None = None
    receive_time: str | None = None
    event_id: str = ""
    #: The condition node when the subscription selected condition fields; empty otherwise.
    condition_node_id: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    received_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "event_type_id": self.event_type_id,
            "source_node": self.source_node,
            "source_name": self.source_name,
            "severity": self.severity,
            "message": self.message,
            "time": self.time,
            "receive_time": self.receive_time,
            "event_id": self.event_id,
            "condition_node_id": self.condition_node_id,
            "fields": dict(self.fields),
            "received_at_utc": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class EventStart:
    """Result of creating an event subscription."""

    subscription_id: int | None
    requested_publishing_interval_ms: float
    sources: list[EventSourceInfo] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        accepted = sum(1 for source in self.sources if source.ok)
        return (
            f"Subscription {self.subscription_id}: {accepted} of {len(self.sources)} event source(s) active; "
            f"requested publishing interval {self.requested_publishing_interval_ms:.0f} ms."
        )
