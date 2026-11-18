"""Events tab: one event subscription on explicitly added source nodes.

Nothing is subscribed implicitly. The operator adds one or more source NodeIds
(usually the Server object), starts one subscription, and sees the events the
server publishes as a bounded log. Every selected event can be inspected field
by field.

The tab never writes a variable. Its one action is an operator-confirmed
``Acknowledge`` method call on a condition event, sent once through
``write_service``; it never calls ``Confirm``, ``Enable`` or anything else and
never changes a monitoring mode.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
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

from ...defaults import DEFAULT_EVENT_KEEP_ROWS, DEFAULT_EVENT_PUBLISHING_INTERVAL_MS
from ...errors import InvalidNodeIdError
from ...models import (
    STATUS_PASSED,
    EventConfig,
    EventNotification,
    EventSourceInfo,
    EventStart,
    MonitorStatusEvent,
    iso_utc,
)
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...opcua.event_service import SERVER_OBJECT_NODE_ID, event_id_bytes
from ...operator_text import acknowledge_condition_confirmation
from ..async_runner import AsyncRunner, TaskHandle
from ..theme import ERROR, mono_font

_logger = logging.getLogger(__name__)

SOURCE_NODE_ID = Qt.UserRole + 11
SOURCE_HANDLE = Qt.UserRole + 12
EVENT_PAYLOAD = Qt.UserRole + 13

_MAX_MESSAGE_CELL_CHARS = 300
_MAX_TIME_CELL_CHARS = 32


class EventsTab(QWidget):
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
        self._selection_provider = selection_provider
        self._running = False
        self._busy = False
        self._closed = False
        self._events_key: str | None = None
        #: In-flight operations by task key, so shutdown can cancel them.
        self._tasks: dict[str, TaskHandle] = {}
        self._subscription_id: int | None = None
        self._total_events = 0
        self._selected_notification: EventNotification | None = None
        self._ack_busy = False

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
        subtitle = QLabel(
            "One event subscription on the source nodes you add explicitly (usually the Server object). The "
            "server decides what it publishes; the fields come from the selected event type. For condition "
            "events choose an event type such as ConditionType (i=2782) or AcknowledgeableConditionType "
            "(i=2881), then a selected event can be acknowledged: one confirmed Acknowledge method call, "
            "sent once. No variable is written and no other condition method is called."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

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
        layout.addLayout(source_row)

        controls = QHBoxLayout()
        self.event_type_edit = QLineEdit()
        self.event_type_edit.setPlaceholderText("BaseEventType — every event (default)")
        self.event_type_edit.setToolTip(
            "Event type whose fields are requested. Empty means BaseEventType (all events, standard fields). "
            "A custom event type adds its own properties to the detail view."
        )
        self.publishing_spin = QDoubleSpinBox()
        self.publishing_spin.setRange(100.0, 60_000.0)
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
        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(100, 50_000)
        self.max_rows_spin.setSingleStep(100)
        self.max_rows_spin.setValue(DEFAULT_EVENT_KEEP_ROWS)
        self.max_rows_spin.setToolTip("Rows kept in the event log; the oldest rows are dropped first.")
        self.max_rows_spin.valueChanged.connect(self._on_max_rows_changed)

        controls.addWidget(QLabel("Event type:"))
        controls.addWidget(self.event_type_edit, 1)
        controls.addWidget(QLabel("Publishing:"))
        controls.addWidget(self.publishing_spin)
        controls.addSpacing(12)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        controls.addSpacing(12)
        controls.addWidget(self.clear_log_btn)
        controls.addWidget(QLabel("Keep rows:"))
        controls.addWidget(self.max_rows_spin)
        layout.addLayout(controls)

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
        self.acknowledge_btn = QPushButton("Acknowledge condition…")
        self.acknowledge_btn.setToolTip(
            "Calls the condition's Acknowledge method once, after an explicit confirmation. Needs an event "
            "subscription whose event type selects condition fields (ConditionType or a subtype)."
        )
        self.acknowledge_btn.clicked.connect(self._on_acknowledge)
        self.acknowledge_btn.setEnabled(False)
        details_header.addWidget(self.acknowledge_btn)
        details_layout.addLayout(details_header)
        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setFont(mono_font(9))
        self.details_view.setPlaceholderText("Select an event to see all of its fields.")
        details_layout.addWidget(self.details_view, 1)
        splitter.addWidget(details_box)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("Not connected.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.counters_label = QLabel("0 event(s) · 0 source(s)")
        self.counters_label.setObjectName("hintLabel")
        layout.addWidget(self.counters_label)

    # -- connection state --------------------------------------------------
    def set_connected(self, connected: bool) -> None:
        if not connected:
            self._detach_listener()
            self._running = False
            self._busy = False
            self._subscription_id = None
            self._selected_notification = None
            self._ack_busy = False
            self.sources_table.setRowCount(0)
            self.events_table.setRowCount(0)
            self.details_view.clear()
            self._total_events = 0
            self._set_status("Not connected. Use the Connection tab to open a session.")
            self._update_counters()
            self.sources_changed.emit()
        self._update_controls()

    def shutdown(self) -> None:
        """Stop receiving events and cancel work; the service deletes the subscription."""
        self._closed = True
        self._detach_listener()
        pending = list(self._tasks.values())
        self._tasks.clear()
        for handle in pending:
            handle.cancel()

    def _detach_listener(self) -> None:
        if self._events_key is not None:
            self._runner.unlisten(self._events_key)
            self._events_key = None

    # -- configuration -----------------------------------------------------
    def _config(self) -> EventConfig:
        return EventConfig(
            publishing_interval_ms=float(self.publishing_spin.value()),
            event_type_node_id=self.event_type_edit.text().strip(),
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
        if not self._service.is_connected:
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
        if not self._service.is_connected:
            self._set_status("Connect to a server first.", error=True)
            return

        self._busy = True
        self._update_controls()
        config = self._config()
        key = self._runner.new_key()
        self._detach_listener()
        self._events_key = key
        self._runner.listen(key, self._on_progress)
        self._set_status(f"Creating an event subscription for {len(source_ids)} source(s)…")
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
        cells = [
            notification.received_at.astimezone().strftime("%H:%M:%S.%f")[:-3],
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
        self.details_view.clear()
        self._selected_notification = None
        self._total_events = 0
        self._update_ack_button()
        self._update_counters()

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
            self._update_ack_button()
            return
        item = self.events_table.item(min(rows), 0)
        notification = item.data(EVENT_PAYLOAD) if item is not None else None
        if not isinstance(notification, EventNotification):
            self.details_view.clear()
            self._selected_notification = None
            self._update_ack_button()
            return
        self._selected_notification = notification
        payload = notification.to_dict()
        payload["received_at_local"] = notification.received_at.astimezone().isoformat(timespec="milliseconds")
        payload["received_at_utc"] = iso_utc(notification.received_at)
        self.details_view.setPlainText(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        self._update_ack_button()

    # -- condition acknowledgement ----------------------------------------
    def _update_ack_button(self) -> None:
        notification = self._selected_notification
        can_acknowledge = (
            notification is not None
            and bool(notification.condition_node_id)
            and event_id_bytes(notification.event_id) is not None
            and self._service.is_connected
            and not self._ack_busy
            and not self._busy
        )
        self.acknowledge_btn.setEnabled(can_acknowledge)

    def _on_acknowledge(self) -> None:
        notification = self._selected_notification
        if notification is None:
            self._set_status("Select an event first.", error=True)
            return
        if not notification.condition_node_id or event_id_bytes(notification.event_id) is None:
            self._set_status(
                "This event carries no condition identity or EventId; it cannot be acknowledged. "
                "Use an event type such as ConditionType when starting the subscription.",
                error=True,
            )
            return

        comment = self._ask_acknowledge_comment()
        if comment is None:
            self._set_status("Acknowledging the condition was cancelled; nothing was sent.")
            return
        text = acknowledge_condition_confirmation(
            endpoint_url=self._service.endpoint_url,
            condition_node_id=notification.condition_node_id,
            event_id=notification.event_id,
            comment=comment,
        )
        if not self._confirm_acknowledge(text):
            self._set_status("Acknowledging the condition was cancelled; nothing was sent.")
            return

        self._ack_busy = True
        self._update_ack_button()
        condition_node_id = notification.condition_node_id
        self._set_status(f"Calling Acknowledge on {condition_node_id}…")

        def done(outcome: Any) -> None:
            self._ack_busy = False
            self._update_ack_button()
            if outcome.status == STATUS_PASSED:
                self._set_status(f"Condition {condition_node_id} acknowledged.")
                self.report_ready.emit(
                    f"Acknowledge on {condition_node_id} was sent once (EventId {notification.event_id}); "
                    "no Confirm/Enable call was made and the acknowledgement is not proof the condition cleared."
                )
                return
            self._set_status(
                f"Acknowledge was not accepted: {outcome.detail or outcome.status}", error=True
            )
            self.report_ready.emit(
                f"Acknowledge on {condition_node_id} was not accepted ({outcome.status}): "
                f"{outcome.detail or 'no detail'}."
            )

        def failed(message: str) -> None:
            self._ack_busy = False
            self._update_ack_button()
            self._set_status(f"Acknowledge failed: {message}", error=True)
            self.report_ready.emit(f"Acknowledge on {condition_node_id} failed: {message}.")

        self._submit(
            lambda: self._service.acknowledge_condition(
                condition_node_id,
                notification.event_id,
                comment,
                reference=f"events-tab {condition_node_id}",
            ),
            on_done=done,
            on_error=failed,
        )

    def _ask_acknowledge_comment(self) -> str | None:
        """Ask for the Acknowledge comment; ``None`` means the operator cancelled."""
        text, accepted = QInputDialog.getText(
            self, "Acknowledge condition", "Comment (optional, sent as the LocalizedText argument):"
        )
        return str(text) if accepted else None

    def _confirm_acknowledge(self, text: str) -> bool:
        """Ask for explicit approval of the single Acknowledge call."""
        answer = QMessageBox.question(
            self,
            "Confirm condition acknowledgement",
            text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    # -- controls ----------------------------------------------------------
    def _update_controls(self) -> None:
        connected = self._service.is_connected
        has_sources = self.sources_table.rowCount() > 0
        has_selection = bool(self.sources_table.selectedIndexes())

        self.source_edit.setEnabled(connected and not self._busy)
        self.add_source_btn.setEnabled(connected and not self._busy)
        self.use_selection_btn.setEnabled(connected and not self._busy)
        self.remove_source_btn.setEnabled(connected and has_selection and not self._busy)
        self.clear_sources_btn.setEnabled(connected and not self._running)
        self.event_type_edit.setEnabled(not self._running)
        self.publishing_spin.setEnabled(not self._running)
        self.start_btn.setEnabled(connected and has_sources and not self._running and not self._busy)
        self.stop_btn.setEnabled(self._running and not self._busy)
        self._update_ack_button()

    def _update_counters(self) -> None:
        sources = self.sources_table.rowCount()
        parts = [f"{self._total_events} event(s)", f"{sources} source(s)"]
        if self._subscription_id is not None:
            parts.append(f"subscription {self._subscription_id}")
        self.counters_label.setText(" · ".join(parts))

    # -- background work ---------------------------------------------------
    def _submit(
        self,
        coro: Callable[[], Awaitable[Any]],
        *,
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None],
        key: str | None = None,
    ) -> None:
        """Submit one operation and remember its handle so ``shutdown()`` can cancel it."""
        task_key = key or self._runner.new_key()
        handle = self._runner.submit(
            coro,
            key=task_key,
            on_done=lambda result: self._finish(task_key, on_done, result),
            on_error=lambda message: self._finish(task_key, on_error, message),
        )
        self._tasks[task_key] = handle

    def _finish(self, key: str, callback: Callable[[Any], None], payload: Any) -> None:
        self._tasks.pop(key, None)
        if self._closed:
            return
        callback(payload)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(("⚠ " if error else "") + message)
        self.status_label.setObjectName("errorLabel" if error else "hintLabel")
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)
