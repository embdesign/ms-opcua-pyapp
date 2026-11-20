"""Server status tab: read-only ServerStatus, ServerDiagnostics and the session view.

One batched Read per refresh. Nothing is written, no method is called and no
subscription is created; the monitored-item rows are this client's own view of
its live monitor, not a server-side ``GetMonitoredItems`` answer.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...formatting import format_duration
from ...models import ServerStatusSnapshot
from ...opcua.client_service import OpcUaService
from ...opcua.server_diagnostics import SERVER_COUNTER_NODES, SERVER_LIMIT_NODES
from ..async_runner import AsyncRunner
from ..theme import ERROR, mono_font
from .base import SessionTab

_logger = logging.getLogger(__name__)

_NOT_PROVIDED = "not provided"


class ServerTab(SessionTab):
    """Read-only view of the server's own status and diagnostics counters."""

    def __init__(self, runner: AsyncRunner, service: OpcUaService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        #: GUI-side connection state pushed by ``set_connected``.
        self._connected = False
        self._endpoint_url = ""
        self._busy = False
        self._closed = False
        self._snapshot: ServerStatusSnapshot | None = None

        self._build_ui()
        self.set_connected(False)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Server status & diagnostics")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Read-only: one batched Read of the standard ServerStatus and ServerDiagnostics variables plus the "
            "ServerCapabilities limits this client depends on. Nothing is written and no method is called. The "
            "monitored-item rows are this client's own view of its live-monitor subscription, not the server's "
            "GetMonitoredItems answer."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("primary")
        self.refresh_btn.clicked.connect(self._on_refresh)
        self.last_label = QLabel("Not read yet.")
        self.last_label.setObjectName("hintLabel")
        controls.addWidget(self.refresh_btn)
        controls.addSpacing(12)
        controls.addWidget(self.last_label, 1)
        layout.addLayout(controls)

        layout.addWidget(QLabel("Status, diagnostics summary and capability limits"))
        self.values_table = QTableWidget(0, 4)
        self.values_table.setHorizontalHeaderLabels(["Group", "Item", "Value", "Status"])
        self.values_table.verticalHeader().setVisible(False)
        self.values_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.values_table.setAlternatingRowColors(True)
        self.values_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.values_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.values_table, 1)

        self.monitor_label = QLabel("This session's live monitor: no subscription.")
        self.monitor_label.setObjectName("hintLabel")
        layout.addWidget(self.monitor_label)
        self.monitor_table = QTableWidget(0, 4)
        self.monitor_table.setHorizontalHeaderLabels(["NodeId", "Server handle", "Status", "Sampling (ms)"])
        self.monitor_table.verticalHeader().setVisible(False)
        self.monitor_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.monitor_table.setAlternatingRowColors(True)
        self.monitor_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.monitor_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.monitor_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.monitor_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.monitor_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.monitor_table.setMaximumHeight(150)
        layout.addWidget(self.monitor_table)

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
            self._snapshot = None
            self._busy = False
            self.values_table.setRowCount(0)
            self.monitor_table.setRowCount(0)
            self.monitor_label.setText("This session's live monitor: no subscription.")
            self.last_label.setText("Not read yet.")
            self._set_status("Not connected. Use the Connection tab to open a session.")
        self._update_controls()
        if connected and not self._closed:
            self._on_refresh()

    # -- refresh -----------------------------------------------------------
    def _on_refresh(self) -> None:
        if self._busy or not self._connected:
            return
        self._busy = True
        self._update_controls()
        self._set_status("Reading ServerStatus and ServerDiagnostics…")

        def done(snapshot: ServerStatusSnapshot) -> None:
            self._busy = False
            if self._closed:
                return
            self._snapshot = snapshot
            self._fill(snapshot)
            self._update_controls()

        def failed(message: str) -> None:
            self._busy = False
            if self._closed:
                return
            _logger.warning("Reading server status failed: %s", message)
            self._set_status(f"Reading server status failed: {message}", error=True)
            self._update_controls()

        self._runner.submit(self._service.server_status, on_done=done, on_error=failed)

    def _fill(self, snapshot: ServerStatusSnapshot) -> None:
        rows: list[tuple[str, str, str, str, bool]] = []
        status_values: list[tuple[str, str, str | None]] = [
            ("State", "State", snapshot.state),
            ("Time", "Current time", snapshot.current_time),
            ("Time", "Start time", snapshot.start_time),
            ("Time", "Uptime", format_duration(snapshot.uptime_seconds)),
            ("Time", "Seconds till shutdown", _text(snapshot.seconds_till_shutdown)),
            ("Time", "Shutdown reason", snapshot.shutdown_reason),
            ("Diagnostics", "Enabled flag", _bool_text(snapshot.diagnostics_enabled)),
        ]
        for group, name, value in status_values:
            rows.append(self._row(group, name, value, snapshot))

        for name in SERVER_COUNTER_NODES:
            rows.append(self._row("Counter", name, _text(snapshot.counters.get(name)), snapshot))
        for name in SERVER_LIMIT_NODES:
            rows.append(self._row("Limit", name, _text(snapshot.limits.get(name)), snapshot))

        table = self.values_table
        table.setRowCount(len(rows))
        for index, (group, name, value, status, error) in enumerate(rows):
            for column, text in enumerate((group, name, value, status)):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 2:
                    item.setFont(mono_font(9))
                if column == 3 and error:
                    item.setForeground(QColor(ERROR))
                table.setItem(index, column, item)

        self._fill_monitor(snapshot)
        self.last_label.setText(f"Read at {snapshot.received_at.astimezone().strftime('%H:%M:%S')}.")
        self._set_status(snapshot.summary_text())

    @staticmethod
    def _row(
        group: str, name: str, value: str | None, snapshot: ServerStatusSnapshot
    ) -> tuple[str, str, str, str, bool]:
        if name in snapshot.unavailable:
            return group, name, "—", snapshot.unavailable[name], True
        if value is None or name in snapshot.not_provided:
            return group, name, "—", _NOT_PROVIDED, False
        return group, name, value, "Good", False

    def _fill_monitor(self, snapshot: ServerStatusSnapshot) -> None:
        items = snapshot.monitored_items
        if snapshot.has_monitor:
            self.monitor_label.setText(
                f"This session's live monitor: subscription {snapshot.monitor_subscription_id}, "
                f"{len(items)} monitored item(s) (client view)."
            )
        else:
            self.monitor_label.setText("This session's live monitor: no subscription.")

        table = self.monitor_table
        table.setRowCount(len(items))
        for row, info in enumerate(items):
            values = [
                info.node_id,
                "" if info.client_handle is None else str(info.client_handle),
                info.status,
                f"{info.sampling_interval_ms:.0f}",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setFont(mono_font(9))
                if column == 2 and not info.ok:
                    item.setForeground(QColor(ERROR))
                table.setItem(row, column, item)

    # -- controls ----------------------------------------------------------
    def _update_controls(self) -> None:
        connected = self._connected
        self.refresh_btn.setEnabled(connected and not self._busy)



def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _bool_text(value: bool | None) -> str | None:
    if value is None:
        return None
    return "true" if value else "false"
