"""Events tab: one event subscription, stored events and the alarm summary.

Nothing is subscribed implicitly. The operator adds one or more source NodeIds
(usually the Server object), starts one subscription, and sees the events the
server publishes as a bounded log. Every selected event can be inspected field
by field. The same page can read a source's stored events (``ReadEventDetails``)
and read the state of the conditions it has seen on demand.

The tab never writes a variable. It has exactly two condition actions, both
sent once through ``write_service``: an operator-confirmed condition method call
(Acknowledge, Confirm, Enable, Disable, AddComment) on a condition event, and a
``ConditionRefresh`` call on the running subscription that asks the server to
publish its retained conditions again. Condition state is read from the
condition object's own variables and never inferred from an event.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
    DEFAULT_EVENT_KEEP_ROWS,
    DEFAULT_EVENT_PUBLISHING_INTERVAL_MS,
    DEFAULT_HISTORY_MAX_VALUES,
    MAX_HISTORY_MAX_VALUES,
    MAX_KEEP_ROWS,
    MAX_PUBLISHING_INTERVAL_MS,
    MIN_EVENT_PUBLISHING_INTERVAL_MS,
    MIN_KEEP_ROWS,
)
from ...errors import InvalidNodeIdError
from ...exporters.observation_log import write_observation_log
from ...models import (
    CONDITION_ACTION_LABELS,
    CONDITION_ACTIONS,
    STATUS_PASSED,
    ConditionStateSnapshot,
    EventConfig,
    EventHistoryConfig,
    EventHistoryResult,
    EventNotification,
    EventSourceInfo,
    EventStart,
    MonitorStatusEvent,
    iso_utc,
)
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...opcua.event_service import SERVER_OBJECT_NODE_ID, event_id_bytes
from ...operator_text import (
    condition_action_confirmation,
    condition_action_result_text,
    condition_refresh_text,
    condition_state_text,
)
from ..async_runner import AsyncRunner
from ..selection_marker import SelectionMarker
from ..tab_notice import OptionsRow, TabNotice
from ..theme import ERROR, mono_font
from .base import SessionTab

_logger = logging.getLogger(__name__)

SOURCE_NODE_ID = Qt.UserRole + 11
SOURCE_HANDLE = Qt.UserRole + 12
EVENT_PAYLOAD = Qt.UserRole + 13
EVENT_SOURCE = Qt.UserRole + 14

_MAX_MESSAGE_CELL_CHARS = 300
_MAX_TIME_CELL_CHARS = 32


class EventsTab(SessionTab):
    """One event subscription with a source list and a bounded event log."""

    #: Emitted on the GUI thread for every event delivered to this tab.
    event_received = Signal(object)
    #: Emitted when the source list or its status text changed.
    sources_changed = Signal()
    #: An operational summary for the shared "Logs & reports" tab.
    report_ready = Signal(str)

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
        self._running = False
        self._busy = False
        self._closed = False
        self._events_key: str | None = None
        self._subscription_id: int | None = None
        self._total_events = 0
        self._selected_notification: EventNotification | None = None
        self._condition_busy = False
        self._stored_events = 0

        self._build_ui()
        self.set_connected(False)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Events")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addWidget(
            TabNotice(
                "One event subscription on the sources you add, stored events and an on-demand alarm "
                "summary.",
                "One event subscription on the source nodes you add explicitly (usually the Server object). The "
                "server decides what it publishes; the fields come from the selected event type. For condition "
                "events choose an event type such as ConditionType (i=2782) or AcknowledgeableConditionType "
                "(i=2881), then one selected event can be sent to the condition's own methods: Acknowledge, "
                "Confirm, Enable, Disable and AddComment - one confirmed method call each, sent once, never "
                "retried. Refresh conditions asks the server once to publish the conditions it already retains "
                "for this subscription. 'Event history' reads stored events; 'Read states' reads each condition "
                "object's AckedState/ConfirmedState/EnabledState/Retain on demand, never inferred from an event. "
                "No variable is written.",
            )
        )

        source_row = QHBoxLayout()
        self.source_edit = QLineEdit(SERVER_OBJECT_NODE_ID)
        self.source_edit.setPlaceholderText(SERVER_OBJECT_NODE_ID)
        self.source_edit.setToolTip(
            "NodeId whose EventNotifier is monitored. The OPC UA Server object is i=2253; "
            "objects that expose events can be added too."
        )
        self.source_edit.returnPressed.connect(self._on_add_source)
        self.add_source_btn = QPushButton("Add source")
        self.add_source_btn.clicked.connect(self._on_add_source)
        self.use_selection_btn = QPushButton("Use browser selection")
        self.use_selection_btn.clicked.connect(self._on_use_selection)
        self.remove_source_btn = QPushButton("Remove selected")
        self.remove_source_btn.clicked.connect(self._on_remove_source)
        self.clear_sources_btn = QPushButton("Clear list")
        self.clear_sources_btn.clicked.connect(self._on_clear_sources)
        source_row.addWidget(QLabel("Event source:"))
        source_row.addWidget(self.source_edit, 2)
        source_row.addWidget(self.add_source_btn)
        source_row.addWidget(self.use_selection_btn)
        source_row.addWidget(self.remove_source_btn)
        source_row.addWidget(self.clear_sources_btn)
        source_row.addWidget(SelectionMarker(self.source_edit, self._selection_provider))
        layout.addLayout(source_row)

        controls = QHBoxLayout()
        self.event_type_edit = QLineEdit()
        self.event_type_edit.setPlaceholderText("BaseEventType — every event (default)")
        self.event_type_edit.setToolTip(
            "Event type whose fields are requested. Empty means BaseEventType (all events, standard fields). "
            "A custom event type adds its own properties to the detail view."
        )
        self.publishing_spin = QDoubleSpinBox()
        self.publishing_spin.setRange(MIN_EVENT_PUBLISHING_INTERVAL_MS, MAX_PUBLISHING_INTERVAL_MS)
        self.publishing_spin.setDecimals(0)
        self.publishing_spin.setSingleStep(100.0)
        self.publishing_spin.setValue(DEFAULT_EVENT_PUBLISHING_INTERVAL_MS)
        self.publishing_spin.setSuffix(" ms")
        self.publishing_spin.setToolTip("Requested publishing interval of the event subscription.")
        self.start_btn = QPushButton("Start events")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self._on_stop)
        self.clear_log_btn = QPushButton("Clear events")
        self.clear_log_btn.clicked.connect(self._on_clear_events)
        self.export_log_btn = QPushButton("Export log…")
        self.export_log_btn.setToolTip(
            "Write the event rows currently in the table as a versioned JSON document or CSV. "
            "It is exactly what the table holds (Keep rows), not everything ever received."
        )
        self.export_log_btn.clicked.connect(self._on_export_log)
        self.export_log_btn.setEnabled(False)
        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(MIN_KEEP_ROWS, MAX_KEEP_ROWS)
        self.max_rows_spin.setSingleStep(100)
        self.max_rows_spin.setValue(DEFAULT_EVENT_KEEP_ROWS)
        self.max_rows_spin.setToolTip("Rows kept in the event log; the oldest rows are dropped first.")
        self.max_rows_spin.valueChanged.connect(self._on_max_rows_changed)
        # The server-side filter is fixed when the subscription starts; it lives in a
        # hidden row whose toggle names whatever is in effect.
        self.options = OptionsRow("Filter")
        self.history_options = OptionsRow("Event history")

        controls.addWidget(QLabel("Event type:"))
        controls.addWidget(self.event_type_edit, 1)
        controls.addWidget(QLabel("Publishing:"))
        controls.addWidget(self.publishing_spin)
        controls.addSpacing(12)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        controls.addSpacing(12)
        controls.addWidget(self.clear_log_btn)
        controls.addWidget(self.export_log_btn)
        controls.addWidget(QLabel("Keep rows:"))
        controls.addWidget(self.max_rows_spin)
        controls.addSpacing(12)
        controls.addWidget(self.options.toggle)
        controls.addWidget(self.history_options.toggle)
        layout.addLayout(controls)

        self.fields_edit = QLineEdit()
        self.fields_edit.setPlaceholderText("Extra fields, e.g. ConditionClassId, 2:Temperature")
        self.fields_edit.setToolTip(
            "Requested in addition to the standard fields. Comma-separated; use 'index:Name' for a "
            "property outside namespace 0. The filter is fixed when the subscription starts."
        )
        self.fields_edit.textChanged.connect(self._update_controls)
        self.min_severity_spin = QSpinBox()
        self.min_severity_spin.setRange(0, 65_535)
        self.min_severity_spin.setSpecialValueText("any")
        self.min_severity_spin.setToolTip(
            "Server-side WhereClause: only events with at least this Severity are delivered (0 = any)."
        )
        self.min_severity_spin.valueChanged.connect(self._update_controls)
        self.options.add_widget(QLabel("Extra fields:"))
        self.options.add_widget(self.fields_edit, 1)
        self.options.add_widget(QLabel("Min severity:"))
        self.options.add_widget(self.min_severity_spin)
        layout.addWidget(self.options.widget)

        # Stored events: one source node, one optional range, one bounded read.
        self.history_from_check = QCheckBox("From")
        self.history_from_check.toggled.connect(self._update_controls)
        self.history_from_edit = QDateTimeEdit()
        self.history_from_edit.setCalendarPopup(True)
        self.history_from_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.history_from_edit.setDateTime(QDateTime.currentDateTime())
        self.history_to_check = QCheckBox("To")
        self.history_to_check.toggled.connect(self._update_controls)
        self.history_to_edit = QDateTimeEdit()
        self.history_to_edit.setCalendarPopup(True)
        self.history_to_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.history_to_edit.setDateTime(QDateTime.currentDateTime())
        self.history_max_spin = QSpinBox()
        self.history_max_spin.setRange(1, MAX_HISTORY_MAX_VALUES)
        self.history_max_spin.setValue(DEFAULT_HISTORY_MAX_VALUES)
        self.history_max_spin.setToolTip("Hard cap on the stored events one read returns.")
        self.read_history_btn = QPushButton("Read stored events")
        self.read_history_btn.setToolTip(
            "One ReadEventDetails on the selected source (or the only one in the list). "
            "Rows are marked 'stored'; the server decides what it kept."
        )
        self.read_history_btn.clicked.connect(self._on_read_event_history)
        self.history_options.add_widget(QLabel("Read one source:"))
        self.history_options.add_widget(self.history_from_check)
        self.history_options.add_widget(self.history_from_edit)
        self.history_options.add_widget(self.history_to_check)
        self.history_options.add_widget(self.history_to_edit)
        self.history_options.add_widget(QLabel("Max:"))
        self.history_options.add_widget(self.history_max_spin)
        self.history_options.add_widget(self.read_history_btn)
        self.history_options.add_stretch(1)
        layout.addWidget(self.history_options.widget)

        splitter = QSplitter(Qt.Vertical)

        sources_box = QWidget()
        sources_layout = QVBoxLayout(sources_box)
        sources_layout.setContentsMargins(0, 0, 0, 0)
        sources_layout.addWidget(QLabel("Event sources"))
        self.sources_table = QTableWidget(0, 3)
        self.sources_table.setHorizontalHeaderLabels(["NodeId", "Status", "Handle"])
        self.sources_table.verticalHeader().setVisible(False)
        self.sources_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sources_table.setAlternatingRowColors(True)
        self.sources_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sources_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sources_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.sources_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.sources_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.sources_table.itemSelectionChanged.connect(self._update_controls)
        self.sources_table.setMaximumHeight(150)
        sources_layout.addWidget(self.sources_table)
        splitter.addWidget(sources_box)

        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QLabel("Events (newest last)"))
        self.events_table = QTableWidget(0, 6)
        self.events_table.setHorizontalHeaderLabels(["Received", "Time", "Severity", "Type", "Source", "Message"])
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.events_table.setAlternatingRowColors(True)
        self.events_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.events_table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.events_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.events_table.itemSelectionChanged.connect(self._on_event_selected)
        log_layout.addWidget(self.events_table, 1)
        splitter.addWidget(log_box)

        details_box = QWidget()
        details_layout = QVBoxLayout(details_box)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_header = QHBoxLayout()
        details_header.addWidget(QLabel("Selected event — every field the subscription selected"))
        details_header.addStretch(1)
        self.read_states_btn = QPushButton("Read states")
        self.read_states_btn.setToolTip(condition_state_text())
        self.read_states_btn.clicked.connect(self._on_read_condition_states)
        self.read_states_btn.setEnabled(False)
        details_header.addWidget(self.read_states_btn)
        self.refresh_conditions_btn = QPushButton("Refresh conditions")
        self.refresh_conditions_btn.setToolTip(
            "Calls ConditionRefresh once on this subscription so the server publishes the conditions it "
            "already retains again. Stores, acknowledges and changes nothing; no confirmation is needed."
        )
        self.refresh_conditions_btn.clicked.connect(self._on_refresh_conditions)
        self.refresh_conditions_btn.setEnabled(False)
        details_header.addWidget(self.refresh_conditions_btn)
        details_layout.addLayout(details_header)

        # The condition methods of one selected event. Each is one confirmed call on
        # the same EventId; the method is resolved below the condition node by its
        # standard name, so a condition that does not expose one simply refuses.
        condition_row = QHBoxLayout()
        condition_row.addWidget(QLabel("Condition:"))
        self.condition_buttons: dict[str, QPushButton] = {}
        tooltips = {
            "acknowledge": "Calls the condition's Acknowledge method once, after an explicit confirmation.",
            "confirm": "Calls the condition's Confirm method once: confirms an acknowledged condition event.",
            "enable": "Calls the condition's Enable method once so the server may report its events again.",
            "disable": "Calls the condition's Disable method once; the condition stops reporting events.",
            "comment": "Calls the condition's AddComment method once with the text you enter.",
        }
        for action in CONDITION_ACTIONS:
            button = QPushButton(f"{CONDITION_ACTION_LABELS[action]}…")
            button.setToolTip(
                tooltips[action]
                + " Needs an event subscription whose event type selects condition fields "
                "(ConditionType or a subtype)."
            )
            button.clicked.connect(
                lambda _checked=False, selected=action: self._on_condition_action(selected)
            )
            button.setEnabled(False)
            self.condition_buttons[action] = button
            condition_row.addWidget(button)
        condition_row.addStretch(1)
        details_layout.addLayout(condition_row)
        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setFont(mono_font(9))
        self.details_view.setPlaceholderText("Select an event to see all of its fields.")
        details_layout.addWidget(self.details_view, 1)
        splitter.addWidget(details_box)

        state_box = QWidget()
        state_layout = QVBoxLayout(state_box)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.addWidget(
            QLabel("Alarm summary — condition state read on demand (never inferred from an event)")
        )
        self.state_table = QTableWidget(0, 8)
        self.state_table.setHorizontalHeaderLabels(
            ["Condition", "Name", "Acked", "Confirmed", "Enabled", "Retain", "Last severity", "Status"]
        )
        self.state_table.verticalHeader().setVisible(False)
        self.state_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.state_table.setAlternatingRowColors(True)
        self.state_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        state_header = self.state_table.horizontalHeader()
        for column in range(7):
            state_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        state_header.setSectionResizeMode(1, QHeaderView.Stretch)
        state_layout.addWidget(self.state_table, 1)
        splitter.addWidget(state_box)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        splitter.setStretchFactor(3, 1)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("Not connected.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.counters_label = QLabel("0 event(s) · 0 source(s)")
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
            self._selected_notification = None
            self._condition_busy = False
            self.sources_table.setRowCount(0)
            self.events_table.setRowCount(0)
            self.state_table.setRowCount(0)
            self.details_view.clear()
            self._total_events = 0
            self._stored_events = 0
            self._set_status("Not connected. Use the Connection tab to open a session.")
            self._update_counters()
            self.sources_changed.emit()
        self._update_controls()

    def shutdown(self) -> None:
        """Stop receiving events and cancel work; the service deletes the subscription."""
        self._detach_listener()
        super().shutdown()

    def _detach_listener(self) -> None:
        if self._events_key is not None:
            self._runner.unlisten(self._events_key)
            self._events_key = None

    # -- configuration -----------------------------------------------------
    def _config(self) -> EventConfig:
        return EventConfig(
            publishing_interval_ms=float(self.publishing_spin.value()),
            event_type_node_id=self.event_type_edit.text().strip(),
            select_fields=[part.strip() for part in self.fields_edit.text().split(",") if part.strip()],
            min_severity=int(self.min_severity_spin.value()) or None,
        ).normalised()

    # -- sources -----------------------------------------------------------
    def add_source(self, node_id: str) -> bool:
        """Add one source NodeId; subscribes immediately when already running."""
        if self._busy:
            self._set_status("The subscription is being created; try again in a moment.", error=True)
            return False
        try:
            canonical = parse_node_id(str(node_id)).to_string()
        except InvalidNodeIdError as exc:
            self._set_status(str(exc), error=True)
            return False
        if self._find_source_row(canonical) is not None:
            self._set_status(f"{canonical} is already in the source list.")
            return False
        if not self._connected:
            self._set_status("Connect to a server first.", error=True)
            return False

        row = self._append_source_row(canonical, "subscribed" if self._running else "pending")
        self.sources_changed.emit()
        if self._running:
            self._subscribe_sources([canonical], [row])
            self._set_status(f"Subscribing to events of {canonical}…")
        else:
            self._set_status(f"Added {canonical}. Press 'Start events' to subscribe.")
        self._update_controls()
        return True

    def _on_add_source(self) -> None:
        text = self.source_edit.text().strip()
        if not text:
            self._set_status(f"Enter a source NodeId such as {SERVER_OBJECT_NODE_ID}.", error=True)
            return
        if self.add_source(text):
            self.source_edit.clear()

    def _on_use_selection(self) -> None:
        """Offer the node selected in this session's address-space browser."""
        node_id = self._selection_provider() if self._selection_provider is not None else None
        if not node_id:
            self._set_status("No node is selected in the address space browser.", error=True)
            return
        self.source_edit.setText(str(node_id))
        self._set_status(f"Event source set to {node_id}. Press 'Add source' to listen to its events.")

    def _on_remove_source(self) -> None:
        rows = sorted({index.row() for index in self.sources_table.selectedIndexes()})
        if not rows:
            self._set_status("Select one or more sources first.")
            return
        handles = [
            self.sources_table.item(row, 0).data(SOURCE_HANDLE)
            for row in rows
            if self.sources_table.item(row, 0) is not None
        ]
        handles = [handle for handle in handles if isinstance(handle, int)]
        if self._running and handles:
            self._submit(
                lambda: self._service.event_monitor_remove(handles),
                on_done=lambda _result: self._remove_source_rows(rows),
                on_error=lambda message: self._set_status(f"Removing sources failed: {message}", error=True),
            )
            return
        self._remove_source_rows(rows)

    def _remove_source_rows(self, rows: list[int]) -> None:
        for row in sorted(rows, reverse=True):
            self.sources_table.removeRow(row)
        self._set_status(f"Removed {len(rows)} event source(s) from the list.")
        self._update_controls()
        self._update_counters()
        self.sources_changed.emit()

    def _on_clear_sources(self) -> None:
        if self._running:
            self._set_status("Stop the event subscription before clearing the list.")
            return
        self.sources_table.setRowCount(0)
        self._update_controls()
        self._update_counters()
        self.sources_changed.emit()

    def _append_source_row(self, node_id: str, status: str) -> int:
        row = self.sources_table.rowCount()
        self.sources_table.insertRow(row)
        node_item = QTableWidgetItem(node_id)
        node_item.setData(SOURCE_NODE_ID, node_id)
        node_item.setData(SOURCE_HANDLE, None)
        self.sources_table.setItem(row, 0, node_item)
        self.sources_table.setItem(row, 1, QTableWidgetItem(status))
        self.sources_table.setItem(row, 2, QTableWidgetItem(""))
        return row

    def _find_source_row(self, node_id: str) -> int | None:
        for row in range(self.sources_table.rowCount()):
            item = self.sources_table.item(row, 0)
            if item is not None and item.data(SOURCE_NODE_ID) == node_id:
                return row
        return None

    def _apply_source_info(self, info: EventSourceInfo) -> None:
        row = self._find_source_row(info.node_id)
        if row is None:
            row = self._append_source_row(info.node_id, info.status)
        node_item = self.sources_table.item(row, 0)
        if node_item is not None:
            node_item.setData(SOURCE_HANDLE, info.client_handle)
        handle_item = self.sources_table.item(row, 2)
        if handle_item is not None:
            handle_item.setText("" if info.client_handle is None else str(info.client_handle))
        status_item = self.sources_table.item(row, 1)
        if status_item is not None:
            status_item.setText(info.status if info.ok else f"rejected: {info.status}")
            status_item.setToolTip(info.detail or info.status)
            if not info.ok:
                status_item.setForeground(QColor(ERROR))
        self.sources_changed.emit()

    def _source_node_ids(self) -> list[str]:
        node_ids: list[str] = []
        for row in range(self.sources_table.rowCount()):
            item = self.sources_table.item(row, 0)
            if item is not None and item.data(SOURCE_NODE_ID):
                node_ids.append(str(item.data(SOURCE_NODE_ID)))
        return node_ids

    @property
    def is_running(self) -> bool:
        """True while the event subscription of this session exists."""
        return self._running

    # -- subscription lifecycle -------------------------------------------
    def _on_start(self) -> None:
        if self._busy or self._running:
            return
        source_ids = self._source_node_ids()
        if not source_ids:
            self._set_status("Add at least one event source before starting.", error=True)
            return
        if not self._connected:
            self._set_status("Connect to a server first.", error=True)
            return

        self._busy = True
        self._update_controls()
        config = self._config()
        key = self._runner.new_key()
        self._detach_listener()
        self._events_key = key
        self._runner.listen(key, self._on_progress)
        filter_text = self._filter_summary()
        suffix = f" with a server-side filter ({filter_text})" if filter_text else ""
        self._set_status(f"Creating an event subscription for {len(source_ids)} source(s){suffix}…")
        self._submit(
            lambda: self._service.start_event_monitor(
                source_ids, config, emit=self._runner.progress_callback(key)
            ),
            key=key,
            on_done=self._on_started,
            on_error=self._on_start_failed,
        )

    def _on_started(self, start: EventStart) -> None:
        self._busy = False
        self._running = True
        self._subscription_id = start.subscription_id
        for info in start.sources:
            self._apply_source_info(info)
        self._set_status(start.summary_text())
        self._update_controls()
        self._update_counters()
        self.sources_changed.emit()

    def _on_start_failed(self, message: str) -> None:
        self._busy = False
        self._running = False
        self._subscription_id = None
        self._detach_listener()
        _logger.warning("Starting event monitoring failed: %s", message)
        self._set_status(f"Starting events failed: {message}", error=True)
        self._update_controls()

    def _on_stop(self) -> None:
        if self._busy or not self._running:
            return
        self._busy = True
        self._update_controls()
        self._set_status("Deleting the event subscription…")
        self._submit(
            self._service.event_monitor_stop,
            on_done=lambda _result: self._on_stopped(),
            on_error=lambda message: self._on_stop_failed(message),
        )

    def _on_stopped(self) -> None:
        self._busy = False
        self._running = False
        self._subscription_id = None
        self._detach_listener()
        for row in range(self.sources_table.rowCount()):
            self.sources_table.setItem(row, 1, QTableWidgetItem("stopped"))
            node_item = self.sources_table.item(row, 0)
            if node_item is not None:
                node_item.setData(SOURCE_HANDLE, None)
            handle_item = self.sources_table.item(row, 2)
            if handle_item is not None:
                handle_item.setText("")
        self._set_status("Event monitoring stopped; the subscription was deleted.")
        self._update_controls()
        self._update_counters()
        self.sources_changed.emit()

    def _on_stop_failed(self, message: str) -> None:
        self._busy = False
        self._detach_listener()
        _logger.warning("Stopping event monitoring reported: %s", message)
        self._set_status(f"Stopping events reported: {message}", error=True)
        self._update_controls()

    def resubscribe_after_reconnect(self) -> None:
        """Rebuild the event subscription after an automatic reconnect."""
        if not self._running or self._busy:
            return
        _logger.info("Restarting event monitoring after an automatic reconnect")
        self._running = False
        self._subscription_id = None
        self._detach_listener()
        for row in range(self.sources_table.rowCount()):
            node_item = self.sources_table.item(row, 0)
            if node_item is not None:
                node_item.setData(SOURCE_HANDLE, None)
            handle_item = self.sources_table.item(row, 2)
            if handle_item is not None:
                handle_item.setText("")
            status_item = self.sources_table.item(row, 1)
            if status_item is not None:
                status_item.setText("resubscribing…")
        self._on_start()

    def _subscribe_sources(self, node_ids: list[str], rows: list[int]) -> None:
        for row in rows:
            status_item = self.sources_table.item(row, 1)
            if status_item is not None:
                status_item.setText("subscribing…")

        def done(infos: list[EventSourceInfo]) -> None:
            for info in infos:
                self._apply_source_info(info)
            rejected = [info for info in infos if not info.ok]
            if rejected:
                self._set_status(
                    "The server refused: " + ", ".join(f"{info.node_id} ({info.status})" for info in rejected),
                    error=True,
                )
            else:
                self._set_status(f"Listening to events of {len(infos)} source(s).")
            self._update_counters()
            self.sources_changed.emit()

        self._submit(
            lambda: self._service.event_monitor_add(node_ids),
            on_done=done,
            on_error=lambda message: self._subscribe_failed(node_ids, message),
        )

    def _subscribe_failed(self, node_ids: list[str], message: str) -> None:
        _logger.warning("Subscribing event sources %s failed: %s", node_ids, message)
        for node_id in node_ids:
            row = self._find_source_row(node_id)
            if row is not None:
                status_item = self.sources_table.item(row, 1)
                if status_item is not None:
                    status_item.setText(f"failed: {message}")
                    status_item.setForeground(QColor(ERROR))
        self._set_status(f"Subscribing failed: {message}", error=True)
        self.sources_changed.emit()

    # -- notifications -----------------------------------------------------
    def _on_progress(self, payload: Any) -> None:
        if self._closed:
            return
        if isinstance(payload, EventNotification):
            self._on_event(payload)
        elif isinstance(payload, MonitorStatusEvent):
            self._on_status_event(payload)

    def _on_event(self, notification: EventNotification) -> None:
        self._total_events += 1
        self.event_received.emit(notification)
        self._append_event_row(notification, stored=False)

    def _append_event_row(self, notification: EventNotification, *, stored: bool) -> None:
        """Append one row; a stored event is marked as such, never as live."""
        table = self.events_table
        bar = table.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2

        message = notification.message
        if len(message) > _MAX_MESSAGE_CELL_CHARS:
            message = message[: _MAX_MESSAGE_CELL_CHARS - 1] + "…"
        event_time = notification.time or ""
        if len(event_time) > _MAX_TIME_CELL_CHARS:
            event_time = event_time[:_MAX_TIME_CELL_CHARS]
        source = notification.source_name or notification.source_node

        row = table.rowCount()
        table.insertRow(row)
        received = (
            "stored"
            if stored
            else notification.received_at.astimezone().strftime("%H:%M:%S.%f")[:-3]
        )
        cells = [
            received,
            event_time,
            "" if notification.severity is None else str(notification.severity),
            notification.event_type,
            source,
            message,
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 0:
                item.setData(EVENT_PAYLOAD, notification)
                item.setData(EVENT_SOURCE, "stored" if stored else "live")
                if stored:
                    item.setToolTip("Read from the server's stored events (ReadEventDetails).")
            if column == 5:
                item.setToolTip(notification.message)
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

    def _on_clear_events(self) -> None:
        self.events_table.setRowCount(0)
        self.state_table.setRowCount(0)
        self.details_view.clear()
        self._selected_notification = None
        self._total_events = 0
        self._stored_events = 0
        self._update_condition_buttons()
        self._update_counters()

    # -- stored events -----------------------------------------------------
    def _history_source_id(self) -> str:
        """The one source a stored-event read targets."""
        rows = {index.row() for index in self.sources_table.selectedIndexes()}
        if len(rows) > 1:
            raise ValueError("Select exactly one event source for a stored-event read.")
        if len(rows) == 1:
            item = self.sources_table.item(min(rows), 0)
            value = item.data(SOURCE_NODE_ID) if item is not None else None
            if value:
                return str(value)
        if self.sources_table.rowCount() == 1:
            item = self.sources_table.item(0, 0)
            value = item.data(SOURCE_NODE_ID) if item is not None else None
            if value:
                return str(value)
        if self.sources_table.rowCount() > 1:
            raise ValueError("Select one event source in the table for the stored-event read.")
        text = self.source_edit.text().strip() or SERVER_OBJECT_NODE_ID
        return parse_node_id(text).to_string()

    def _event_history_config(self) -> EventHistoryConfig:
        start = (
            self.history_from_edit.dateTime().toUTC().toPython()
            if self.history_from_check.isChecked()
            else None
        )
        end = (
            self.history_to_edit.dateTime().toUTC().toPython()
            if self.history_to_check.isChecked()
            else None
        )
        return EventHistoryConfig(
            start=start,
            end=end,
            max_values=int(self.history_max_spin.value()),
            event_type_node_id=self.event_type_edit.text().strip(),
            select_fields=[part.strip() for part in self.fields_edit.text().split(",") if part.strip()],
            min_severity=int(self.min_severity_spin.value()) or None,
        ).normalised()

    def _on_read_event_history(self) -> None:
        """Read one source's stored events; read-only, one bounded request per page."""
        if self._busy or not self._connected:
            return
        try:
            source = self._history_source_id()
        except (ValueError, InvalidNodeIdError) as exc:
            self._set_status(str(exc), error=True)
            return
        config = self._event_history_config()
        if config.start and config.end and config.start > config.end:
            self._set_status("The stored-event start must not be after its end.", error=True)
            return
        self._busy = True
        self._update_controls()
        self._set_status(f"Reading stored events of {source}…")

        def done(result: EventHistoryResult) -> None:
            self._busy = False
            if self._closed:
                return
            for notification in result.events:
                self._stored_events += 1
                self._append_event_row(notification, stored=True)
            self._set_status(result.summary_text(), error=result.status != "Good")
            self._update_controls()
            self._update_counters()

        def failed(message: str) -> None:
            self._busy = False
            if self._closed:
                return
            _logger.warning("Reading stored events of %s failed: %s", source, message)
            self._set_status(f"Reading stored events failed: {message}", error=True)
            self._update_controls()

        self._runner.submit(
            lambda: self._service.read_event_history(source, config),
            on_done=done,
            on_error=failed,
        )

    # -- alarm summary -----------------------------------------------------
    def _condition_node_ids(self) -> list[str]:
        """Condition nodes seen in the event table, first appearance first."""
        ids: list[str] = []
        seen: set[str] = set()
        for row in range(self.events_table.rowCount()):
            item = self.events_table.item(row, 0)
            payload = item.data(EVENT_PAYLOAD) if item is not None else None
            if isinstance(payload, EventNotification) and payload.condition_node_id:
                node_id = payload.condition_node_id
                if node_id not in seen:
                    seen.add(node_id)
                    ids.append(node_id)
        return ids

    def _on_read_condition_states(self) -> None:
        if self._busy or not self._connected:
            return
        node_ids = self._condition_node_ids()
        if not node_ids:
            self._set_status(
                "No condition event is in the log; add a source with ConditionType events first.",
                error=True,
            )
            return
        self._busy = True
        self._update_controls()
        self._set_status(f"Reading the state of {len(node_ids)} condition(s)…")

        def done(snapshot: ConditionStateSnapshot) -> None:
            self._busy = False
            if self._closed:
                return
            self._fill_condition_states(snapshot)
            self._set_status(snapshot.summary_text())
            self._update_controls()

        def failed(message: str) -> None:
            self._busy = False
            if self._closed:
                return
            _logger.warning("Reading condition states failed: %s", message)
            self._set_status(f"Reading condition states failed: {message}", error=True)
            self._update_controls()

        self._runner.submit(
            lambda: self._service.read_condition_states(node_ids),
            on_done=done,
            on_error=failed,
        )

    def _fill_condition_states(self, snapshot: ConditionStateSnapshot) -> None:
        table = self.state_table
        table.setRowCount(len(snapshot.conditions))
        for row, condition in enumerate(snapshot.conditions):
            cells = [
                condition.node_id,
                condition.condition_name,
                condition.acked_text(),
                condition.confirmed_text(),
                condition.enabled_text(),
                condition.retain_text(),
                "" if condition.last_severity is None else str(condition.last_severity),
                condition.status if not condition.missing else "partial: " + ", ".join(condition.missing),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(condition.detail or text)
                table.setItem(row, column, item)

    def _on_export_log(self) -> None:
        """Write the rows the event table currently shows; nothing is re-read."""
        payloads = self._event_payloads()
        if not payloads:
            self._set_status("There is no event to export.", error=True)
            return
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export event log",
            str(Path.home() / "opcua-event-log.json"),
            "JSON document (*.json);;CSV (*.csv)",
        )
        if not path:
            self._set_status("Exporting the event log was cancelled.")
            return
        if not Path(path).suffix:
            path += ".csv" if "CSV" in selected else ".json"
        self._set_status(f"Writing {len(payloads)} event(s) to {path}…")

        def done(written: Any) -> None:
            if self._closed:
                return
            text = (
                f"Exported {len(payloads)} event(s) to {written}; "
                "the table keeps its bounded rows only."
            )
            self._set_status(text)
            self.report_ready.emit(f"Event log exported: {text}")

        self._runner.submit_call(
            write_observation_log,
            path,
            kind="events",
            payloads=payloads,
            endpoint_url=self._endpoint_url,
            on_done=done,
            on_error=lambda message: self._set_status(
                f"Exporting the event log failed: {message}", error=True
            ),
        )

    def _event_payloads(self) -> list[EventNotification]:
        payloads: list[EventNotification] = []
        table = self.events_table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            payload = item.data(EVENT_PAYLOAD) if item is not None else None
            if isinstance(payload, EventNotification):
                payloads.append(payload)
        return payloads

    def _on_max_rows_changed(self, value: int) -> None:
        table = self.events_table
        while table.rowCount() > int(value):
            table.removeRow(0)
        self._update_counters()

    def _on_event_selected(self) -> None:
        rows = {index.row() for index in self.events_table.selectedIndexes()}
        if not rows:
            self.details_view.clear()
            self._selected_notification = None
            self._update_condition_buttons()
            return
        item = self.events_table.item(min(rows), 0)
        notification = item.data(EVENT_PAYLOAD) if item is not None else None
        if not isinstance(notification, EventNotification):
            self.details_view.clear()
            self._selected_notification = None
            self._update_condition_buttons()
            return
        self._selected_notification = notification
        payload = notification.to_dict()
        payload["received_at_local"] = notification.received_at.astimezone().isoformat(timespec="milliseconds")
        payload["received_at_utc"] = iso_utc(notification.received_at)
        self.details_view.setPlainText(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        self._update_condition_buttons()

    # -- condition methods and refresh -------------------------------------
    def _update_condition_buttons(self) -> None:
        notification = self._selected_notification
        has_identity = (
            notification is not None
            and bool(notification.condition_node_id)
            and event_id_bytes(notification.event_id) is not None
        )
        can_call = has_identity and self._connected and not self._condition_busy and not self._busy
        for button in self.condition_buttons.values():
            button.setEnabled(can_call)
        self.refresh_conditions_btn.setEnabled(
            self._connected and self._running and not self._condition_busy and not self._busy
        )
        self.read_states_btn.setEnabled(
            self._connected
            and not self._condition_busy
            and not self._busy
            and bool(self._condition_node_ids())
        )

    def _on_refresh_conditions(self) -> None:
        """Call ConditionRefresh once on the running subscription; no dialog, no retry."""
        if not self._running:
            self._set_status(
                "Start event monitoring first: ConditionRefresh applies to one subscription.", error=True
            )
            return
        self._condition_busy = True
        self._update_condition_buttons()
        self._set_status("Asking the server to refresh the conditions of the running subscription…")

        def done(result: Any) -> None:
            self._condition_busy = False
            self._update_condition_buttons()
            if self._closed:
                return
            self._set_status(result.summary_text(), error=not result.ok)
            self.report_ready.emit(
                " ".join(
                    [
                        result.summary_text(),
                        condition_refresh_text(
                            subscription_id=result.subscription_id,
                            object_node_id=result.object_node_id,
                        ),
                    ]
                )
            )

        def failed(message: str) -> None:
            self._condition_busy = False
            self._update_condition_buttons()
            if self._closed:
                return
            self._set_status(f"ConditionRefresh failed: {message}", error=True)
            self.report_ready.emit(f"ConditionRefresh failed: {message}.")

        self._submit(
            lambda: self._service.refresh_event_conditions(reference="events-tab refresh conditions"),
            on_done=done,
            on_error=failed,
        )

    def _on_condition_action(self, action: str) -> None:
        """Ask for a comment and a confirmation, then send exactly one condition method call."""
        notification = self._selected_notification
        if notification is None:
            self._set_status("Select an event first.", error=True)
            return
        label = CONDITION_ACTION_LABELS.get(action, action)
        if not notification.condition_node_id or event_id_bytes(notification.event_id) is None:
            self._set_status(
                f"This event carries no condition identity or EventId; {label} cannot be called. "
                "Use an event type such as ConditionType when starting the subscription.",
                error=True,
            )
            return

        comment = self._ask_condition_comment(label)
        if comment is None:
            self._set_status(f"{label} was cancelled; nothing was sent.")
            return
        if action == "comment" and not comment.strip():
            self._set_status("A comment cannot be empty; nothing was sent.", error=True)
            return
        text = condition_action_confirmation(
            action=action,
            endpoint_url=self._endpoint_url,
            condition_node_id=notification.condition_node_id,
            event_id=notification.event_id,
            comment=comment,
        )
        if not self._confirm_condition_action(label, text):
            self._set_status(f"{label} was cancelled; nothing was sent.")
            return

        self._condition_busy = True
        self._update_condition_buttons()
        condition_node_id = notification.condition_node_id
        event_id = notification.event_id
        self._set_status(f"Calling {label} on {condition_node_id}…")

        def done(outcome: Any) -> None:
            self._condition_busy = False
            self._update_condition_buttons()
            if self._closed:
                return
            if outcome.status == STATUS_PASSED:
                self._set_status(f"{label} on condition {condition_node_id} was sent once.")
                self.report_ready.emit(
                    condition_action_result_text(
                        action=action, condition_node_id=condition_node_id, event_id=event_id
                    )
                )
                return
            self._set_status(
                f"{label} was not accepted: {outcome.detail or outcome.status}", error=True
            )
            self.report_ready.emit(
                f"{label} on {condition_node_id} was not accepted ({outcome.status}): "
                f"{outcome.detail or 'no detail'}."
            )

        def failed(message: str) -> None:
            self._condition_busy = False
            self._update_condition_buttons()
            if self._closed:
                return
            self._set_status(f"{label} failed: {message}", error=True)
            self.report_ready.emit(f"{label} on {condition_node_id} failed: {message}.")

        self._submit(
            lambda: self._service.condition_action(
                condition_node_id,
                event_id,
                comment,
                action=action,
                reference=f"events-tab {label.lower()} {condition_node_id}",
            ),
            on_done=done,
            on_error=failed,
        )

    def _ask_condition_comment(self, label: str) -> str | None:
        """Ask for the comment argument; ``None`` means the operator cancelled."""
        required = " (required for AddComment)" if label == "Add comment" else " (optional)"
        text, accepted = QInputDialog.getText(
            self,
            f"{label} condition",
            f"Comment{required}, sent as the LocalizedText argument:",
        )
        return str(text) if accepted else None

    def _confirm_condition_action(self, label: str, text: str) -> bool:
        """Ask for explicit approval of the single method call."""
        answer = QMessageBox.question(
            self,
            f"Confirm {label.lower()} condition",
            text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    # -- controls ----------------------------------------------------------
    def _filter_summary(self) -> str:
        """Name the server-side filter the collapsed row is carrying."""
        parts: list[str] = []
        fields = [text for text in self.fields_edit.text().split(",") if text.strip()]
        if fields:
            parts.append(f"{len(fields)} extra field(s)")
        severity = int(self.min_severity_spin.value())
        if severity > 0:
            parts.append(f"severity ≥ {severity}")
        return ", ".join(parts)

    def _update_controls(self) -> None:
        connected = self._connected
        has_sources = self.sources_table.rowCount() > 0
        has_selection = bool(self.sources_table.selectedIndexes())

        self.options.set_active(self._filter_summary())
        self.source_edit.setEnabled(connected and not self._busy)
        self.add_source_btn.setEnabled(connected and not self._busy)
        self.use_selection_btn.setEnabled(connected and not self._busy)
        self.remove_source_btn.setEnabled(connected and has_selection and not self._busy)
        self.clear_sources_btn.setEnabled(connected and not self._running)
        self.event_type_edit.setEnabled(not self._running)
        self.publishing_spin.setEnabled(not self._running)
        self.fields_edit.setEnabled(not self._running)
        self.min_severity_spin.setEnabled(not self._running)
        self.start_btn.setEnabled(connected and has_sources and not self._running and not self._busy)
        self.stop_btn.setEnabled(self._running and not self._busy)
        self.read_history_btn.setEnabled(connected and not self._busy)
        self.history_from_check.setEnabled(connected and not self._busy)
        self.history_to_check.setEnabled(connected and not self._busy)
        self.history_from_edit.setEnabled(
            connected and not self._busy and self.history_from_check.isChecked()
        )
        self.history_to_edit.setEnabled(
            connected and not self._busy and self.history_to_check.isChecked()
        )
        self.history_max_spin.setEnabled(connected and not self._busy)
        self._update_condition_buttons()

    def _update_counters(self) -> None:
        sources = self.sources_table.rowCount()
        self.export_log_btn.setEnabled(self.events_table.rowCount() > 0)
        parts = [f"{self._total_events} event(s)", f"{sources} source(s)"]
        if self._stored_events:
            parts.append(f"{self._stored_events} stored")
        if self._subscription_id is not None:
            parts.append(f"subscription {self._subscription_id}")
        self.counters_label.setText(" · ".join(parts))


