"""Read-only aggregation of the live monitors of every session.

The per-session **Live monitor** tab owns its subscription (add, start, stop,
remove). This tab only *observes*: it listens to every session's monitor and
shows one merged table of monitored items plus one merged notification log,
with a session column and a session filter. It never creates, changes or deletes
a subscription, so there is exactly one owner per subscription.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...models import MonitorNotification, iso_utc
from ..theme import ERROR, mono_font
from .base import SessionTab
from .monitor_tab import DEFAULT_MAX_ROWS, MonitorTab

_logger = logging.getLogger(__name__)

SESSION_CELL_KEY = Qt.UserRole + 11
NOTIFICATION_PAYLOAD = Qt.UserRole + 12
_VALUE_CELL_CHARS = 300
_LATEST_CELL_CHARS = 120


def _is_bad_status(status: str) -> bool:
    lowered = status.lower()
    return lowered.startswith("bad") or lowered.startswith(("rejected", "failed", "disabled", "invalid"))


class UnifiedMonitorTab(SessionTab):
    """Merged, read-only view of all sessions' monitored items and notifications."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._closed = False
        self._paused = False
        self._dropped = 0
        self._total_notifications = 0
        #: workspace key -> {title, monitor, notify callback, items callback}
        self._sessions: dict[Any, dict[str, Any]] = {}
        #: (workspace key, node_id) -> {status, value, timestamp, count}
        self._items: dict[tuple[Any, str], dict[str, Any]] = {}
        #: (workspace key, node_id) -> last notification (for the details pane)
        self._latest: dict[tuple[Any, str], MonitorNotification] = {}
        #: (workspace key, node_id) -> items-table row
        self._rows: dict[tuple[Any, str], int] = {}
        self._filter_key: Any | None = None

        self._build_ui()
        self._reload_filter()
        self._update_counters()

    # -- registration ------------------------------------------------------
    def register(self, key: Any, title: str, monitor: MonitorTab) -> None:
        """Start observing one session's monitor tab."""
        if key in self._sessions:
            return
        notify = partial(self._on_notification, key)
        items = partial(self._on_items_changed, key)
        monitor.notification_received.connect(notify)
        monitor.items_changed.connect(items)
        self._sessions[key] = {"title": title, "monitor": monitor, "notify": notify, "items": items}
        _logger.info("Aggregating live monitor of session %r", title)
        self._reload_filter()
        self._sync(key)

    def unregister(self, key: Any) -> None:
        """Stop observing a session and drop its monitored-item rows."""
        entry = self._sessions.pop(key, None)
        if entry is None:
            return
        monitor: MonitorTab = entry["monitor"]
        for signal, callback in ((monitor.notification_received, entry["notify"]), (monitor.items_changed, entry["items"])):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):  # pragma: no cover - already gone
                _logger.debug("Disconnecting a unified-monitor callback failed", exc_info=True)
        for item_key in [item_key for item_key in self._items if item_key[0] is key]:
            self._items.pop(item_key, None)
            self._latest.pop(item_key, None)
        if self._filter_key is key:
            self._filter_key = None
        self._reload_filter()
        self._rebuild_items()
        self._hide_log_rows()
        self._update_counters()
        self._set_status(f"Session {entry['title']} was closed; its monitored items were removed from this view.")

    def set_session_title(self, key: Any, title: str) -> None:
        entry = self._sessions.get(key)
        if entry is None or entry["title"] == title:
            return
        entry["title"] = title
        self._reload_filter()
        self._rebuild_items()
        for row in range(self.notifications_table.rowCount()):
            item = self.notifications_table.item(row, 0)
            if item is not None and item.data(SESSION_CELL_KEY) is key:
                session_item = self.notifications_table.item(row, 1)
                if session_item is not None:
                    session_item.setText(title)

    def shutdown(self) -> None:
        super().shutdown()
        for key in list(self._sessions):
            self.unregister(key)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("All monitors")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Merged view of every session's live monitor. This page is read-only: add, start, stop and remove "
            "monitored items in the session's own Live monitor tab. Each row and log entry is labelled with the "
            "session it came from; identical NodeIds on different servers stay separate."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.filter_combo = QComboBox()
        self.filter_combo.setMinimumWidth(200)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.pause_btn = QPushButton("Pause log")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        self.clear_btn = QPushButton("Clear log")
        self.clear_btn.clicked.connect(self._on_clear_notifications)
        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(100, 50_000)
        self.max_rows_spin.setSingleStep(100)
        self.max_rows_spin.setValue(DEFAULT_MAX_ROWS)
        self.max_rows_spin.setToolTip("Rows kept in the merged notification log; the oldest rows are dropped first.")
        self.max_rows_spin.valueChanged.connect(self._on_max_rows_changed)
        controls.addWidget(QLabel("Session:"))
        controls.addWidget(self.filter_combo)
        controls.addSpacing(12)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.clear_btn)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Keep rows:"))
        controls.addWidget(self.max_rows_spin)
        controls.addStretch(1)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Monitored items of all sessions — latest value (updated in place)"))
        self.items_table = QTableWidget(0, 6)
        self.items_table.setHorizontalHeaderLabels(
            ["Session", "NodeId", "Status", "Latest value", "Source timestamp", "Count"]
        )
        self.items_table.verticalHeader().setVisible(False)
        self.items_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.items_table.setAlternatingRowColors(True)
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.items_table.itemSelectionChanged.connect(self._on_item_selected)
        header = self.items_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.items_table.setMinimumWidth(480)
        left_layout.addWidget(self.items_table, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Notifications of all sessions (newest last)"))
        self.notifications_table = QTableWidget(0, 5)
        self.notifications_table.setHorizontalHeaderLabels(["Received", "Session", "NodeId", "Status", "Value"])
        self.notifications_table.verticalHeader().setVisible(False)
        self.notifications_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.notifications_table.setAlternatingRowColors(True)
        self.notifications_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.notifications_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.notifications_table.itemSelectionChanged.connect(self._on_notification_selected)
        log_header = self.notifications_table.horizontalHeader()
        log_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        log_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        log_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        log_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        log_header.setSectionResizeMode(4, QHeaderView.Stretch)
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

        self.status_label = QLabel("No session has an active monitor yet.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.counters_label = QLabel("")
        self.counters_label.setObjectName("hintLabel")
        layout.addWidget(self.counters_label)

    # -- session bookkeeping ----------------------------------------------
    def _matches(self, key: Any) -> bool:
        return self._filter_key is None or self._filter_key is key

    def _reload_filter(self) -> None:
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItem("All sessions", None)
        for key, entry in self._sessions.items():
            self.filter_combo.addItem(str(entry["title"]), key)
        index = 0
        for candidate in range(self.filter_combo.count()):
            if self.filter_combo.itemData(candidate) is self._filter_key:
                index = candidate
                break
        self.filter_combo.setCurrentIndex(index)
        self.filter_combo.blockSignals(False)
        self._filter_key = self.filter_combo.itemData(index)

    def _on_filter_changed(self, index: int) -> None:
        self._filter_key = self.filter_combo.itemData(index)
        self._rebuild_items()
        self._hide_log_rows()

    def _sync(self, key: Any) -> None:
        """Reconcile one session's item list with its monitor tab's public snapshot."""
        entry = self._sessions.get(key)
        if entry is None:
            return
        seen: set[str] = set()
        for node_id, status, value, timestamp in entry["monitor"].item_snapshot():
            seen.add(node_id)
            state = self._items.get((key, node_id))
            if state is None:
                self._items[(key, node_id)] = {
                    "status": status,
                    "value": value,
                    "timestamp": timestamp,
                    "count": 0,
                }
                continue
            if status:
                state["status"] = status
            if not state["value"] and value:
                state["value"] = value
                state["timestamp"] = timestamp
        for item_key in [item_key for item_key in self._items if item_key[0] is key and item_key[1] not in seen]:
            self._items.pop(item_key, None)
            self._latest.pop(item_key, None)
        self._rebuild_items()
        self._update_counters()

    def _on_items_changed(self, key: Any) -> None:
        if not self._closed:
            self._sync(key)

    # -- notifications -----------------------------------------------------
    def _on_notification(self, key: Any, notification: MonitorNotification) -> None:
        if self._closed:
            return
        self._total_notifications += 1
        item_key = (key, notification.node_id)
        state = self._items.get(item_key)
        if state is None:
            state = {"status": notification.status, "value": "", "timestamp": "", "count": 0}
            self._items[item_key] = state
            self._rebuild_items()
        state["count"] += 1
        state["status"] = notification.status
        value_text = notification.value_text
        if len(value_text) > _LATEST_CELL_CHARS:
            value_text = value_text[: _LATEST_CELL_CHARS - 1] + "…"
        state["value"] = value_text
        state["timestamp"] = notification.source_timestamp or ""
        self._latest[item_key] = notification

        row = self._rows.get(item_key)
        if row is not None:
            self._update_item_row(row, key, notification.node_id, state)

        # Pause freezes the merged log only; the per-item latest values keep updating.
        if self._paused:
            self._dropped += 1
            self._update_counters()
            return
        self._append_log_row(key, notification)
        self._update_counters()

    def _append_log_row(self, key: Any, notification: MonitorNotification) -> None:
        table = self.notifications_table
        bar = table.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2

        row = table.rowCount()
        table.insertRow(row)
        title = self._sessions[key]["title"] if key in self._sessions else "?"
        received = notification.received_at.astimezone().strftime("%H:%M:%S.%f")[:-3]
        value_text = notification.value_text
        if len(value_text) > _VALUE_CELL_CHARS:
            value_text = value_text[: _VALUE_CELL_CHARS - 1] + "…"
        cells = [received, title, notification.node_id, notification.status, value_text]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if column == 0:
                item.setData(NOTIFICATION_PAYLOAD, notification)
                item.setData(SESSION_CELL_KEY, key)
            if column == 4:
                item.setFont(mono_font(9))
                item.setToolTip(notification.value_text)
            if column == 3 and _is_bad_status(notification.status):
                item.setForeground(QColor(ERROR))
            table.setItem(row, column, item)

        table.setRowHidden(row, not self._matches(key))
        while table.rowCount() > int(self.max_rows_spin.value()):
            table.removeRow(0)
        if follow:
            table.scrollToBottom()

    # -- rendering ---------------------------------------------------------
    def _update_item_row(self, row: int, key: Any, node_id: str, state: dict[str, Any]) -> None:
        table = self.items_table
        if row >= table.rowCount() or (key, node_id) not in self._rows:
            self._rebuild_items()
            return
        title = self._sessions[key]["title"] if key in self._sessions else "?"
        session_item = table.item(row, 0)
        if session_item is not None:
            session_item.setText(title)
        status_item = table.item(row, 2)
        if status_item is not None:
            status_item.setText(state["status"])
            status_item.setForeground(QColor(ERROR) if _is_bad_status(state["status"]) else QColor())
        value_item = table.item(row, 3)
        if value_item is not None:
            value_item.setText(state["value"])
            latest = self._latest.get((key, node_id))
            if latest is not None:
                value_item.setToolTip(latest.value_text)
        time_item = table.item(row, 4)
        if time_item is not None:
            time_item.setText(state["timestamp"])
        count_item = table.item(row, 5)
        if count_item is not None:
            count_item.setText(str(state["count"]))

    def _rebuild_items(self) -> None:
        table = self.items_table
        table.blockSignals(True)
        table.setRowCount(0)
        self._rows.clear()
        for (key, node_id), state in self._items.items():
            if key not in self._sessions:
                continue
            row = table.rowCount()
            table.insertRow(row)
            cells = [
                str(self._sessions[key]["title"]),
                node_id,
                state["status"],
                state["value"],
                state["timestamp"],
                str(state["count"]),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column in (1, 3):
                    item.setFont(mono_font(9))
                if column == 2 and _is_bad_status(state["status"]):
                    item.setForeground(QColor(ERROR))
                table.setItem(row, column, item)
            self._rows[(key, node_id)] = row
            table.setRowHidden(row, not self._matches(key))
        table.blockSignals(False)

    def _hide_log_rows(self) -> None:
        for row in range(self.notifications_table.rowCount()):
            item = self.notifications_table.item(row, 0)
            key = item.data(SESSION_CELL_KEY) if item is not None else None
            self.notifications_table.setRowHidden(row, not self._matches(key))

    # -- details -----------------------------------------------------------
    def _show_payload(self, notification: MonitorNotification, *, title: str = "") -> None:
        payload = notification.to_dict()
        if notification.truncated:
            payload["value_preview"] = notification.value_text
        payload["received_at_local"] = notification.received_at.astimezone().isoformat(timespec="milliseconds")
        payload["received_at_utc"] = iso_utc(notification.received_at)
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        self.details_view.setPlainText(f"{title}\n{text}" if title else text)

    def _on_item_selected(self) -> None:
        rows = {index.row() for index in self.items_table.selectedIndexes()}
        if len(rows) != 1:
            return
        row = min(rows)
        session_item = self.items_table.item(row, 0)
        node_item = self.items_table.item(row, 1)
        if node_item is None:
            return
        node_id = node_item.text()
        key = self._key_of_row(row)
        if key is None:
            return
        latest = self._latest.get((key, node_id))
        title = f"Latest value for {node_id} ({session_item.text() if session_item else '?'})"
        if latest is None:
            self.details_view.setPlainText(f"{title}\nNo notification received yet.")
            return
        self._show_payload(latest, title=title)

    def _key_of_row(self, row: int) -> Any | None:
        for (key, _node_id), row_index in self._rows.items():
            if row_index == row:
                return key
        return None

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
    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._dropped = 0
            self._set_status("Paused: new rows are not added to the merged log (latest values keep updating).")
        else:
            dropped = self._dropped
            self._dropped = 0
            self._set_status(
                "Resumed."
                if not dropped
                else f"Resumed; {dropped} notification(s) were not added to the merged log while paused."
            )
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

    def _update_counters(self) -> None:
        parts = [
            f"{len(self._sessions)} session(s)",
            f"{len(self._items)} monitored item(s)",
            f"{self._total_notifications} notification(s)",
        ]
        if self._dropped:
            parts.append(f"{self._dropped} not added while paused")
        self.counters_label.setText(" · ".join(parts))
        if not self._items:
            self._set_status(
                "No monitored items yet. Add nodes and start the subscription in a session's Live monitor tab."
            )
