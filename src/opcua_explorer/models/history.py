"""History read/update configuration and result records.

Reads are raw values or one aggregate. Updates are the three ``HistoryUpdate``
shapes this client sends: insert raw values, replace raw values, or delete a
raw range. A history update is not an OPC UA ``Write`` - it changes what the
server's historian stores, not the variable's live value - so it has its own
outcome record and its own confirmation text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import (
    DEFAULT_HISTORY_MAX_VALUES,
    MAX_HISTORY_MAX_VALUES,
)
from .common import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    iso_utc,
    utc_now,
)
from .graph import ValueSnapshot
from .writes import WriteAuthority

#: Aggregate functions the client can request; the wire NodeIds live in
#: ``opcua/history_service.py``. ``""``/``None`` means a raw read.
HISTORY_AGGREGATES = ("average", "minimum", "maximum", "count")

#: The ``HistoryUpdate`` shapes the client sends. Browse-name mapping lives in
#: ``opcua/history_service.py``; an unknown value is refused before anything is sent.
HISTORY_UPDATE_ACTIONS = ("insert", "replace", "delete")


@dataclass(slots=True)
class HistoryConfig:
    """Bounds for one raw ``HistoryRead`` call.

    ``start``/``end`` of ``None`` mean an open range; ``max_values`` is a hard
    client-side cap that also becomes ``NumValuesPerNode``.
    """

    start: datetime | None = None
    end: datetime | None = None
    max_values: int = DEFAULT_HISTORY_MAX_VALUES
    #: Raw when empty; otherwise one of :data:`HISTORY_AGGREGATES`.
    aggregate: str | None = None
    #: Processing interval in milliseconds for an aggregate read; ``0`` lets the
    #: server choose (one value over the whole range).
    processing_interval_ms: float = 0.0

    def normalised(self) -> HistoryConfig:
        aggregate = (self.aggregate or "").strip().lower() or None
        if aggregate not in HISTORY_AGGREGATES:
            aggregate = None
        return HistoryConfig(
            start=self.start,
            end=self.end,
            max_values=min(MAX_HISTORY_MAX_VALUES, max(1, int(self.max_values))),
            aggregate=aggregate,
            processing_interval_ms=max(0.0, float(self.processing_interval_ms)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": iso_utc(self.start),
            "end": iso_utc(self.end),
            "max_values": self.max_values,
            "aggregate": self.aggregate,
            "processing_interval_ms": self.processing_interval_ms,
        }


@dataclass(slots=True)
class HistoryResult:
    """Raw history of one variable, oldest value first.

    ``status`` is the HistoryRead StatusCode; ``historizing`` is the node's
    Historizing attribute when it could be read. A Good StatusCode with no
    values is not an error - many servers simply have no data in the range.
    """

    node_id: str
    values: list[ValueSnapshot] = field(default_factory=list)
    status: str = "Good"
    historizing: bool | None = None
    truncated: bool = False
    requested_max_values: int = 0
    pages: int = 0
    detail: str = ""
    duration_ms: float = 0.0
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        parts = [f"History for {self.node_id}: {len(self.values)} value(s)"]
        if self.status != "Good":
            parts.append(f"HistoryRead {self.status}")
        if self.historizing is False:
            parts.append("Historizing = false")
        if self.truncated:
            parts.append("truncated at the requested maximum")
        if self.detail:
            parts.append(self.detail)
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "historizing": self.historizing,
            "truncated": self.truncated,
            "requested_max_values": self.requested_max_values,
            "pages": self.pages,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "values": [snapshot.to_dict() for snapshot in self.values],
            "received_at_utc": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class HistoryUpdateEntry:
    """One stored value to insert or replace, with the timestamp that identifies it.

    ``value_text`` is parsed with the server's DataType/ValueRank before anything
    is sent. ``server_timestamp`` is optional; the server usually assigns it.
    """

    source_timestamp: datetime
    value_text: str = ""
    server_timestamp: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_timestamp": iso_utc(self.source_timestamp),
            "server_timestamp": iso_utc(self.server_timestamp),
            "value_text": self.value_text,
        }


@dataclass(slots=True)
class HistoryUpdateConfig:
    """One ``HistoryUpdate`` request: insert/replace values, or delete a range.

    ``insert``/``replace`` carry ``entries``; ``delete`` carries ``start``/``end``
    and no value. One node per request, never a batch over several nodes and
    never a retry.
    """

    action: str
    node_id: str
    entries: list[HistoryUpdateEntry] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None

    def normalised(self) -> HistoryUpdateConfig:
        # Entry count is validated by the service, not trimmed here: silently
        # sending fewer values than the operator listed would be a wrong update.
        return HistoryUpdateConfig(
            action=str(self.action or "").strip().lower(),
            node_id=str(self.node_id or "").strip(),
            entries=list(self.entries),
            start=self.start,
            end=self.end,
        )

    def describe_entries(self, limit: int = 5) -> str:
        """Short operator-facing list of the values this request carries."""
        if self.action == "delete":
            return ""
        parts = [
            f"{iso_utc(entry.source_timestamp)} = {entry.value_text}" for entry in self.entries[:limit]
        ]
        if len(self.entries) > limit:
            parts.append(f"… {len(self.entries) - limit} more")
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "node_id": self.node_id,
            "start": iso_utc(self.start),
            "end": iso_utc(self.end),
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(slots=True)
class HistoryUpdateOutcome:
    """What one ``HistoryUpdate`` call did, and what a read-back proved.

    ``verified`` is never an inference: it is ``True`` only when the request was
    accepted and the read-back contains every requested source timestamp
    (``insert``/``replace``) or no value in the deleted range (``delete``).
    ``None`` means the read-back could not decide the question, and
    ``outcome_unknown`` is used when the call was sent but no verdict exists.
    """

    action: str
    node_id: str
    status: str = STATUS_FAILED
    status_code: str = ""
    sent: bool = False
    entries_sent: int = 0
    operation_results: list[str] = field(default_factory=list)
    detail: str = ""
    verification: HistoryResult | None = None
    verified: bool | None = None
    duration_ms: float = 0.0
    authorization: WriteAuthority | None = None
    received_at: datetime = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_PASSED

    @property
    def refused(self) -> bool:
        return self.status == STATUS_SKIPPED

    def summary_text(self) -> str:
        parts = [f"History {self.action} on {self.node_id}: {self.status}"]
        if self.entries_sent:
            parts.append(f"{self.entries_sent} value(s) sent")
        if self.status_code:
            parts.append(self.status_code)
        if self.verified is True:
            parts.append("read-back confirms the stored timestamps")
        elif self.verified is False:
            parts.append("read-back does not show the requested result")
        if self.detail:
            parts.append(self.detail)
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "node_id": self.node_id,
            "status": self.status,
            "status_code": self.status_code,
            "sent": self.sent,
            "entries_sent": self.entries_sent,
            "operation_results": list(self.operation_results),
            "detail": self.detail,
            "verified": self.verified,
            "verification": self.verification.to_dict() if self.verification else None,
            "duration_ms": self.duration_ms,
            "authorization": self.authorization.describe() if self.authorization else "",
            "received_at_utc": iso_utc(self.received_at),
        }
