"""Lazy address-space browser with attribute, reference and value panels."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...errors import InvalidNodeIdError
from ...models import AddressSpace, AttributeResult, NodeDetails, ValueSnapshot
from ...opcua.attributes import format_attribute_value
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ..async_runner import AsyncRunner
from ..theme import ACCENT, mono_font

_logger = logging.getLogger(__name__)

ITEM_NODE_ID = Qt.UserRole + 1
ITEM_LOADED = Qt.UserRole + 2
ITEM_BASE_TEXT = Qt.UserRole + 3
ITEM_BASE_TOOLTIP = Qt.UserRole + 4

#: Prefix of a tree row whose node is in this session's live monitor and subscribed.
MONITOR_MARK_RUNNING = "● "
#: Prefix of a tree row whose node is in the monitor list but not subscribed yet.
MONITOR_MARK_PENDING = "○ "

START_NODES = [
    ("Root folder (i=84)", "i=84"),
    ("Objects folder (i=85)", "i=85"),
    ("Server object (i=2253)", "i=2253"),
    ("Custom NodeId…", ""),
]


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):  # pragma: no cover - serializer output is JSON-safe
            return str(value)
    return format_attribute_value(value)


class BrowserTab(QWidget):
    """On-demand tree, attribute table, reference table and value view."""

    download_requested = Signal(str)
    monitor_requested = Signal(str)

    def __init__(self, runner: AsyncRunner, service: OpcUaService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        self._loading: set[str] = set()
        self._detail_token = 0
        self._snapshot: AddressSpace | None = None
        self._matches: list[str] = []
        self._match_index = 0
        self._current_node_id: str | None = None
        #: NodeIds in this session's live-monitor list, marked with ●/○ in the tree.
        self._monitored: set[str] = set()
        self._monitor_active = False

        self._build_ui()
        self._on_start_changed()
        self.set_connected(False)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Address space")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Loading happens on demand. The address space is a graph, not a tree: a node referenced from several "
            "parents appears under each of them, while duplicate references from the same parent are merged into "
            "one row. Nodes in this session's live monitor are prefixed with ● while the subscription runs and "
            "with ○ when they are added but not subscribed yet."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        toolbar = QHBoxLayout()
        self.start_combo = QComboBox()
        for label, node_id in START_NODES:
            self.start_combo.addItem(label, node_id)
        self.start_combo.setCurrentIndex(1)
        self.start_combo.currentIndexChanged.connect(self._on_start_changed)
        self.node_id_edit = QLineEdit()
        self.node_id_edit.setPlaceholderText("ns=2;s=Plant")
        self.node_id_edit.returnPressed.connect(self._on_load_node)
        self.load_btn = QPushButton("Load")
        self.load_btn.clicked.connect(self._on_load_node)
        self.copy_btn = QPushButton("Copy NodeId")
        self.copy_btn.clicked.connect(self._on_copy_node_id)
        self.monitor_btn = QPushButton("Monitor node")
        self.monitor_btn.setToolTip("Add the selected node to the Live monitor tab.")
        self.monitor_btn.clicked.connect(self._on_monitor_requested)
        toolbar.addWidget(QLabel("Start:"))
        toolbar.addWidget(self.start_combo)
        toolbar.addWidget(self.node_id_edit, 2)
        toolbar.addWidget(self.load_btn)
        toolbar.addWidget(self.copy_btn)
        toolbar.addWidget(self.monitor_btn)
        toolbar.addSpacing(16)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search downloaded nodes…")
        self.search_edit.returnPressed.connect(self._on_find)
        self.find_btn = QPushButton("Find")
        self.find_btn.clicked.connect(self._on_find)
        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(self._on_next_match)
        toolbar.addWidget(self.search_edit, 2)
        toolbar.addWidget(self.find_btn)
        toolbar.addWidget(self.next_btn)
        toolbar.addSpacing(16)
        self.download_btn = QPushButton("Download from here")
        self.download_btn.setObjectName("primary")
        self.download_btn.clicked.connect(self._on_download_requested)
        toolbar.addWidget(self.download_btn)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Node class", "NodeId"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.currentItemChanged.connect(self._on_current_item_changed)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        splitter.addWidget(self.tree)

        splitter.addWidget(self._build_details())
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 6)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("Not connected.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_details(self) -> QWidget:
        self.details_tabs = QTabWidget()

        self.attributes_table = QTableWidget(0, 3)
        self.attributes_table.setHorizontalHeaderLabels(["Attribute", "Value", "Status"])
        self.attributes_table.verticalHeader().setVisible(False)
        self.attributes_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.attributes_table.setAlternatingRowColors(True)
        self.attributes_table.setWordWrap(True)
        self.attributes_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.attributes_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.attributes_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.details_tabs.addTab(self.attributes_table, "Attributes")

        self.references_table = QTableWidget(0, 5)
        self.references_table.setHorizontalHeaderLabels(
            ["Direction", "Reference type", "Target", "Target NodeId", "Node class"]
        )
        self.references_table.verticalHeader().setVisible(False)
        self.references_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.references_table.setAlternatingRowColors(True)
        self.references_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.references_table.doubleClicked.connect(self._on_reference_activated)
        self.details_tabs.addTab(self.references_table, "References")

        value_page = QWidget()
        value_layout = QVBoxLayout(value_page)
        value_layout.setContentsMargins(8, 8, 8, 8)
        row = QHBoxLayout()
        self.read_value_btn = QPushButton("Read value")
        self.read_value_btn.clicked.connect(self._on_read_value)
        self.value_meta = QLabel("No value read.")
        self.value_meta.setObjectName("hintLabel")
        self.value_meta.setWordWrap(True)
        row.addWidget(self.read_value_btn)
        row.addWidget(self.value_meta, 1)
        value_layout.addLayout(row)

        self.value_warning = QLabel()
        self.value_warning.setObjectName("warningLabel")
        self.value_warning.setWordWrap(True)
        self.value_warning.hide()
        value_layout.addWidget(self.value_warning)

        self.value_view = QPlainTextEdit()
        self.value_view.setReadOnly(True)
        self.value_view.setFont(mono_font(9))
        self.value_view.setPlaceholderText(
            "Values are read only when you press 'Read value'. Exports exclude values unless enabled."
        )
        value_layout.addWidget(self.value_view, 1)
        self.details_tabs.addTab(value_page, "Value")

        return self.details_tabs

    # -- connection state --------------------------------------------------
    def set_connected(self, connected: bool, connection: Any = None) -> None:
        self.load_btn.setEnabled(connected)
        self.download_btn.setEnabled(connected)
        self.read_value_btn.setEnabled(connected)
        self.monitor_btn.setEnabled(connected)
        if not connected:
            self.tree.clear()
            self._loading.clear()
            self._current_node_id = None
            self._clear_details()
            self.status_label.setText("Not connected. Use the Connection tab to open a session.")
        elif self._service.is_connected:
            self.load_start_node()

    def set_snapshot(self, snapshot: AddressSpace | None) -> None:
        """Receive the most recent download so search works offline."""
        self._snapshot = snapshot
        if snapshot is None:
            return
        self.status_label.setText(
            f"{len(snapshot.nodes)} node(s) in the last download are searchable. "
            "Reference edges are preserved separately from the tree."
        )

    # -- tree --------------------------------------------------------------
    def _on_start_changed(self) -> None:
        custom = not bool(self.start_combo.currentData())
        self.node_id_edit.setEnabled(custom)
        if custom:
            self.node_id_edit.setFocus()

    def _on_load_node(self) -> None:
        node_id = self.start_combo.currentData()
        if not node_id:
            node_id = self.node_id_edit.text().strip()
        if not node_id:
            self._set_status("Enter a NodeId such as ns=2;s=Plant.", error=True)
            return
        self.load_node(str(node_id))

    def load_start_node(self) -> None:
        node_id = self.start_combo.currentData() or self.node_id_edit.text().strip()
        if node_id:
            self.load_node(str(node_id))

    def start_node(self) -> str:
        """The configured start NodeId (fixed entry or a custom one)."""
        return str(self.start_combo.currentData() or self.node_id_edit.text().strip() or "i=85")

    def apply_start_node(self, node_id: str | None) -> None:
        """Restore the start NodeId; it is loaded on the next connect."""
        if not node_id:
            return
        index = self.start_combo.findData(node_id)
        if index >= 0:
            self.start_combo.setCurrentIndex(index)
            self.node_id_edit.clear()
            return
        index = self.start_combo.findData("")
        if index >= 0:
            self.start_combo.setCurrentIndex(index)
        self.node_id_edit.setText(node_id)

    def load_node(self, node_id: str) -> None:
        try:
            canonical = parse_node_id(node_id).to_string()
        except InvalidNodeIdError as exc:
            self._set_status(str(exc), error=True)
            return
        if not self._service.is_connected:
            self._set_status("Connect to a server first.", error=True)
            return

        self.tree.clear()
        item = self._make_item(canonical, canonical, "")
        self.tree.addTopLevelItem(item)
        item.setExpanded(True)
        self.tree.setCurrentItem(item)
        self._set_status(f"Loading {canonical}…")

    def _make_item(self, display: str, node_id: str, node_class: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([display, node_class, node_id])
        item.setData(0, ITEM_NODE_ID, node_id)
        item.setData(0, ITEM_LOADED, False)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        self._decorate_item(item, node_id)
        return item

    # -- monitored-node marking -------------------------------------------
    def set_monitored_nodes(self, node_ids: Iterable[str], *, active: bool) -> None:
        """Mark the rows of nodes in this session's live monitor.

        ``active`` is True while the session's subscription exists; those rows get ●,
        nodes that are only queued get ○.
        """
        normalized = {str(node_id) for node_id in node_ids}
        if normalized == self._monitored and active == self._monitor_active:
            return
        self._monitored = normalized
        self._monitor_active = bool(active)
        self._apply_monitored_marks()
        count = len(self._monitored)
        if count:
            state = "subscribed" if self._monitor_active else "not subscribed"
            self._set_status(f"{count} live-monitor target(s) marked in the address space ({state}).")

    def is_monitored(self, node_id: str) -> bool:
        return str(node_id) in self._monitored

    def _apply_monitored_marks(self) -> None:
        stack = [self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            node_id = item.data(0, ITEM_NODE_ID)
            if node_id:
                self._decorate_item(item, str(node_id))
            stack.extend(item.child(index) for index in range(item.childCount()))

    def _decorate_item(self, item: QTreeWidgetItem, node_id: str) -> None:
        """Apply or remove the monitor marker without losing the original text and tooltip."""
        base_text = item.data(0, ITEM_BASE_TEXT)
        if not isinstance(base_text, str):
            base_text = item.text(0)
            for mark in (MONITOR_MARK_RUNNING, MONITOR_MARK_PENDING):
                if base_text.startswith(mark):
                    base_text = base_text[len(mark):]
            item.setData(0, ITEM_BASE_TEXT, base_text)
        base_tooltip = item.data(0, ITEM_BASE_TOOLTIP)
        if not isinstance(base_tooltip, str):
            base_tooltip = item.toolTip(0)
            item.setData(0, ITEM_BASE_TOOLTIP, base_tooltip)

        monitored = node_id in self._monitored
        if monitored:
            mark = MONITOR_MARK_RUNNING if self._monitor_active else MONITOR_MARK_PENDING
            note = (
                "Monitored in this session's live monitor (subscription running)."
                if self._monitor_active
                else "In this session's live monitor (added, subscription not started)."
            )
        else:
            mark = ""
            note = ""
        item.setText(0, mark + base_text)
        font = item.font(0)
        font.setBold(monitored)
        item.setFont(0, font)
        item.setForeground(0, QBrush(QColor(ACCENT)) if monitored else QBrush())
        if monitored:
            item.setToolTip(0, f"{base_tooltip}\n{note}" if base_tooltip else note)
        else:
            item.setToolTip(0, base_tooltip)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, ITEM_LOADED):
            return
        node_id = item.data(0, ITEM_NODE_ID)
        if not node_id or node_id in self._loading:
            return
        if not self._service.is_connected:
            return
        self._loading.add(node_id)
        self._runner.submit(
            lambda: self._service.browse_children(str(node_id)),
            on_done=lambda children: self._on_children(item, str(node_id), children),
            on_error=lambda message: self._on_children_error(item, str(node_id), message),
        )

    def _on_children(self, item: QTreeWidgetItem, node_id: str, children: list[Any]) -> None:
        self._loading.discard(node_id)
        if not item or item.data(0, ITEM_NODE_ID) != node_id:
            return
        item.takeChildren()
        item.setData(0, ITEM_LOADED, True)
        for child in children:
            child_item = QTreeWidgetItem(
                [child.display_name or child.browse_name or child.node_id, child.node_class, child.node_id]
            )
            child_item.setData(0, ITEM_NODE_ID, child.node_id)
            child_item.setData(0, ITEM_LOADED, False)
            child_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            tooltip = self._child_tooltip(child)
            child_item.setToolTip(0, tooltip)
            self._decorate_item(child_item, child.node_id)
            item.addChild(child_item)
        if not children:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
        self._set_status(f"{len(children)} child node(s) read from {node_id}.")

    @staticmethod
    def _child_tooltip(child: Any) -> str:
        """Describe the child and any duplicate references merged into its row."""
        labels = [child.reference_type_name or child.reference_type, *child.additional_reference_types]
        text = f"{child.browse_name}\nReference: {', '.join(label for label in labels if label)}"
        if len(labels) > 1:
            text += "\n(merged: the server referenced this node more than once)"
        return text

    def _on_children_error(self, item: QTreeWidgetItem, node_id: str, message: str) -> None:
        self._loading.discard(node_id)
        if item:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
            item.setData(0, ITEM_BASE_TOOLTIP, message)
            item.setToolTip(0, message)
            self._decorate_item(item, node_id)
        _logger.warning("Browsing %s failed: %s", node_id, message)
        self._set_status(f"Browsing {node_id} failed: {message}", error=True)

    def _on_current_item_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if current is None:
            return
        node_id = current.data(0, ITEM_NODE_ID)
        if not node_id:
            return
        self._current_node_id = str(node_id)
        self._load_details(str(node_id))

    # -- details -----------------------------------------------------------
    def _load_details(self, node_id: str) -> None:
        self._detail_token += 1
        token = self._detail_token
        self._clear_details()
        self._runner.submit(
            lambda: self._service.read_node_details(node_id),
            on_done=lambda details: self._on_details(token, details),
            on_error=lambda message: self._on_details_error(token, node_id, message),
        )

    def _on_details(self, token: int, details: NodeDetails) -> None:
        if token != self._detail_token:
            return
        record = details.record

        current = self.tree.currentItem()
        if current is not None and current.data(0, ITEM_NODE_ID) == record.node_id:
            if not current.text(0) or current.text(0) == record.node_id:
                current.setText(0, record.display_name or record.browse_name or record.node_id)
            current.setText(1, record.node_class)
            current.setText(2, record.node_id)

        self._fill_attributes(details.attributes)
        self._fill_references(details.references)
        self._render_value(record.value)
        self._set_status(
            f"{record.node_class} {record.browse_name or record.node_id} · "
            f"{len(details.attributes)} attribute(s), {len(details.references)} reference(s)"
        )

    def _on_details_error(self, token: int, node_id: str, message: str) -> None:
        if token != self._detail_token:
            return
        _logger.warning("Reading node details for %s failed: %s", node_id, message)
        self._set_status(f"Reading {node_id} failed: {message}", error=True)

    def _clear_details(self) -> None:
        self.attributes_table.setRowCount(0)
        self.references_table.setRowCount(0)
        self.value_view.clear()
        self.value_meta.setText("No value read.")
        self.value_warning.hide()

    def _fill_attributes(self, attributes: list[AttributeResult]) -> None:
        table = self.attributes_table
        table.setRowCount(len(attributes))
        for row, attribute in enumerate(attributes):
            name_item = QTableWidgetItem(attribute.attribute)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            value_item = QTableWidgetItem(_format_cell(attribute.value))
            value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
            if not attribute.ok:
                value_item.setToolTip(attribute.status)
            status_item = QTableWidgetItem(attribute.status)
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, value_item)
            table.setItem(row, 2, status_item)

    def _fill_references(self, references: list[Any]) -> None:
        table = self.references_table
        table.setRowCount(len(references))
        for row, reference in enumerate(references):
            values = [
                reference.direction,
                reference.reference_type_name or reference.reference_type,
                reference.target_display_name or reference.target_browse_name,
                reference.target_node_id,
                reference.target_node_class,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 3:
                    item.setData(Qt.UserRole, reference.target_node_id)
                table.setItem(row, column, item)

    def _on_reference_activated(self, index: Any) -> None:
        item = self.references_table.item(index.row(), 3)
        if item is None:
            return
        node_id = item.data(Qt.UserRole) or item.text()
        if node_id:
            self.load_node(str(node_id))

    # -- value -------------------------------------------------------------
    def _on_read_value(self) -> None:
        if not self._current_node_id or not self._service.is_connected:
            return
        node_id = self._current_node_id
        self.value_meta.setText(f"Reading {node_id}…")
        self._runner.submit(
            lambda: self._service.read_value(node_id),
            on_done=self._render_value,
            on_error=lambda message: self._set_status(f"Reading the value failed: {message}", error=True),
        )

    def _render_value(self, snapshot: ValueSnapshot | None) -> None:
        self.value_warning.hide()
        if snapshot is None:
            self.value_view.clear()
            self.value_meta.setText("This node has no value (it is not a Variable).")
            return

        parts = [f"Status: {snapshot.status}"]
        if snapshot.variant_type:
            parts.append(f"Type: {snapshot.variant_type}")
        parts.append(f"Encoding: {snapshot.encoding}")
        self.value_meta.setText(" · ".join(parts))

        if snapshot.encoding == "unavailable":
            # No value to show: the server returned a bad status, or the client
            # could not decode it (see the status text, e.g. a DecodeError).
            self.value_view.setPlainText(f"The value is not available: {snapshot.status}")
            return

        if snapshot.source_timestamp:
            self.value_view.setPlainText(
                json.dumps(
                    {
                        "value": snapshot.value,
                        "source_timestamp": snapshot.source_timestamp,
                        "server_timestamp": snapshot.server_timestamp,
                    },
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )
            )
        else:
            self.value_view.setPlainText(json.dumps(snapshot.value, indent=2, ensure_ascii=False, default=str))

        if snapshot.unsupported_encoding:
            kinds = ", ".join(sorted({str(item.get("kind")) for item in snapshot.unsupported}))
            self.value_warning.setText(f"Some parts of this value could not be encoded faithfully: {kinds}")
            self.value_warning.show()

    # -- search ------------------------------------------------------------
    def _on_find(self) -> None:
        text = self.search_edit.text().strip().lower()
        if not text:
            return
        if self._snapshot is None or not self._snapshot.nodes:
            self._set_status(
                "Search needs a downloaded snapshot. Download the address space on the Export tab first, "
                "or open the node directly by NodeId.",
                error=True,
            )
            return
        self._matches = [
            node_id
            for node_id, record in sorted(self._snapshot.nodes.items())
            if text in record.display_name.lower()
            or text in record.browse_name.lower()
            or text in node_id.lower()
        ]
        self._match_index = 0
        if not self._matches:
            self._set_status(f"No downloaded node matches {text!r}.")
            return
        self._goto_match()

    def _on_next_match(self) -> None:
        if not self._matches:
            return
        self._match_index = (self._match_index + 1) % len(self._matches)
        self._goto_match()

    def _goto_match(self) -> None:
        node_id = self._matches[self._match_index]
        self.node_id_edit.setText(node_id)
        index = self.start_combo.findData("")
        if index >= 0:
            self.start_combo.setCurrentIndex(index)
        self.load_node(node_id)
        self._set_status(f"Match {self._match_index + 1} of {len(self._matches)}: {node_id}")

    # -- actions -----------------------------------------------------------
    def _on_copy_node_id(self) -> None:
        node_id = self._current_node_id
        if not node_id:
            item = self.tree.currentItem()
            node_id = item.data(0, ITEM_NODE_ID) if item else None
        if not node_id:
            self._set_status("Select a node first.")
            return
        QApplication.clipboard().setText(str(node_id))
        self._set_status(f"Copied {node_id} to the clipboard.")

    def _on_download_requested(self) -> None:
        if self._current_node_id:
            self.download_requested.emit(self._current_node_id)

    def _on_monitor_requested(self) -> None:
        if not self._current_node_id:
            self._set_status("Select a node first.")
            return
        self.monitor_requested.emit(self._current_node_id)
        self._set_status(f"{self._current_node_id} added to the Live monitor tab.")

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(("⚠ " if error else "") + message)
        self.status_label.setObjectName("errorLabel" if error else "hintLabel")
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    def current_node_id(self) -> str | None:
        return self._current_node_id
