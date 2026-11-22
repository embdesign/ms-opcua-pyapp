"""Numeric trend extraction for a HistoryRead result (pure, Qt- and asyncua-free).

The History tab shows a table and a chart of the same values. This module turns a
list of :class:`~opcua_explorer.models.ValueSnapshot` into the numeric series the
chart draws, and records what it had to skip, so the picture can never claim more
than the data says:

* a value that is not a single number (a structure, an array, a string that does
  not parse, a bad StatusCode) is skipped and counted, never coerced to zero;
* a boolean becomes ``1.0``/``0.0`` so a running/faulted flag still has a shape;
* when any point lacks a timestamp, the x axis falls back to the row number
  instead of inventing times (``time_based`` says which one the chart used).

Nothing here reads, writes, subscribes or connects; the server's timestamps and
order are taken as they arrived.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .models import ValueSnapshot, iso_utc

#: StatusCode prefix that marks a value the server could not deliver.
_BAD_STATUS_PREFIX = ("Bad", "Uncertain")


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """One plottable value: its row in the table, its timestamp and its number."""

    row: int
    value: float
    timestamp: datetime | None
    status: str = "Good"

    def to_dict(self) -> dict[str, object]:
        return {
            "row": self.row,
            "value": self.value,
            "timestamp": iso_utc(self.timestamp),
            "status": self.status,
        }


@dataclass(slots=True)
class TrendSeries:
    """The numeric part of one history result, ready to draw.

    ``points`` is in the order the history was shown (oldest first); ``skipped``
    counts values that were not a single number, ``bad_status`` counts non-Good
    StatusCodes among the points that were drawn. ``time_based`` is ``True`` when
    every point carries a timestamp, so the x axis is time rather than row index.
    """

    points: list[TrendPoint] = field(default_factory=list)
    skipped: int = 0
    bad_status: int = 0
    time_based: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.points

    @property
    def min_value(self) -> float:
        return min((point.value for point in self.points), default=0.0)

    @property
    def max_value(self) -> float:
        return max((point.value for point in self.points), default=0.0)

    @property
    def start(self) -> datetime | None:
        stamps = [point.timestamp for point in self.points if point.timestamp is not None]
        return min(stamps) if stamps else None

    @property
    def end(self) -> datetime | None:
        stamps = [point.timestamp for point in self.points if point.timestamp is not None]
        return max(stamps) if stamps else None

    def summary_text(self) -> str:
        """One operator-facing line: what the chart draws and what it left out."""
        if self.is_empty:
            return "No numeric values to plot."
        axis = "time" if self.time_based else "row order"
        parts = [
            f"Trend of {len(self.points)} numeric value(s) by {axis}",
            f"min {self.min_value:g}, max {self.max_value:g}",
        ]
        if self.skipped:
            parts.append(f"{self.skipped} non-numeric value(s) not plotted")
        if self.bad_status:
            parts.append(f"{self.bad_status} with a non-Good StatusCode")
        return "; ".join(parts) + "."


def _as_number(value: object) -> float | None:
    """A single finite number from one OPC UA value; ``None`` when it is not one."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if number == number and abs(number) != float("inf") else None
    return None


def _parse_timestamp(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def is_bad_status(status: str) -> bool:
    """True when a StatusCode name means the server did not deliver a good value."""
    return bool(status) and status.startswith(_BAD_STATUS_PREFIX)


#: Block characters a text trend is drawn with, lowest to highest.
SPARK_CHARS = "▁▂▃▄▅▆▇█"
#: Points a text trend keeps; longer series are bucketed so a line still fits.
SPARK_WIDTH = 100


def _bucket_means(values: list[float], width: int) -> list[float]:
    """Average each of ``width`` equal buckets, so the shape survives a long series."""
    means: list[float] = []
    for index in range(width):
        start = index * len(values) // width
        end = max(start + 1, (index + 1) * len(values) // width)
        bucket = values[start:end]
        means.append(sum(bucket) / len(bucket))
    return means


def sparkline(series: TrendSeries, *, width: int = SPARK_WIDTH) -> str:
    """One line of block characters for a numeric series (empty when there is none).

    This is a text rendering, not a second analysis: it plots the same points the
    chart does. A series longer than ``width`` is bucketed by averaging, so the
    text trend shows the shape while the table and the chart keep every value.
    """
    if series.is_empty:
        return ""
    values = [point.value for point in series.points]
    if width > 1 and len(values) > width:
        values = _bucket_means(values, width)
    low, high = min(values), max(values)
    if high == low:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(values)
    steps = len(SPARK_CHARS) - 1
    return "".join(
        SPARK_CHARS[min(steps, max(0, round((value - low) / (high - low) * steps)))] for value in values
    )


def build_trend_series(snapshots: Iterable[ValueSnapshot]) -> TrendSeries:
    """Extract the plottable numbers of a history result, oldest row first."""
    series = TrendSeries()
    for row, snapshot in enumerate(snapshots, start=1):
        number = _as_number(snapshot.value)
        if number is None:
            series.skipped += 1
            continue
        status = str(snapshot.status or "Good")
        timestamp = _parse_timestamp(snapshot.source_timestamp) or _parse_timestamp(
            snapshot.server_timestamp
        )
        series.points.append(
            TrendPoint(row=row, value=number, timestamp=timestamp, status=status)
        )
        if is_bad_status(status):
            series.bad_status += 1
    series.time_based = bool(series.points) and all(
        point.timestamp is not None for point in series.points
    )
    return series


__all__ = [
    "SPARK_CHARS",
    "SPARK_WIDTH",
    "TrendPoint",
    "TrendSeries",
    "build_trend_series",
    "is_bad_status",
    "sparkline",
]
