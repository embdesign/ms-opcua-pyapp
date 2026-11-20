"""Raw-history read configuration and result records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import DEFAULT_HISTORY_MAX_VALUES, MAX_HISTORY_MAX_VALUES
from .common import iso_utc, utc_now
from .graph import ValueSnapshot


@dataclass(slots=True)
class HistoryConfig:
    """Bounds for one raw ``HistoryRead`` call.

    ``start``/``end`` of ``None`` mean an open range; ``max_values`` is a hard
    client-side cap that also becomes ``NumValuesPerNode``.
    """

    start: datetime | None = None
    end: datetime | None = None
    max_values: int = DEFAULT_HISTORY_MAX_VALUES

    def normalised(self) -> HistoryConfig:
        return HistoryConfig(
            start=self.start,
            end=self.end,
            max_values=min(MAX_HISTORY_MAX_VALUES, max(1, int(self.max_values))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": iso_utc(self.start),
            "end": iso_utc(self.end),
            "max_values": self.max_values,
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
