"""Explicit Value-read options and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import DEFAULT_READ_MAX_AGE_MS, MAX_READ_MAX_AGE_MS
from .common import iso_utc, utc_now
from .graph import ValueSnapshot

#: ``TimestampsToReturn`` names this client offers; the server may still return
#: fewer timestamps than asked for.
TIMESTAMPS_TO_RETURN = ("Source", "Server", "Both", "Neither")


@dataclass(slots=True)
class ValueReadOptions:
    """Options of one explicit Value read: a slice, a cache age and timestamps.

    ``index_range`` is a NumericRange string (``"1:3"``, ``"0,2"``) applied to the
    Value attribute; empty means the whole value. ``max_age_ms`` is the protocol
    ``MaxAge``: ``0`` lets the server answer from a cache, the maximum asks for a
    value from the device. ``timestamps`` selects ``TimestampsToReturn``.
    """

    index_range: str = ""
    max_age_ms: int = DEFAULT_READ_MAX_AGE_MS
    timestamps: str = "Both"

    def normalised(self) -> ValueReadOptions:
        name = self.timestamps if self.timestamps in TIMESTAMPS_TO_RETURN else "Both"
        return ValueReadOptions(
            index_range=self.index_range.strip(),
            max_age_ms=max(0, min(int(self.max_age_ms), MAX_READ_MAX_AGE_MS)),
            timestamps=name,
        )

    def label(self) -> str:
        parts = [f"timestamps {self.timestamps}", f"MaxAge {self.max_age_ms} ms"]
        if self.index_range:
            parts.insert(0, f"IndexRange {self.index_range}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_range": self.index_range,
            "max_age_ms": self.max_age_ms,
            "timestamps": self.timestamps,
        }


@dataclass(slots=True)
class ValueReadResult:
    """One explicit Value read with its options and the decoded snapshot."""

    node_id: str
    options: ValueReadOptions
    snapshot: ValueSnapshot
    duration_ms: float = 0.0
    detail: str = ""
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        head = f"Value of {self.node_id} [{self.snapshot.status}]"
        return f"{head} — {self.detail}" if self.detail else head

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "options": self.options.to_dict(),
            "value": self.snapshot.to_dict(),
            "duration_ms": round(self.duration_ms, 3),
            "detail": self.detail,
            "received_at": iso_utc(self.received_at),
        }
