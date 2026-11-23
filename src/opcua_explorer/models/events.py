"""Event-subscription, stored-event and condition-state records.

Condition events additionally carry the identity needed for the guarded condition
methods (Acknowledge, Confirm, Enable, Disable, AddComment); this data alone never
writes anything. Condition *state* is read on demand from the condition object's
own variables - never inferred from an event and never cached as client state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import (
    DEFAULT_EVENT_PUBLISHING_INTERVAL_MS,
    DEFAULT_HISTORY_MAX_VALUES,
    MAX_HISTORY_MAX_VALUES,
    MAX_PUBLISHING_INTERVAL_MS,
    MIN_EVENT_PUBLISHING_INTERVAL_MS,
)
from .common import STATUS_FAILED, STATUS_PASSED, iso_utc, utc_now
from .writes import WriteAuthority

#: One event field longer than this is shortened before it reaches the GUI or a
#: log; event payloads are server-defined and may contain large structures.
EVENT_FIELD_CHAR_LIMIT = 2000

#: The standard A&C ``ConditionRefresh`` method (a component of ConditionType) and
#: the object that owns it. Refreshing changes no data; it is the only condition
#: method this client calls without the condition identity of one event.
CONDITION_REFRESH_METHOD_ID = "i=3875"
CONDITION_TYPE_NODE_ID = "i=2782"
#: Event types the server publishes around a refresh; their arrival is the only
#: local evidence that the request was accepted.
REFRESH_START_EVENT_TYPE_ID = "i=2787"
REFRESH_END_EVENT_TYPE_ID = "i=2788"

#: The condition methods this client may call on one selected condition event, in
#: the order the Events tab offers them. Every one takes the same two arguments -
#: EventId (ByteString) and Comment (LocalizedText) - and is resolved below the
#: condition node by its standard browse name, never by a NodeId the client
#: invented. ``Acknowledge``/``Confirm`` exist on AcknowledgeableConditionType,
#: ``Enable``/``Disable``/``AddComment`` on ConditionType; a condition that does not
#: expose one of them answers with the refusal, which is reported and never retried.
CONDITION_ACTIONS = ("acknowledge", "confirm", "enable", "disable", "comment")
#: Action key -> standard method browse name.
CONDITION_ACTION_METHODS = {
    "acknowledge": "Acknowledge",
    "confirm": "Confirm",
    "enable": "Enable",
    "disable": "Disable",
    "comment": "AddComment",
}
#: Action key -> operator-facing button/report label.
CONDITION_ACTION_LABELS = {
    "acknowledge": "Acknowledge",
    "confirm": "Confirm",
    "enable": "Enable",
    "disable": "Disable",
    "comment": "Add comment",
}

#: State variables the alarm summary reads from a condition object, in display
#: order. They are resolved by standard browse name below the condition node; the
#: client never invents a NodeId for them. A condition that does not expose one is
#: reported as "not exposed", never as False.
CONDITION_STATE_PROPERTIES = ("AckedState", "ConfirmedState", "EnabledState", "Retain")
#: Optional descriptive properties read together with the states.
CONDITION_SUMMARY_PROPERTIES = ("ConditionName", "LastSeverity")


def condition_action_label(action: str) -> str:
    """Operator-facing label of one condition action; unknown keys are shown verbatim."""
    return CONDITION_ACTION_LABELS.get(str(action), str(action))


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


@dataclass(slots=True)
class EventHistoryConfig:
    """Bounds and filter of one ``HistoryRead`` of stored events.

    ``event_type_node_id`` selects the fields, exactly as for a live subscription;
    the same standard fields are always selected so the summary columns work.
    ``min_severity`` is a server-side ``WhereClause``.
    """

    start: datetime | None = None
    end: datetime | None = None
    max_values: int = DEFAULT_HISTORY_MAX_VALUES
    event_type_node_id: str = ""
    select_fields: list[str] = field(default_factory=list)
    min_severity: int | None = None

    def normalised(self) -> EventHistoryConfig:
        fields: list[str] = []
        for name in self.select_fields:
            cleaned = str(name).strip()
            if cleaned and cleaned not in fields:
                fields.append(cleaned)
        return EventHistoryConfig(
            start=self.start,
            end=self.end,
            max_values=min(MAX_HISTORY_MAX_VALUES, max(1, int(self.max_values))),
            event_type_node_id=str(self.event_type_node_id or "").strip(),
            select_fields=fields,
            min_severity=None if self.min_severity is None else max(0, min(int(self.min_severity), 65_535)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": iso_utc(self.start),
            "end": iso_utc(self.end),
            "max_values": self.max_values,
            "event_type_node_id": self.event_type_node_id,
            "select_fields": list(self.select_fields),
            "min_severity": self.min_severity,
        }


@dataclass(slots=True)
class EventHistoryResult:
    """Stored events of one source node, oldest first.

    A Good StatusCode with no events is normal: the source may not be historized,
    or the range may be empty. The server decides what it kept.
    """

    node_id: str
    events: list[EventNotification] = field(default_factory=list)
    status: str = "Good"
    truncated: bool = False
    requested_max_values: int = 0
    pages: int = 0
    detail: str = ""
    duration_ms: float = 0.0
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        parts = [f"Stored events of {self.node_id}: {len(self.events)} event(s)"]
        if self.status != "Good":
            parts.append(f"HistoryRead {self.status}")
        if self.truncated:
            parts.append("truncated at the requested maximum")
        if self.detail:
            parts.append(self.detail)
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "truncated": self.truncated,
            "requested_max_values": self.requested_max_values,
            "pages": self.pages,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "events": [event.to_dict() for event in self.events],
            "received_at_utc": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class ConditionState:
    """The state variables of one condition object, as the server reports them.

    ``acked_state``/``confirmed_state``/``enabled_state`` are the display text of
    the ``TwoStateVariableType``/``ConditionVariableType`` Value; the matching
    ``*_id`` is its Boolean ``Id`` property (the machine-readable state). A state
    the condition does not expose is ``""``/``None`` and named in ``missing``.
    """

    node_id: str
    condition_name: str = ""
    acked_state: str = ""
    acked_id: bool | None = None
    confirmed_state: str = ""
    confirmed_id: bool | None = None
    enabled_state: str = ""
    enabled_id: bool | None = None
    retain: bool | None = None
    last_severity: int | None = None
    missing: list[str] = field(default_factory=list)
    status: str = "Good"
    detail: str = ""

    def acked_text(self) -> str:
        return _state_text(self.acked_state, self.acked_id)

    def confirmed_text(self) -> str:
        return _state_text(self.confirmed_state, self.confirmed_id)

    def enabled_text(self) -> str:
        return _state_text(self.enabled_state, self.enabled_id)

    def retain_text(self) -> str:
        return "not exposed" if self.retain is None else ("True" if self.retain else "False")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "condition_name": self.condition_name,
            "acked_state": self.acked_state,
            "acked_id": self.acked_id,
            "confirmed_state": self.confirmed_state,
            "confirmed_id": self.confirmed_id,
            "enabled_state": self.enabled_state,
            "enabled_id": self.enabled_id,
            "retain": self.retain,
            "last_severity": self.last_severity,
            "missing": list(self.missing),
            "status": self.status,
            "detail": self.detail,
        }


def _state_text(text: str, value: bool | None) -> str:
    if text:
        return text
    if value is None:
        return "not exposed"
    return "True" if value else "False"


@dataclass(slots=True)
class ConditionStateSnapshot:
    """On-demand state of several condition objects, read once.

    This is a read of the server's own state variables at one moment, not a
    client-side condition model: nothing is tracked between reads and no state is
    derived from an event.
    """

    conditions: list[ConditionState] = field(default_factory=list)
    status: str = "Good"
    detail: str = ""
    duration_ms: float = 0.0
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        if not self.conditions:
            return "No condition object was read."
        missing = sum(1 for condition in self.conditions if condition.missing)
        parts = [f"State of {len(self.conditions)} condition(s) read on demand"]
        if missing:
            parts.append(f"{missing} expose no state variable")
        if self.detail:
            parts.append(self.detail)
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "conditions": [condition.to_dict() for condition in self.conditions],
            "received_at_utc": iso_utc(self.received_at),
        }
