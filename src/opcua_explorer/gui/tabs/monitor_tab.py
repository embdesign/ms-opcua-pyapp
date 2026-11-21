"""Live monitor tab: data-change subscriptions for explicitly added nodes.

Nothing is subscribed implicitly. The operator adds NodeIds, starts one
subscription, and sees the notifications as they arrive. Two views are kept:

* the monitored-item table shows the **latest value per node in place** (one row
  per node, overwritten on every notification), and
* the notification table is an append-only log with a bounded number of rows,
  because a diagnostics client must not grow without bound while a fast-changing
  variable publishes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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
    DEFAULT_MONITOR_KEEP_ROWS,
    DEFAULT_PUBLISHING_INTERVAL_MS,
    DEFAULT_SAMPLING_INTERVAL_MS,
    MAX_KEEP_ROWS,
    MAX_PUBLISHING_INTERVAL_MS,
    MIN_KEEP_ROWS,
    MIN_PUBLISHING_INTERVAL_MS,
)
from ...errors import InvalidNodeIdError
from ...models import (
    DATA_CHANGE_TRIGGERS,
    DEADBAND_TYPES,
    MonitorConfig,
    MonitoredItemInfo,
    MonitorNotification,
    MonitorStart,
    MonitorStatusEvent,
    iso_utc,
)
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...project import MonitorState
from ..async_runner import AsyncRunner
from ..theme import ERROR, mono_font
from .base import SessionTab

_logger = logging.getLogger(__name__)

ITEM_NODE_ID = Qt.UserRole + 1
ITEM_HANDLE = Qt.UserRole + 2
NOTIFICATION_PAYLOAD = Qt.UserRole + 3

DEFAULT_MAX_ROWS = DEFAULT_MONITOR_KEEP_ROWS
_VALUE_CELL_CHARS = 300
_LATEST_CELL_CHARS = 120


class MonitorTab(SessionTab):
    """One subscription, a monitored-item list and a bounded live notification table."""

    #: Emitted on the GUI thread for every notification delivered to this tab.
    notification_received = Signal(object)
    #: Emitted when the monitored-item list or its status text changed.
    items_changed = Signal()

    def __init__(self, runner: AsyncRunner, service: OpcUaService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        #: GUI-side connection state pushed by ``set_connected``.
        self._connected = False
        self._endpoint_url = ""
        self._running = False
        self._busy = False
        self._paused = False
        self._closed = False
        self._monitor_key: str | None = None
        self._subscription_id: int | None = None
        self._total_notifications = 0
        self._dropped = 0
        self._monitoring_mode = "Reporting"
        #: node_id -> last notification, used by the in-place value columns.
        self._latest: dict[str, MonitorNotification] = {}

        self._build_ui()
        self.set_connected(False)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Live monitor")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "One data-change subscription on nodes you add explicitly. The left table keeps one row per node "
            "and overwrites it with the latest value; the notification list is an append-only log. The server "
            "decides the effective publishing and sampling rate. Nothing is written to the server and nothing is "
            "exported from this tab."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        items_row = QHBoxLayout()
        self.node_edit = QLineEdit()
        self.node_edit.setPlaceholderText("ns=2;s=Temperature")
        self.node_edit.returnPressed.connect(self._on_add)
        self.add_btn = QPushButton("Add node")
        self.add_btn.clicked.connect(self._on_add)
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._on_remove)
        self.clear_items_btn = QPushButton("Clear list")
        self.clear_items_btn.clicked.connect(self._on_clear_items)
        items_row.addWidget(QLabel("Node to monitor:"))
        items_row.addWidget(self.node_edit, 2)
        items_row.addWidget(self.add_btn)
        items_row.addWidget(self.remove_btn)
        items_row.addWidget(self.clear_items_btn)
        layout.addLayout(items_row)

        controls = QHBoxLayout()
        self.publishing_spin = QDoubleSpinBox()
        self.publishing_spin.setRange(MIN_PUBLISHING_INTERVAL_MS, MAX_PUBLISHING_INTERVAL_MS)
        self.publishing_spin.setDecimals(0)
        self.publishing_spin.setSingleStep(100.0)
        self.publishing_spin.setValue(DEFAULT_PUBLISHING_INTERVAL_MS)
        self.publishing_spin.setSuffix(" ms")
        self.publishing_spin.setToolTip("Requested publishing interval of the subscription.")
        self.sampling_spin = QDoubleSpinBox()
        self.sampling_spin.setRange(0.0, MAX_PUBLISHING_INTERVAL_MS)
        self.sampling_spin.setDecimals(0)
        self.sampling_spin.setSingleStep(100.0)
        self.sampling_spin.setValue(DEFAULT_SAMPLING_INTERVAL_MS)
        self.sampling_spin.setSuffix(" ms")
        self.sampling_spin.setToolTip("Requested sampling interval per monitored item; 0 = fastest.")
        self.start_btn = QPushButton("Start monitoring")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self._on_stop)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        self.clear_log_btn = QPushButton("Clear notifications")
        self.clear_log_btn.clicked.connect(self._on_clear_notifications)
        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(MIN_KEEP_ROWS, MAX_KEEP_ROWS)
        self.max_rows_spin.setSingleStep(100)
        self.max_rows_spin.setValue(DEFAULT_MAX_ROWS)
        self.max_rows_spin.setToolTip("Rows kept in the notification table; the oldest rows are dropped first.")
        self.max_rows_spin.valueChanged.connect(self._on_max_rows_changed)

        controls.addWidget(QLabel("Publishing:"))
        controls.addWidget(self.publishing_spin)
        controls.addWidget(QLabel("Sampling:"))
        controls.addWidget(self.sampling_spin)
        controls.addSpacing(12)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.pause_btn)
        controls.addSpacing(12)
        controls.addWidget(self.clear_log_btn)
        controls.addWidget(QLabel("Keep rows:"))
        controls.addWidget(self.max_rows_spin)
        layout.addLayout(controls)

        filter_row = QHBoxLayout()
        self.trigger_combo = QComboBox()
        self.trigger_combo.addItems(list(DATA_CHANGE_TRIGGERS))
        self.trigger_combo.setCurrentText("StatusValue")
        self.trigger_combo.setToolTip(
            "DataChangeTrigger: report on status changes, on status or value changes (default), or also "
            "include source-timestamp changes."
        )
        self.deadband_combo = QComboBox()
        self.deadband_combo.addItems(list(DEADBAND_TYPES))
        self.deadband_combo.setToolTip(
            "Deadband suppresses notifications while the value stays within the band. Absolute uses the "
            "variable's engineering units; Percent needs an EURange property on the server."
        )
        self.deadband_combo.currentTextChanged.connect(self._on_deadband_changed)
        self.deadband_spin = QDoubleSpinBox()
        self.deadband_spin.setRange(0.0, 1_000_000.0)
        self.deadband_spin.setDecimals(3)
        self.deadband_spin.setSingleStep(0.1)
        self.deadband_spin.setValue(0.0)
        self.deadband_spin.setToolTip("Deadband value; ignored when the deadband type is None.")
        self.apply_intervals_btn = QPushButton("Apply intervals")
        self.apply_intervals_btn.setToolTip(
            "Modify the running subscription (ModifySubscription / ModifyMonitoredItems); the server may "
            "revise the requested intervals."
        )
        self.apply_intervals_btn.clicked.connect(self._on_apply_intervals)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Reporting", "Sampling", "Disabled"])
        self.mode_combo.setToolTip(
            "Server-side SetMonitoringMode for every monitored item: Reporting delivers notifications, "
            "Sampling only samples, Disabled stops sampling. Rows and the log are kept."
        )
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)

        filter_row.addWidget(QLabel("Trigger:"))
        filter_row.addWidget(self.trigger_combo)
        filter_row.addWidget(QLabel("Deadband:"))
        filter_row.addWidget(self.deadband_combo)
        filter_row.addWidget(self.deadband_spin)
        filter_row.addWidget(self.apply_intervals_btn)
        filter_row.addSpacing(12)
        filter_row.addWidget(QLabel("Server mode:"))
        filter_row.addWidget(self.mode_combo)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Monitored items — latest value (updated in place)"))
        self.items_table = QTableWidget(0, 6)
        self.items_table.setHorizontalHeaderLabels(
            ["NodeId", "Handle", "Status", "Latest value", "Source timestamp", "Count"]
        )
        self.items_table.verticalHeader().setVisible(False)
        self.items_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.items_table.setAlternatingRowColors(True)
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header = self.items_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.items_table.itemSelectionChanged.connect(self._update_controls)
        self.items_table.itemSelectionChanged.connect(self._on_item_selected)
        self.items_table.setMinimumWidth(460)
        left_layout.addWidget(self.items_table, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Notifications (newest last)"))
        self.notifications_table = QTableWidget(0, 5)
        self.notifications_table.setHorizontalHeaderLabels(
            ["Received", "NodeId", "Value", "Status", "Source timestamp"]
        )
        self.notifications_table.verticalHeader().setVisible(False)
        self.notifications_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.notifications_table.setAlternatingRowColors(True)
        self.notifications_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.notifications_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.notifications_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.notifications_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.notifications_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.notifications_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.notifications_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.notifications_table.itemSelectionChanged.connect(self._on_notification_selected)
        right_layout.addWidget(self.notifications_table, 1)

        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setFont(mono_font(9))
        self.details_view.setMaximumHeight(150)
        self.details_view.setPlaceholderText("Select a notification or a monitored item to see its full payload.")
        right_layout.addWidget(self.details_view)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("Not connected.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.counters_label = QLabel("0 notification(s) · 0 monitored item(s)")
        self.counters_label.setObjectName("hintLabel")
        layout.addWidget(self.counters_label)

    # -- connection state --------------------------------------------------
    def set_connected(self, connected: bool, connection: Any = None) -> None:
        self._connected = bool(connected)
        if connection is not None:
            self._endpoint_url = str(getattr(connection, "endpoint_url", "") or "")
        if not connected:
            self._detach_listener()
            self._running = False
            self._busy = False
            self._subscription_id = None
            self._paused = False
            self.pause_btn.setChecked(False)
            self._monitoring_mode = "Reporting"
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentText("Reporting")
            self.mode_combo.blockSignals(False)
            self.items_table.setRowCount(0)
            self.notifications_table.setRowCount(0)
            self.details_view.clear()
            self._total_notifications = 0
            self._dropped = 0
            self._latest.clear()
            self._set_status("Not connected. Use the Connection tab to open a session.")
            self._update_counters()
            self.items_changed.emit()
        self._update_controls()

    def shutdown(self) -> None:
        """Stop receiving notifications; the service deletes the subscription on shutdown."""
        self._detach_listener()
        super().shutdown()

    def _detach_listener(self) -> None:
        if self._monitor_key is not None:
            self._runner.unlisten(self._monitor_key)
            self._monitor_key = None

    # -- configuration -----------------------------------------------------
    def _config(self) -> MonitorConfig:
        return MonitorConfig(
            publishing_interval_ms=float(self.publishing_spin.value()),
            sampling_interval_ms=float(self.sampling_spin.value()),
            trigger=self.trigger_combo.currentText(),
            deadband_type=self.deadband_combo.currentText(),
            deadband_value=float(self.deadband_spin.value()),
        ).normalised()

    def _on_deadband_changed(self, deadband_type: str) -> None:
        """Percent deadband is 0–100; absolute is unbounded engineering units."""
        if deadband_type == "Percent":
            self.deadband_spin.setRange(0.0, 100.0)
        else:
            self.deadband_spin.setRange(0.0, 1_000_000.0)
        self.deadband_spin.setEnabled(deadband_type != "None" and not self._running and not self._busy)

    # -- monitored items ---------------------------------------------------
    def add_node(self, node_id: str) -> bool:
        """Add one NodeId to the list; subscribes immediately when already running."""
        if self._busy:
            self._set_status("The subscription is being created; try again in a moment.", error=True)
            return False
        try:
            canonical = parse_node_id(str(node_id)).to_string()
        except InvalidNodeIdError as exc:
            self._set_status(str(exc), error=True)
            return False
        if self._find_item_row(canonical) is not None:
            self._set_status(f"{canonical} is already in the monitored-item list.")
            return False
        if not self._connected:
            self._set_status("Connect to a server first.", error=True)
            return False

        row = self._append_item_row(canonical, "subscribed" if self._running else "pending")
        self.items_changed.emit()
        if self._running:
            self._subscribe_nodes([canonical], [row])
            self._set_status(f"Subscribing {canonical}…")
        else:
            self._set_status(f"Added {canonical}. Press 'Start monitoring' to subscribe.")
        self._update_controls()
        return True

    def _on_add(self) -> None:
        text = self.node_edit.text().strip()
        if not text:
            self._set_status("Enter a NodeId such as ns=2;s=Temperature.", error=True)
            return
        if self.add_node(text):
            self.node_edit.clear()

    def _on_remove(self) -> None:
        rows = sorted({index.row() for index in self.items_table.selectedIndexes()})
        if not rows:
            self._set_status("Select one or more monitored items first.")
            return
        handles = [
            self.items_table.item(row, 0).data(ITEM_HANDLE)
            for row in rows
            if self.items_table.item(row, 0) is not None
        ]
        handles = [handle for handle in handles if isinstance(handle, int)]
        if self._running and handles:
            self._runner.submit(
                lambda: self._service.monitor_remove(handles),
                on_done=lambda _result: self._remove_rows(rows),
                on_error=lambda message: self._set_status(f"Removing items failed: {message}", error=True),
            )
            return
        self._remove_rows(rows)

    def _remove_rows(self, rows: list[int]) -> None:
        for row in sorted(rows, reverse=True):
            item = self.items_table.item(row, 0)
            if item is not None:
                self._latest.pop(str(item.data(ITEM_NODE_ID)), None)
            self.items_table.removeRow(row)
        self._set_status(f"Removed {len(rows)} monitored item(s) from the list.")
        self._update_controls()
        self._update_counters()
        self.items_changed.emit()

    def _on_clear_items(self) -> None:
        if self._running:
            self._set_status("Stop monitoring before clearing the list.")
            return
        self.items_table.setRowCount(0)
        self._latest.clear()
        self._update_controls()
        self._update_counters()
        self.items_changed.emit()

    def _append_item_row(self, node_id: str, status: str) -> int:
        row = self.items_table.rowCount()
        self.items_table.insertRow(row)
        node_item = QTableWidgetItem(node_id)
        node_item.setData(ITEM_NODE_ID, node_id)
        node_item.setData(ITEM_HANDLE, None)
        value_item = QTableWidgetItem("")
        value_item.setFont(mono_font(9))
        self.items_table.setItem(row, 0, node_item)
        self.items_table.setItem(row, 1, QTableWidgetItem(""))
        self.items_table.setItem(row, 2, QTableWidgetItem(status))
        self.items_table.setItem(row, 3, value_item)
        self.items_table.setItem(row, 4, QTableWidgetItem(""))
        self.items_table.setItem(row, 5, QTableWidgetItem("0"))
        return row

    def _find_item_row(self, node_id: str) -> int | None:
        for row in range(self.items_table.rowCount()):
            item = self.items_table.item(row, 0)
            if item is not None and item.data(ITEM_NODE_ID) == node_id:
                return row
        return None

    def _apply_item_info(self, info: MonitoredItemInfo) -> None:
        row = self._find_item_row(info.node_id)
        if row is None:
            row = self._append_item_row(info.node_id, info.status)
        node_item = self.items_table.item(row, 0)
        if node_item is not None:
            node_item.setData(ITEM_HANDLE, info.client_handle)
        handle_item = self.items_table.item(row, 1)
        if handle_item is not None:
            handle_item.setText("" if info.client_handle is None else str(info.client_handle))
        status_item = self.items_table.item(row, 2)
        if status_item is not None:
            status_item.setText(info.status if info.ok else f"rejected: {info.status}")
            status_item.setToolTip(info.detail or info.status)
            if not info.ok:
                status_item.setForeground(QColor(ERROR))
        self.items_changed.emit()

    def _item_node_ids(self) -> list[str]:
        node_ids: list[str] = []
        for row in range(self.items_table.rowCount()):
            item = self.items_table.item(row, 0)
            if item is not None and item.data(ITEM_NODE_ID):
                node_ids.append(str(item.data(ITEM_NODE_ID)))
        return node_ids

    def item_snapshot(self) -> list[tuple[str, str, str, str]]:
        """Public read-only view for aggregation: (node_id, status, latest value, source timestamp)."""
        rows: list[tuple[str, str, str, str]] = []
        for row in range(self.items_table.rowCount()):
            node_item = self.items_table.item(row, 0)
            if node_item is None:
                continue
            node_id = str(node_item.data(ITEM_NODE_ID) or node_item.text())
            status_item = self.items_table.item(row, 2)
            value_item = self.items_table.item(row, 3)
            time_item = self.items_table.item(row, 4)
            rows.append(
                (
                    node_id,
                    status_item.text() if status_item is not None else "",
                    value_item.text() if value_item is not None else "",
                    time_item.text() if time_item is not None else "",
                )
            )
        return rows

    @property
    def is_running(self) -> bool:
        """True while the subscription of this session exists."""
        return self._running

    def monitored_node_ids(self) -> set[str]:
        """NodeIds in this session's monitor list (pending or already subscribed)."""
        return set(self._item_node_ids())

    # -- project state -----------------------------------------------------
    def monitor_state(self) -> MonitorState:
        """Editor state for a project file (the log and values are not stored)."""
        return MonitorState(
            node_ids=self._item_node_ids(),
            publishing_interval_ms=float(self.publishing_spin.value()),
            sampling_interval_ms=float(self.sampling_spin.value()),
            keep_rows=int(self.max_rows_spin.value()),
        )

    def apply_monitor_state(self, state: MonitorState) -> None:
        """Restore targets as pending rows; no subscription is created."""
        restored = 0
        for node_id in state.node_ids:
            try:
                canonical = parse_node_id(str(node_id)).to_string()
            except InvalidNodeIdError:
                _logger.warning("Ignoring an invalid NodeId from the project: %r", node_id)
                continue
            if self._find_item_row(canonical) is None:
                self._append_item_row(canonical, "pending")
                restored += 1
        self.publishing_spin.setValue(float(state.publishing_interval_ms))
        self.sampling_spin.setValue(float(state.sampling_interval_ms))
        self.max_rows_spin.setValue(int(state.keep_rows))
        self.items_changed.emit()
        self._update_controls()
        self._update_counters()
        if restored:
            self._set_status(f"{restored} node(s) restored as pending; press Start monitoring to subscribe.")

    # -- subscription lifecycle -------------------------------------------
    def _on_start(self) -> None:
        if self._busy or self._running:
            return
        node_ids = self._item_node_ids()
        if not node_ids:
            self._set_status("Add at least one NodeId before starting the monitor.", error=True)
            return
        if not self._connected:
            self._set_status("Connect to a server first.", error=True)
            return

        self._busy = True
        self._update_controls()
        config = self._config()
        key = self._runner.new_key()
        self._detach_listener()
        self._monitor_key = key
        self._runner.listen(key, self._on_progress)
        self._set_status(f"Creating a subscription for {len(node_ids)} node(s)…")
        self._runner.submit(
            lambda: self._service.start_monitor(node_ids, config, emit=self._runner.progress_callback(key)),
            key=key,
            on_done=self._on_started,
            on_error=self._on_start_failed,
        )

    def _on_started(self, start: MonitorStart) -> None:
        self._busy = False
        self._running = True
        self._subscription_id = start.subscription_id
        self._monitoring_mode = "Reporting"
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText("Reporting")
        self.mode_combo.blockSignals(False)
        for info in start.items:
            self._apply_item_info(info)
        self._set_status(start.summary_text())
        self._update_controls()
        self._update_counters()
        self.items_changed.emit()

    def _on_start_failed(self, message: str) -> None:
        self._busy = False
        self._running = False
        self._subscription_id = None
        self._detach_listener()
        _logger.warning("Starting the live monitor failed: %s", message)
        self._set_status(f"Starting the monitor failed: {message}", error=True)
        self._update_controls()

    def _on_stop(self) -> None:
        if self._busy or not self._running:
            return
        self._busy = True
        self._update_controls()
        self._set_status("Deleting the subscription…")
        self._runner.submit(
            self._service.monitor_stop,
            on_done=lambda _result: self._on_stopped(),
            on_error=lambda message: self._on_stop_failed(message),
        )

    def _on_stopped(self) -> None:
        self._busy = False
        self._running = False
        self._subscription_id = None
        self._monitoring_mode = "Reporting"
        self._detach_listener()
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText("Reporting")
        self.mode_combo.blockSignals(False)
        for row in range(self.items_table.rowCount()):
            self.items_table.setItem(row, 2, QTableWidgetItem("stopped"))
            node_item = self.items_table.item(row, 0)
            if node_item is not None:
                node_item.setData(ITEM_HANDLE, None)
            handle_item = self.items_table.item(row, 1)
            if handle_item is not None:
                handle_item.setText("")
        self._set_status("Monitoring stopped; the subscription was deleted.")
        self._update_controls()
        self._update_counters()
        self.items_changed.emit()

    def _on_stop_failed(self, message: str) -> None:
        self._busy = False
        self._detach_listener()
        _logger.warning("Stopping the live monitor reported: %s", message)
        self._set_status(f"Stopping the monitor reported: {message}", error=True)
        self._update_controls()

    def resubscribe_after_reconnect(self) -> None:
        """Rebuild the subscription after an automatic reconnect.

        Monitored-item handles belong to the session that created them, so a
        restored session gets a fresh subscription for the same NodeIds.
        """
        if not self._running or self._busy:
            return
        _logger.info("Restarting the live monitor after an automatic reconnect")
        self._running = False
        self._subscription_id = None
        self._detach_listener()
        for row in range(self.items_table.rowCount()):
            node_item = self.items_table.item(row, 0)
            if node_item is not None:
                node_item.setData(ITEM_HANDLE, None)
            handle_item = self.items_table.item(row, 1)
            if handle_item is not None:
                handle_item.setText("")
            status_item = self.items_table.item(row, 2)
            if status_item is not None:
                status_item.setText("resubscribing…")
        self._on_start()

    def _subscribe_nodes(self, node_ids: list[str], rows: list[int]) -> None:
        for row in rows:
            status_item = self.items_table.item(row, 2)
            if status_item is not None:
                status_item.setText("subscribing…")

        def done(infos: list[MonitoredItemInfo]) -> None:
            for info in infos:
                self._apply_item_info(info)
            rejected = [info for info in infos if not info.ok]
            if rejected:
                self._set_status(
                    "The server rejected: " + ", ".join(f"{info.node_id} ({info.status})" for info in rejected),
                    error=True,
                )
            else:
                self._set_status(f"Monitoring {len(infos)} node(s).")
            self._update_counters()
            self.items_changed.emit()

        self._runner.submit(
            lambda: self._service.monitor_add(node_ids),
            on_done=done,
            on_error=lambda message: self._subscribe_failed(node_ids, message),
        )

    def _subscribe_failed(self, node_ids: list[str], message: str) -> None:
        _logger.warning("Subscribing %s failed: %s", node_ids, message)
        for node_id in node_ids:
            row = self._find_item_row(node_id)
            if row is not None:
                status_item = self.items_table.item(row, 2)
                if status_item is not None:
                    status_item.setText(f"failed: {message}")
                    status_item.setForeground(QColor(ERROR))
        self._set_status(f"Subscribing failed: {message}", error=True)
        self.items_changed.emit()

    # -- runtime subscription changes --------------------------------------
    def _on_apply_intervals(self) -> None:
        if self._busy or not self._running:
            return
        publishing = float(self.publishing_spin.value())
        sampling = float(self.sampling_spin.value())
        self._set_status(f"Applying intervals (publishing {publishing:.0f} ms, sampling {sampling:.0f} ms)…")

        def done(result: Any) -> None:
            revised, failures = result
            if failures:
                self._set_status(
                    f"Publishing interval revised to {revised:.0f} ms; sampling was refused for: "
                    + ", ".join(failures),
                    error=True,
                )
                return
            self._set_status(
                f"Intervals applied: publishing revised to {revised:.0f} ms, sampling {sampling:.0f} ms."
            )
            self._update_counters()

        self._runner.submit(
            lambda: self._service.monitor_apply_intervals(
                publishing_interval_ms=publishing, sampling_interval_ms=sampling
            ),
            on_done=done,
            on_error=lambda message: self._set_status(
                f"Applying the intervals failed: {message}", error=True
            ),
        )

    def _on_mode_changed(self, mode: str) -> None:
        if self._busy or not self._running:
            return
        self._set_status(f"Setting the server monitoring mode to {mode}…")

        def done(applied: str) -> None:
            self._monitoring_mode = applied
            note = " No data changes are delivered while disabled." if applied == "Disabled" else ""
            self._set_status(f"Server monitoring mode is {applied}.{note}")

        def failed(message: str) -> None:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentText(self._monitoring_mode)
            self.mode_combo.blockSignals(False)
            self._set_status(f"Changing the monitoring mode failed: {message}", error=True)

        self._runner.submit(
            lambda: self._service.monitor_set_mode(mode),
            on_done=done,
            on_error=failed,
        )

    # -- notifications -----------------------------------------------------
    def _on_progress(self, payload: Any) -> None:
        if self._closed:
            return
        if isinstance(payload, MonitorNotification):
            self._on_notification(payload)
        elif isinstance(payload, MonitorStatusEvent):
            self._on_status_event(payload)

    def _on_notification(self, notification: MonitorNotification) -> None:
        self._total_notifications += 1
        # Aggregators (the "All monitors" tab) see every notification, including
        # those that pause keeps out of this tab's log.
        self.notification_received.emit(notification)
        # The per-node row is updated even while paused: pause freezes the log only.
        self._update_item_latest(notification)
        if self._paused:
            self._dropped += 1
            self._update_counters()
            return

        table = self.notifications_table
        bar = table.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2

        row = table.rowCount()
        table.insertRow(row)
        received = notification.received_at.astimezone().strftime("%H:%M:%S.%f")[:-3]
        value_text = notification.value_text
        if len(value_text) > _VALUE_CELL_CHARS:
            value_text = value_text[: _VALUE_CELL_CHARS - 1] + "…"
        cells = [
            received,
            notification.node_id,
            value_text,
            notification.status,
            notification.source_timestamp or "",
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 0:
                item.setData(NOTIFICATION_PAYLOAD, notification)
            if column == 2:
                item.setFont(mono_font(9))
                item.setToolTip(notification.value_text)
            if column == 4 and notification.server_timestamp:
                item.setToolTip(
                    f"Source: {notification.source_timestamp or '-'}\n"
                    f"Server: {notification.server_timestamp}"
                )
            if notification.status != "Good" and column == 3:
                item.setForeground(QColor(ERROR))
            table.setItem(row, column, item)

        while table.rowCount() > int(self.max_rows_spin.value()):
            table.removeRow(0)
        if follow:
            table.scrollToBottom()
        self._update_counters()

    def _on_status_event(self, event: MonitorStatusEvent) -> None:
        detail = f" ({event.detail})" if event.detail else ""
        self._set_status(
            f"Server reported subscription status {event.status}{detail}.",
            error=event.status != "Good",
        )

    def _update_item_latest(self, notification: MonitorNotification) -> None:
        """Overwrite the monitored-item row for this node with the newest value."""
        row = self._find_item_row(notification.node_id)
        if row is None:
            return
        self._latest[notification.node_id] = notification

        value_text = notification.value_text
        if len(value_text) > _LATEST_CELL_CHARS:
            value_text = value_text[: _LATEST_CELL_CHARS - 1] + "…"
        value_item = self.items_table.item(row, 3)
        if value_item is None:
            value_item = QTableWidgetItem()
            value_item.setFont(mono_font(9))
            self.items_table.setItem(row, 3, value_item)
        value_item.setText(value_text)
        value_item.setToolTip(notification.value_text)

        status_item = self.items_table.item(row, 2)
        if status_item is not None:
            status_item.setText(notification.status)
            status_item.setToolTip(f"Last notification: {notification.status}")
            status_item.setForeground(QColor(ERROR) if notification.status != "Good" else QBrush())

        timestamp_item = self.items_table.item(row, 4)
        if timestamp_item is not None:
            timestamp_item.setText(notification.source_timestamp or "")
            timestamp_item.setToolTip(
                f"Source: {notification.source_timestamp or '-'}\n"
                f"Server: {notification.server_timestamp or '-'}\n"
                f"Received: {notification.received_at.astimezone().isoformat(timespec='milliseconds')}"
            )

        count_item = self.items_table.item(row, 5)
        if count_item is not None:
            try:
                count = int(count_item.text() or 0)
            except ValueError:  # pragma: no cover - defensive
                count = 0
            count_item.setText(str(count + 1))

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        if not self._running:
            self._update_counters()
            return
        if paused:
            self._dropped = 0
            self._set_status("Paused: new rows are not added to the notification log (latest values keep updating).")
        else:
            dropped = self._dropped
            self._dropped = 0
            message = (
                "Resumed."
                if not dropped
                else f"Resumed; {dropped} notification(s) were not added to the log while paused."
            )
            self._set_status(message)
        self._update_counters()

    def _on_clear_notifications(self) -> None:
        self.notifications_table.setRowCount(0)
        self.details_view.clear()
        self._total_notifications = 0
        self._dropped = 0
        self._update_counters()

    def _on_max_rows_changed(self, value: int) -> None:
        table = self.notifications_table
        while table.rowCount() > int(value):
            table.removeRow(0)
        self._update_counters()

    def _on_item_selected(self) -> None:
        """Show the last value of the selected monitored item."""
        rows = {index.row() for index in self.items_table.selectedIndexes()}
        if len(rows) != 1:
            return
        item = self.items_table.item(min(rows), 0)
        node_id = str(item.data(ITEM_NODE_ID)) if item is not None and item.data(ITEM_NODE_ID) else ""
        if not node_id:
            return
        notification = self._latest.get(node_id)
        if notification is None:
            self.details_view.setPlainText(f"{node_id}\nNo notification received yet.")
            return
        self._show_payload(notification, title=f"Latest value for {node_id}")

    def _show_payload(self, notification: MonitorNotification, *, title: str = "") -> None:
        payload = notification.to_dict()
        if notification.truncated:
            payload["value_preview"] = notification.value_text
        payload["received_at_local"] = notification.received_at.astimezone().isoformat(timespec="milliseconds")
        payload["received_at_utc"] = iso_utc(notification.received_at)
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        self.details_view.setPlainText(f"{title}\n{text}" if title else text)

    def _on_notification_selected(self) -> None:
        rows = {index.row() for index in self.notifications_table.selectedIndexes()}
        if not rows:
            self.details_view.clear()
            return
        item = self.notifications_table.item(min(rows), 0)
        notification = item.data(NOTIFICATION_PAYLOAD) if item is not None else None
        if not isinstance(notification, MonitorNotification):
            self.details_view.clear()
            return
        self._show_payload(notification)

    # -- controls ----------------------------------------------------------
    def _update_controls(self) -> None:
        connected = self._connected
        has_items = self.items_table.rowCount() > 0
        has_selection = bool(self.items_table.selectedIndexes())

        self.node_edit.setEnabled(connected and not self._busy)
        self.add_btn.setEnabled(connected and not self._busy)
        self.remove_btn.setEnabled(connected and has_selection and not self._busy)
        self.clear_items_btn.setEnabled(connected and not self._running)
        # The interval fields stay editable while running so "Apply intervals"
        # can modify the live subscription; the filter is fixed at start.
        self.publishing_spin.setEnabled(connected and not self._busy)
        self.sampling_spin.setEnabled(connected and not self._busy)
        filter_editable = connected and not self._running and not self._busy
        self.trigger_combo.setEnabled(filter_editable)
        self.deadband_combo.setEnabled(filter_editable)
        self.deadband_spin.setEnabled(
            filter_editable and self.deadband_combo.currentText() != "None"
        )
        self.apply_intervals_btn.setEnabled(self._running and not self._busy)
        self.mode_combo.setEnabled(self._running and not self._busy)
        self.start_btn.setEnabled(connected and has_items and not self._running and not self._busy)
        self.stop_btn.setEnabled(self._running and not self._busy)
        self.pause_btn.setEnabled(self._running)
        if not self._running:
            self.pause_btn.setChecked(False)

    def _update_counters(self) -> None:
        items = self.items_table.rowCount()
        parts = [f"{self._total_notifications} notification(s)", f"{items} monitored item(s)"]
        if self._subscription_id is not None:
            parts.append(f"subscription {self._subscription_id}")
        if self._dropped:
            parts.append(f"{self._dropped} not added while paused")
        self.counters_label.setText(" · ".join(parts))
