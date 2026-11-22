"""Raw-history read configuration and result records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import DEFAULT_HISTORY_MAX_VALUES, MAX_HISTORY_MAX_VALUES
from .common import iso_utc, utc_now
from .graph import ValueSnapshot

#: Aggregate functions the client can request; the wire NodeIds live in
#: ``opcua/history_service.py``. ``""``/``None`` means a raw read.
HISTORY_AGGREGATES = ("average", "minimum", "maximum", "count")


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
