"""History tab: read-only raw HistoryRead for one variable.

One node, one range, one bounded raw-history read. No HistoryUpdate, no
processed/aggregate reads, no event history and no retry.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateTimeEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...defaults import DEFAULT_HISTORY_MAX_VALUES, MAX_HISTORY_MAX_VALUES
from ...errors import InvalidNodeIdError
from ...formatting import flatten_for_csv
from ...models import HistoryConfig, HistoryResult
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ..async_runner import AsyncRunner
from ..theme import ERROR, mono_font
from .base import SessionTab

_logger = logging.getLogger(__name__)

HISTORY_PAYLOAD = Qt.UserRole + 21
_MAX_VALUE_CELL_CHARS = 300


class HistoryTab(SessionTab):
    """Raw history of one variable, bounded by a time range and a value cap."""

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
        subtitle = QLabel(
            "Raw HistoryRead for one variable: an optional time range and a maximum number of values. "
            "Read-only — no HistoryUpdate, no processed/aggregate reads and no event history. The server "
            "decides whether a node has history; an empty result is not an error."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        node_row = QHBoxLayout()
        self.node_edit = QLineEdit()
        self.node_edit.setPlaceholderText("ns=2;s=Temperature")
        self.node_edit.returnPressed.connect(self._on_read)
        self.use_selection_btn = QPushButton("Use browser selection")
        self.use_selection_btn.clicked.connect(self._on_use_selection)
        node_row.addWidget(QLabel("Node:"))
        node_row.addWidget(self.node_edit, 2)
        node_row.addWidget(self.use_selection_btn)
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
        self.read_btn = QPushButton("Read history")
        self.read_btn.setObjectName("primary")
        self.read_btn.clicked.connect(self._on_read)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)

        range_row.addWidget(self.from_check)
        range_row.addWidget(self.from_edit)
        range_row.addSpacing(12)
        range_row.addWidget(self.to_check)
        range_row.addWidget(self.to_edit)
        range_row.addSpacing(12)
        range_row.addWidget(QLabel("Max values:"))
        range_row.addWidget(self.max_spin)
        range_row.addSpacing(12)
        range_row.addWidget(self.read_btn)
        range_row.addWidget(self.clear_btn)
        range_row.addStretch(1)
        layout.addLayout(range_row)

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
        layout.addWidget(self.values_table, 1)

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
        ).normalised()

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
        self._busy = True
        self._update_controls()
        self._set_status(f"Reading raw history of {canonical}…")

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
        if result.status != "Good":
            self._set_status(f"HistoryRead returned {result.status}.", error=True)
        elif not result.values:
            self._set_status(result.detail or "The server returned no values for this range.")
        else:
            self._set_status(
                f"Read {len(result.values)} value(s) in {result.duration_ms:.0f} ms "
                f"({result.pages} page(s))."
            )

    def _on_clear(self) -> None:
        self.values_table.setRowCount(0)
        self.summary_label.setText("No history read yet.")
        self._set_status("Cleared the local history table.")

    # -- controls ----------------------------------------------------------
    def _update_controls(self) -> None:
        connected = self._connected
        self.node_edit.setEnabled(connected and not self._busy)
        self.use_selection_btn.setEnabled(connected and not self._busy)
        self.from_edit.setEnabled(self.from_check.isChecked() and connected and not self._busy)
        self.to_edit.setEnabled(self.to_check.isChecked() and connected and not self._busy)
        self.from_check.setEnabled(connected and not self._busy)
        self.to_check.setEnabled(connected and not self._busy)
        self.max_spin.setEnabled(connected and not self._busy)
        self.read_btn.setEnabled(connected and not self._busy)
        self.clear_btn.setEnabled(not self._busy)
