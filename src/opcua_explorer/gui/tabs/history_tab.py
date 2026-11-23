"""History tab: HistoryRead of one variable and one confirmed HistoryUpdate.

Reads: one node, one range, one bounded read (raw or one aggregate); an aggregate
requires both From and To and the server may not support it. Updates: insert,
replace or delete stored values on the same node, after a preview, an explicit
confirmation and one send, followed by a read-back of the affected range. The
values are shown as a table and - when they are single numbers - as a trend chart
that plots what the server returned and nothing else.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...defaults import (
    DEFAULT_HISTORY_MAX_VALUES,
    MAX_HISTORY_MAX_VALUES,
    MAX_HISTORY_UPDATE_ENTRIES,
)
from ...errors import InvalidNodeIdError
from ...formatting import flatten_for_csv
from ...models import (
    HISTORY_AGGREGATES,
    HISTORY_UPDATE_ACTIONS,
    HistoryConfig,
    HistoryResult,
    HistoryUpdateConfig,
    HistoryUpdateEntry,
    HistoryUpdateOutcome,
)
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...operator_text import history_update_confirmation
from ...trend import build_trend_series
from ..async_runner import AsyncRunner
from ..selection_marker import SelectionMarker
from ..tab_notice import OptionsRow, TabNotice
from ..theme import ERROR, mono_font
from ..trend_chart import TrendChart
from .base import SessionTab

_logger = logging.getLogger(__name__)

HISTORY_PAYLOAD = Qt.UserRole + 21
_MAX_VALUE_CELL_CHARS = 300
_UPDATE_ACTION_LABELS = {"insert": "Insert", "replace": "Replace", "delete": "Delete range"}


def ask_confirmation(parent: QWidget, title: str, text: str) -> bool:
    """Modal yes/no with the exact operation text; a function so tests can stub it."""
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


class HistoryTab(SessionTab):
    """History of one variable, bounded by a time range and a value cap."""

    def __init__(
        self,
        runner: AsyncRunner,
        service: OpcUaService,
        *,
        selection_provider: Callable[[], str | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        #: GUI-side connection state pushed by ``set_connected``.
        self._connected = False
        self._endpoint_url = ""
        self._selection_provider = selection_provider
        self._busy = False
        self._closed = False

        self._build_ui()
        self.set_connected(False)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("History")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addWidget(
            TabNotice(
                "HistoryRead of one variable (raw or one aggregate), plus one confirmed HistoryUpdate.",
                "HistoryRead for one variable: an optional time range and a maximum number of values, "
                "optionally one aggregate (average/minimum/maximum/count) over that range. The server decides "
                "whether a node has history and whether it computes aggregates; an empty result is not an "
                "error. Single numeric values are also drawn as a trend: nothing is interpolated, and "
                "non-numeric values are counted instead of plotted. 'Update history' is one confirmed "
                "HistoryUpdate (insert, replace or delete stored values) on the same node; it changes the "
                "server's historian, not the live value, and the affected range is read back afterwards.",
            )
        )

        node_row = QHBoxLayout()
        self.node_edit = QLineEdit()
        self.node_edit.setPlaceholderText("ns=2;s=Temperature")
        self.node_edit.returnPressed.connect(self._on_read)
        self.use_selection_btn = QPushButton("Use browser selection")
        self.use_selection_btn.clicked.connect(self._on_use_selection)
        # Speaks up only when this page's target differs from the browser selection.
        self.selection_marker = SelectionMarker(self.node_edit, self._selection_provider)
        node_row.addWidget(QLabel("Node:"))
        node_row.addWidget(self.node_edit, 2)
        node_row.addWidget(self.use_selection_btn)
        node_row.addWidget(self.selection_marker)
        layout.addLayout(node_row)

        range_row = QHBoxLayout()
        self.from_check = QCheckBox("From")
        self.from_check.toggled.connect(self._update_controls)
        self.from_edit = QDateTimeEdit()
        self.from_edit.setCalendarPopup(True)
        self.from_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.from_edit.setDateTime(QDateTime.currentDateTime())
        self.to_check = QCheckBox("To")
        self.to_check.toggled.connect(self._update_controls)
        self.to_edit = QDateTimeEdit()
        self.to_edit.setCalendarPopup(True)
        self.to_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.to_edit.setDateTime(QDateTime.currentDateTime())
        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, MAX_HISTORY_MAX_VALUES)
        self.max_spin.setSingleStep(100)
        self.max_spin.setValue(DEFAULT_HISTORY_MAX_VALUES)
        self.max_spin.setToolTip("Hard cap: also sent as NumValuesPerNode.")
        self.max_spin.valueChanged.connect(self._update_controls)
        self.read_btn = QPushButton("Read history")
        self.read_btn.setObjectName("primary")
        self.read_btn.clicked.connect(self._on_read)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        # Secondary read settings stay out of the working area; the toggle names
        # whatever is in effect, so a collapsed row can never be forgotten.
        self.options = OptionsRow("Options")
        self.update_options = OptionsRow("Update history")

        range_row.addWidget(self.from_check)
        range_row.addWidget(self.from_edit)
        range_row.addSpacing(12)
        range_row.addWidget(self.to_check)
        range_row.addWidget(self.to_edit)
        range_row.addSpacing(12)
        range_row.addWidget(self.options.toggle)
        range_row.addSpacing(12)
        range_row.addWidget(self.update_options.toggle)
        range_row.addSpacing(12)
        range_row.addWidget(self.read_btn)
        range_row.addWidget(self.clear_btn)
        range_row.addStretch(1)
        layout.addLayout(range_row)

        self.aggregate_combo = QComboBox()
        self.aggregate_combo.addItem("Raw", "")
        for name in HISTORY_AGGREGATES:
            self.aggregate_combo.addItem(name.capitalize(), name)
        self.aggregate_combo.setToolTip(
            "Aggregate read; requires both From and To. The server may not provide aggregate history."
        )
        self.aggregate_combo.currentIndexChanged.connect(self._update_controls)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.0, 86_400_000.0)
        self.interval_spin.setDecimals(0)
        self.interval_spin.setSingleStep(1000.0)
        self.interval_spin.setValue(0.0)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setToolTip(
            "Processing interval for an aggregate read; 0 asks for one value over the whole range."
        )
        self.interval_spin.valueChanged.connect(self._update_controls)
        self.options.add_widget(QLabel("Aggregate:"))
        self.options.add_widget(self.aggregate_combo)
        self.options.add_widget(QLabel("Interval:"))
        self.options.add_widget(self.interval_spin)
        self.options.add_spacing(12)
        self.options.add_widget(QLabel("Max values:"))
        self.options.add_widget(self.max_spin)
        self.options.add_stretch(1)
        layout.addWidget(self.options.widget)

        self.update_action_combo = QComboBox()
        for action in HISTORY_UPDATE_ACTIONS:
            self.update_action_combo.addItem(_UPDATE_ACTION_LABELS[action], action)
        self.update_action_combo.setToolTip(
            "Insert or replace stored values, or delete the stored range from/to. This changes the server's "
            "historian, not the live value."
        )
        self.update_action_combo.currentIndexChanged.connect(self._update_controls)
        self.update_entries_edit = QPlainTextEdit()
        self.update_entries_edit.setPlaceholderText(
            "One value per line: 2026-11-21T10:00:00Z=21.5"
        )
        self.update_entries_edit.setFixedHeight(54)
        self.update_entries_edit.setFont(mono_font(9))
        self.history_write_btn = QPushButton("Write history…")
        self.history_write_btn.setToolTip(
            "Validate the values, ask for confirmation, send one HistoryUpdate and read the range back."
        )
        self.history_write_btn.clicked.connect(self._on_history_write)
        self.update_hint_label = QLabel("Delete range uses From/To.")
        self.update_hint_label.setObjectName("hintLabel")
        self.update_options.add_widget(QLabel("Action:"))
        self.update_options.add_widget(self.update_action_combo)
        self.update_options.add_widget(self.update_entries_edit, 2)
        self.update_options.add_widget(self.history_write_btn)
        self.update_options.add_widget(self.update_hint_label, 1)
        layout.addWidget(self.update_options.widget)

        self.values_table = QTableWidget(0, 5)
        self.values_table.setHorizontalHeaderLabels(
            ["#", "Source timestamp", "Server timestamp", "Status", "Value"]
        )
        self.values_table.verticalHeader().setVisible(False)
        self.values_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.values_table.setAlternatingRowColors(True)
        self.values_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.values_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)

        self.trend_chart = TrendChart()
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.trend_chart)
        splitter.addWidget(self.values_table)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([200, 280])
        layout.addWidget(splitter, 1)

        self.summary_label = QLabel("No history read yet.")
        self.summary_label.setObjectName("hintLabel")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.status_label = QLabel("Not connected.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # -- connection state --------------------------------------------------
    def set_connected(self, connected: bool, connection: Any = None) -> None:
        self._connected = bool(connected)
        if connection is not None:
            self._endpoint_url = str(getattr(connection, "endpoint_url", "") or "")
        if not connected:
            self._busy = False
            self.values_table.setRowCount(0)
            self.trend_chart.clear("Not connected. Read history to plot a trend.")
            self.summary_label.setText("No history read yet.")
            self._set_status("Not connected. Use the Connection tab to open a session.")
        elif not self._busy:
            self._set_status("Connected. Enter a NodeId or take the browser selection, then read history.")
        self._update_controls()

    # -- configuration -----------------------------------------------------
    def _on_use_selection(self) -> None:
        node_id = self._selection_provider() if self._selection_provider is not None else None
        if not node_id:
            self._set_status("No node is selected in the address space browser.", error=True)
            return
        self.node_edit.setText(str(node_id))
        self._set_status(f"History node set to {node_id}.")

    def _config(self) -> HistoryConfig:
        start = self.from_edit.dateTime().toUTC().toPython() if self.from_check.isChecked() else None
        end = self.to_edit.dateTime().toUTC().toPython() if self.to_check.isChecked() else None
        return HistoryConfig(
            start=start if isinstance(start, datetime) else None,
            end=end if isinstance(end, datetime) else None,
            max_values=int(self.max_spin.value()),
            aggregate=self.aggregate_combo.currentData() or None,
            processing_interval_ms=float(self.interval_spin.value()),
        ).normalised()

    def _parse_update_entries(self) -> list[HistoryUpdateEntry]:
        """Parse ``ISO=VALUE`` lines; a malformed line means nothing is sent."""
        entries: list[HistoryUpdateEntry] = []
        for number, line in enumerate(self.update_entries_edit.toPlainText().splitlines(), start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            stamp_text, separator, value_text = text.partition("=")
            if not separator:
                raise ValueError(f"Line {number} is not ISO=VALUE: {text!r}.")
            cleaned = stamp_text.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            try:
                moment = datetime.fromisoformat(cleaned)
            except ValueError as exc:
                raise ValueError(f"Line {number} has no ISO-8601 timestamp: {stamp_text!r}.") from exc
            entries.append(
                HistoryUpdateEntry(
                    source_timestamp=moment if moment.tzinfo else moment.replace(tzinfo=UTC),
                    value_text=value_text.strip(),
                )
            )
        if len(entries) > MAX_HISTORY_UPDATE_ENTRIES:
            raise ValueError(
                f"At most {MAX_HISTORY_UPDATE_ENTRIES} values can be sent in one HistoryUpdate."
            )
        return entries

    def _history_update_config(self) -> HistoryUpdateConfig:
        action = str(self.update_action_combo.currentData() or "insert")
        start = self.from_edit.dateTime().toUTC().toPython() if self.from_check.isChecked() else None
        end = self.to_edit.dateTime().toUTC().toPython() if self.to_check.isChecked() else None
        entries = [] if action == "delete" else self._parse_update_entries()
        if action == "delete" and (start is None or end is None):
            raise ValueError("Set both From and To for a history delete.")
        if action != "delete" and not entries:
            raise ValueError("Enter at least one ISO=VALUE line to insert or replace stored values.")
        return HistoryUpdateConfig(
            action=action,
            node_id=self.node_edit.text().strip(),
            entries=entries,
            start=start if isinstance(start, datetime) else None,
            end=end if isinstance(end, datetime) else None,
        ).normalised()

    def _on_history_write(self) -> None:
        """Preview, confirm, send once and read the affected range back."""
        if self._busy or not self._connected:
            return
        try:
            canonical = parse_node_id(self.node_edit.text().strip()).to_string()
            config = self._history_update_config()
        except (InvalidNodeIdError, ValueError) as exc:
            self._set_status(str(exc), error=True)
            return
        config = HistoryUpdateConfig(
            action=config.action,
            node_id=canonical,
            entries=config.entries,
            start=config.start,
            end=config.end,
        )
        self._busy = True
        self._update_controls()
        self._set_status(f"Checking the history {config.action} of {canonical}…")

        def preview_done(inspection: Any) -> None:
            self._busy = False
            if self._closed:
                return
            if not inspection.ok:
                self._set_status(f"The history update was not sent: {inspection.detail}", error=True)
                self._update_controls()
                return
            text = history_update_confirmation(
                endpoint_url=self._endpoint_url,
                action=config.action,
                node_id=canonical,
                count=len(config.entries),
                entries_preview=config.describe_entries(),
                start=config.start.isoformat() if config.start else "",
                end=config.end.isoformat() if config.end else "",
            )
            if not ask_confirmation(self, "Confirm history update", text):
                self._set_status("The history update was cancelled; nothing was sent.")
                self._update_controls()
                return
            self._busy = True
            self._update_controls()
            self._set_status(f"Sending one history {config.action} to {canonical}…")
            self._runner.submit(
                lambda: self._service.write_history(config, reference="history-tab update"),
                on_done=self._on_history_update_done,
                on_error=lambda message: self._on_history_update_failed(message),
            )

        self._runner.submit(
            lambda: self._service.history_update_preview(config),
            on_done=preview_done,
            on_error=lambda message: self._on_history_update_failed(message),
        )

    def _on_history_update_done(self, outcome: HistoryUpdateOutcome) -> None:
        self._busy = False
        if self._closed:
            return
        if outcome.verification is not None:
            self._fill(outcome.verification)
            self.summary_label.setText(
                f"{outcome.summary_text()} {outcome.verification.summary_text()}"
            )
        self._set_status(outcome.summary_text(), error=not outcome.ok)
        self._update_controls()

    def _on_history_update_failed(self, message: str) -> None:
        self._busy = False
        if self._closed:
            return
        _logger.warning("The history update reported: %s", message)
        self._set_status(f"The history update failed: {message}", error=True)
        self._update_controls()

    # -- read --------------------------------------------------------------
    def _on_read(self) -> None:
        if self._busy or not self._connected:
            return
        text = self.node_edit.text().strip()
        if not text:
            self._set_status("Enter a NodeId such as ns=2;s=Temperature.", error=True)
            return
        try:
            canonical = parse_node_id(text).to_string()
        except InvalidNodeIdError as exc:
            self._set_status(str(exc), error=True)
            return

        config = self._config()
        if config.aggregate and (config.start is None or config.end is None):
            self._set_status("Set both From and To for an aggregate read.", error=True)
            return
        self._busy = True
        self._update_controls()
        self._set_status(f"Reading {config.aggregate or 'raw'} history of {canonical}…")

        def done(result: HistoryResult) -> None:
            self._busy = False
            if self._closed:
                return
            self._fill(result)
            self._update_controls()

        def failed(message: str) -> None:
            self._busy = False
            if self._closed:
                return
            _logger.warning("Reading history of %s failed: %s", canonical, message)
            self._set_status(f"Reading history failed: {message}", error=True)
            self._update_controls()

        self._runner.submit(
            lambda: self._service.read_history(canonical, config),
            on_done=done,
            on_error=failed,
        )

    def _fill(self, result: HistoryResult) -> None:
        table = self.values_table
        table.setRowCount(len(result.values))
        for row, snapshot in enumerate(result.values):
            value_text = flatten_for_csv(snapshot.value)
            if len(value_text) > _MAX_VALUE_CELL_CHARS:
                value_text = value_text[: _MAX_VALUE_CELL_CHARS - 1] + "…"
            values = [
                str(row + 1),
                snapshot.source_timestamp or "",
                snapshot.server_timestamp or "",
                snapshot.status,
                value_text,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setData(HISTORY_PAYLOAD, snapshot)
                if column == 4:
                    item.setFont(mono_font(9))
                    item.setToolTip(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False, default=str))
                if column == 3 and snapshot.status != "Good":
                    item.setForeground(QColor(ERROR))
                table.setItem(row, column, item)

        self.summary_label.setText(result.summary_text())
        self._plot(result)
        if result.status != "Good":
            self._set_status(f"HistoryRead returned {result.status}.", error=True)
        elif not result.values:
            self._set_status(result.detail or "The server returned no values for this range.")
        else:
            self._set_status(
                f"Read {len(result.values)} value(s) in {result.duration_ms:.0f} ms "
                f"({result.pages} page(s))."
            )

    def _plot(self, result: HistoryResult) -> None:
        """Draw the numeric part of one result; nothing is invented for the rest."""
        series = build_trend_series(result.values)
        if series.is_empty:
            if result.values:
                message = (
                    "This read returned values that are not single numbers, so there is nothing to plot. "
                    "Read a numeric variable (or an aggregate of one) to see a trend."
                )
            else:
                message = result.detail or "The server returned no values for this range."
            self.trend_chart.set_series(series, message=message)
            return
        self.trend_chart.set_series(series, title=result.node_id)
        self.summary_label.setText(f"{result.summary_text()} {series.summary_text()}")

    def _on_clear(self) -> None:
        self.values_table.setRowCount(0)
        self.trend_chart.clear()
        self.summary_label.setText("No history read yet.")
        self._set_status("Cleared the local history table and trend.")

    # -- controls ----------------------------------------------------------
    def _options_summary(self) -> str:
        """Name the non-default read settings, so a collapsed row is never forgotten."""
        config = self._config()
        parts: list[str] = []
        if config.aggregate:
            interval = config.processing_interval_ms
            parts.append(f"{config.aggregate}" if not interval else f"{config.aggregate} / {interval:g} ms")
        if config.max_values != DEFAULT_HISTORY_MAX_VALUES:
            parts.append(f"max {config.max_values}")
        return ", ".join(parts)

    def _update_controls(self) -> None:
        connected = self._connected
        action = str(self.update_action_combo.currentData() or "insert")
        self.options.set_active(self._options_summary())
        self.update_options.set_active(
            "" if action == "insert" else _UPDATE_ACTION_LABELS.get(action, action)
        )
        self.node_edit.setEnabled(connected and not self._busy)
        self.use_selection_btn.setEnabled(connected and not self._busy)
        self.from_edit.setEnabled(self.from_check.isChecked() and connected and not self._busy)
        self.to_edit.setEnabled(self.to_check.isChecked() and connected and not self._busy)
        self.from_check.setEnabled(connected and not self._busy)
        self.to_check.setEnabled(connected and not self._busy)
        self.max_spin.setEnabled(connected and not self._busy)
        self.aggregate_combo.setEnabled(connected and not self._busy)
        self.interval_spin.setEnabled(connected and not self._busy and bool(self.aggregate_combo.currentData()))
        self.read_btn.setEnabled(connected and not self._busy)
        self.clear_btn.setEnabled(not self._busy)
        self.update_action_combo.setEnabled(connected and not self._busy)
        self.update_entries_edit.setEnabled(connected and not self._busy and action != "delete")
        self.history_write_btn.setEnabled(connected and not self._busy)
        self.update_hint_label.setText(
            "Delete range uses From/To."
            if action == "delete"
            else f"At most {MAX_HISTORY_UPDATE_ENTRIES} lines."
        )
