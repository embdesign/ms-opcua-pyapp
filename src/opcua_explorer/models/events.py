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
from .common import STATUS_FAILED, STATUS_PASSED, iso_utc, utc_now
from .writes import WriteAuthority

#: One event field longer than this is shortened before it reaches the GUI or a
#: log; event payloads are server-defined and may contain large structures.
EVENT_FIELD_CHAR_LIMIT = 2000

#: The standard A&C ``ConditionRefresh`` method (a component of ConditionType) and
#: the object that owns it. Refreshing is the only other condition method this
#: client calls besides ``Acknowledge``, and it changes no data.
CONDITION_REFRESH_METHOD_ID = "i=3875"
CONDITION_TYPE_NODE_ID = "i=2782"
#: Event types the server publishes around a refresh; their arrival is the only
#: local evidence that the request was accepted.
REFRESH_START_EVENT_TYPE_ID = "i=2787"
REFRESH_END_EVENT_TYPE_ID = "i=2788"


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
class ConditionRefreshResult:
    """What one ``ConditionRefresh`` call did, and whether its refresh events arrived.

    The call stores nothing and interrupts nothing: it asks the server to publish
    the Conditions it already retains for one subscription again. A ``RefreshStart``
    and a ``RefreshEnd`` event on that subscription are the only local evidence of
    acceptance; their absence is reported and never turned into a claim.
    """

    subscription_id: int | None = None
    object_node_id: str = ""
    method_node_id: str = ""
    status: str = STATUS_FAILED
    status_code: str = ""
    detail: str = ""
    sent: bool = False
    refresh_started: bool = False
    refresh_ended: bool = False
    #: How long the client waited for the refresh events after the call returned.
    observation_ms: float = 0.0
    duration_ms: float = 0.0
    authorization: WriteAuthority | None = None
    received_at: datetime = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_PASSED

    def summary_text(self) -> str:
        head = f"ConditionRefresh on subscription {self.subscription_id} via {self.object_node_id}"
        if self.refresh_started and self.refresh_ended:
            evidence = "RefreshStart and RefreshEnd were published"
        elif self.refresh_started:
            evidence = "RefreshStart was published, RefreshEnd has not arrived yet"
        else:
            evidence = "no RefreshStart event was seen on this subscription"
        parts = [f"{head}: {self.detail or self.status}", evidence]
        if self.status_code:
            parts.append(self.status_code)
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "object_node_id": self.object_node_id,
            "method_node_id": self.method_node_id,
            "status": self.status,
            "status_code": self.status_code,
            "detail": self.detail,
            "sent": self.sent,
            "refresh_started": self.refresh_started,
            "refresh_ended": self.refresh_ended,
            "observation_ms": round(self.observation_ms, 3),
            "duration_ms": round(self.duration_ms, 3),
            "authorization": self.authorization.describe() if self.authorization else "",
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
