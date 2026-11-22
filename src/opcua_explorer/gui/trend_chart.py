"""Trend chart: one polyline over time or row order.

The widget owns no data source and no subscription: it paints a
:class:`~opcua_explorer.trend.TrendSeries` that the caller builds — the History
tab from the values one history read returned, the Live monitor from the
notifications its subscription delivered. Values are drawn in the order they
reached the caller; a point whose StatusCode is not Good is marked, and nothing
is smoothed, interpolated or extrapolated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..trend import TrendSeries, is_bad_status
from .theme import ACCENT, ERROR, mono_font

#: Space the chart reserves for its axis labels.
_LEFT_MARGIN = 62
_RIGHT_MARGIN = 14
_TOP_MARGIN = 10
_BOTTOM_MARGIN = 22
#: Horizontal grid lines (including the top and bottom ones).
_GRID_LINES = 5
#: Above this many points the markers are dropped and only the line is drawn.
_MAX_MARKERS = 120
#: Radius of one marker.
_MARKER_RADIUS = 2.6


class TrendChart(QWidget):
    """Paints one numeric series with axes, grid lines and a hover readout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._series = TrendSeries()
        self._message = "Read history to plot a trend."
        self._title = ""
        self._hover_index: int | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip(
            "Numeric values of the history read. Non-numeric values are not plotted; "
            "hover a point to read its value and timestamp."
        )

    # -- state -------------------------------------------------------------
    @property
    def series(self) -> TrendSeries:
        return self._series

    def set_series(self, series: TrendSeries, *, title: str = "", message: str = "") -> None:
        """Show ``series``; an empty series draws ``message`` instead of a line."""
        self._series = series
        self._title = title
        self._hover_index = None
        if series.is_empty:
            self._message = message or "No numeric values in this history result."
        else:
            self._message = ""
        self.update()

    def clear(self, message: str = "Read history to plot a trend.") -> None:
        self._series = TrendSeries()
        self._title = ""
        self._hover_index = None
        self._message = message
        self.update()

    def sizeHint(self) -> Any:  # noqa: N802 - Qt API
        return QSize(420, 180)

    def minimumSizeHint(self) -> Any:  # noqa: N802 - Qt API
        return QSize(240, 120)

    # -- painting ----------------------------------------------------------
    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        plot = QRectF(
            _LEFT_MARGIN,
            _TOP_MARGIN,
            max(1.0, self.width() - _LEFT_MARGIN - _RIGHT_MARGIN),
            max(1.0, self.height() - _TOP_MARGIN - _BOTTOM_MARGIN),
        )
        if self._series.is_empty:
            painter.setPen(QColor("#6b7a8c"))
            painter.drawText(
                self.rect().adjusted(_LEFT_MARGIN, 0, -_RIGHT_MARGIN, 0),
                Qt.AlignCenter | Qt.TextWordWrap,
                self._message,
            )
            painter.end()
            return

        low, high = self._value_range()
        self._paint_grid(painter, plot, low, high)
        self._paint_axis_labels(painter, plot, low, high)
        self._paint_line(painter, plot, low, high)
        self._paint_hover(painter, plot, low, high)
        painter.end()

    def _value_range(self) -> tuple[float, float]:
        low, high = self._series.min_value, self._series.max_value
        if low == high:
            # A constant series still needs a band to draw in.
            padding = abs(low) * 0.1 or 1.0
            return low - padding, high + padding
        padding = (high - low) * 0.08
        return low - padding, high + padding

    def _x_positions(self, plot: QRectF) -> list[float]:
        points = self._series.points
        if len(points) == 1:
            return [plot.center().x()]
        start = self._series.start
        end = self._series.end
        if self._series.time_based and start is not None and end is not None:
            span = (end - start).total_seconds()
            if span > 0:
                positions: list[float] = []
                for point in points:
                    offset = 0.0 if point.timestamp is None else (point.timestamp - start).total_seconds()
                    positions.append(plot.left() + plot.width() * (offset / span))
                return positions
        step = plot.width() / (len(points) - 1)
        return [plot.left() + index * step for index in range(len(points))]

    def _y_position(self, value: float, plot: QRectF, low: float, high: float) -> float:
        if high == low:
            return plot.center().y()
        return plot.bottom() - plot.height() * ((value - low) / (high - low))

    def _paint_grid(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        pen = QPen(QColor("#e4e9f0"))
        pen.setWidth(1)
        painter.setPen(pen)
        for index in range(_GRID_LINES):
            y = plot.top() + plot.height() * index / (_GRID_LINES - 1)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(QColor("#c8d1dc")))
        painter.drawRect(plot)

    def _paint_axis_labels(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        painter.setFont(mono_font(8))
        painter.setPen(QColor("#51637a"))
        for index in range(_GRID_LINES):
            value = high - (high - low) * index / (_GRID_LINES - 1)
            y = plot.top() + plot.height() * index / (_GRID_LINES - 1)
            painter.drawText(
                QRectF(0, y - 8, _LEFT_MARGIN - 6, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{value:g}",
            )
        first, last = self._x_labels()
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 4, plot.width() / 2, 16),
            Qt.AlignLeft | Qt.AlignVCenter,
            first,
        )
        painter.drawText(
            QRectF(plot.left() + plot.width() / 2, plot.bottom() + 4, plot.width() / 2, 16),
            Qt.AlignRight | Qt.AlignVCenter,
            last,
        )

    def _x_labels(self) -> tuple[str, str]:
        points = self._series.points
        if self._series.time_based:
            start, end = self._series.start, self._series.end
            if start is not None and end is not None:
                return _format_time(start), _format_time(end)
        return f"row {points[0].row}", f"row {points[-1].row}"

    def _paint_line(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        points = self._series.points
        xs = self._x_positions(plot)
        path = QPainterPath()
        for index, (point, x) in enumerate(zip(points, xs, strict=True)):
            y = self._y_position(point.value, plot, low, high)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        pen = QPen(QColor(ACCENT))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawPath(path)

        if len(points) > _MAX_MARKERS:
            return
        for point, x in zip(points, xs, strict=True):
            y = self._y_position(point.value, plot, low, high)
            colour = QColor(ERROR) if is_bad_status(point.status) else QColor(ACCENT)
            painter.setPen(QPen(colour))
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(x, y), _MARKER_RADIUS, _MARKER_RADIUS)
        painter.setBrush(Qt.NoBrush)

    def _paint_hover(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        if self._hover_index is None:
            return
        points = self._series.points
        if not 0 <= self._hover_index < len(points):
            return
        point = points[self._hover_index]
        x = self._x_positions(plot)[self._hover_index]
        y = self._y_position(point.value, plot, low, high)

        pen = QPen(QColor("#8fa2b8"))
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

        painter.setPen(QPen(QColor("#1b2533")))
        painter.setBrush(QColor(ACCENT))
        painter.drawEllipse(QPointF(x, y), 4.0, 4.0)
        painter.setBrush(Qt.NoBrush)

        painter.setFont(mono_font(8))
        text = f"{point.value:g}"
        if point.timestamp is not None:
            text = f"{_format_time(point.timestamp)}  {text}"
        if is_bad_status(point.status):
            text = f"{text}  {point.status}"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 12
        left = min(max(plot.left(), x - width / 2), plot.right() - width)
        box = QRectF(left, plot.top() + 4, width, metrics.height() + 6)
        painter.fillRect(box, QColor(255, 255, 255, 235))
        painter.setPen(QPen(QColor("#c8d1dc")))
        painter.drawRect(box)
        painter.setPen(QColor("#1b2533"))
        painter.drawText(box, Qt.AlignCenter, text)

    # -- interaction -------------------------------------------------------
    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self._series.is_empty:
            self._hover_index = None
            self.update()
            return
        plot = QRectF(
            _LEFT_MARGIN,
            _TOP_MARGIN,
            max(1.0, self.width() - _LEFT_MARGIN - _RIGHT_MARGIN),
            max(1.0, self.height() - _TOP_MARGIN - _BOTTOM_MARGIN),
        )
        if not plot.adjusted(-6, -6, 6, 6).contains(event.position()):
            self._hover_index = None
            self.update()
            return
        xs = self._x_positions(plot)
        nearest = min(range(len(xs)), key=lambda index: abs(xs[index] - event.position().x()))
        if nearest != self._hover_index:
            self._hover_index = nearest
            self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self._hover_index is not None:
            self._hover_index = None
            self.update()


def _format_time(value: datetime) -> str:
    """Axis label for one timestamp: date once, then seconds within the day."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


__all__ = ["TrendChart"]
