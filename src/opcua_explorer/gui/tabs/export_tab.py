"""Address-space download and JSON/CSV export tab."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config import AppSettings
from ...defaults import DEFAULT_MAX_DEPENDENCIES, DEFAULT_MAX_NODES
from ...errors import InvalidNodeIdError
from ...exporters import export_csv, export_json
from ...models import AddressSpace, NamespaceEntry, TraversalConfig, TraversalProgress
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...opcua.nodeset2 import NodeSet2Config, NodeSet2Result
from ...project import ExportState
from ..async_runner import AsyncRunner, TaskHandle
from ..theme import mono_font

_logger = logging.getLogger(__name__)

#: What each output format carries beyond the snapshot itself. Shown in the
#: "Export coverage" panel so the tab answers "is everything exported?".
_FORMAT_NOTES = (
    "JSON / CSV: contain exactly what the snapshot holds (values and descriptions only when the\n"
    "             download included them).\n"
    "NodeSet2:    writes the downloaded nodes plus parents, type definitions, encodings and method\n"
    "             arguments, reading each written node's attributes from the live session at export\n"
    "             time. The XML exporter cannot write WriteMask/UserWriteMask, AccessRestrictions,\n"
    "             RolePermissions/UserRolePermissions, Symmetric/IsAbstract of custom ReferenceTypes,\n"
    "             IsAbstract of custom DataTypes or View nodes; every export lists what it omitted."
)


class ExportTab(QWidget):
    """Bounded download with progress/cancel, then JSON or CSV export."""

    snapshot_ready = Signal(object)  # AddressSpace
    export_finished = Signal(str)  # human-readable report text
    busy_changed = Signal(bool)

    def __init__(
        self,
        runner: AsyncRunner,
        service: OpcUaService,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        selection_provider: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        self._settings = settings
        #: Returns the NodeId selected in *this session's* browser tab.
        self._selection_provider = selection_provider
        self._snapshot: AddressSpace | None = None
        self._handle: TaskHandle | None = None
        self._export_handle: TaskHandle | None = None
        #: Namespace checks restored from a project before the array is read.
        self._pending_namespace_checks: list[int] | None = None

        self._build_ui()
        self.set_connected(False)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)
        outer.addWidget(self._build_settings(), 5)
        outer.addWidget(self._build_output(), 6)

    def _build_settings(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)

        title = QLabel("Download & export")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Downloads are bounded and cancellable. Continuation points are released when possible, "
            "including after cancellation."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        scope_box = QGroupBox("Scope")
        scope_layout = QVBoxLayout(scope_box)
        self.whole_radio = QRadioButton("Whole accessible address space (Root folder)")
        self.subtree_radio = QRadioButton("Selected subtree")
        self.whole_radio.setChecked(True)
        scope_layout.addWidget(self.whole_radio)
        scope_layout.addWidget(self.subtree_radio)

        subtree_row = QHBoxLayout()
        self.subtree_edit = QLineEdit()
        self.subtree_edit.setPlaceholderText("ns=2;s=Plant")
        self.use_selection_btn = QPushButton("Use browser selection")
        self.use_selection_btn.clicked.connect(self._on_use_selection)
        subtree_row.addWidget(self.subtree_edit, 1)
        subtree_row.addWidget(self.use_selection_btn)
        scope_layout.addLayout(subtree_row)
        self.subtree_radio.toggled.connect(self._sync_scope)
        layout.addWidget(scope_box)

        limits_box = QGroupBox("Limits & batching")
        limits_layout = QFormLayout(limits_box)
        self.max_nodes_spin = QSpinBox()
        self.max_nodes_spin.setRange(0, 1_000_000)
        self.max_nodes_spin.setValue(DEFAULT_MAX_NODES)
        self.max_nodes_spin.setSpecialValueText("unlimited")
        self.max_depth_spin = QSpinBox()
        self.max_depth_spin.setRange(0, 200)
        self.max_depth_spin.setValue(0)
        self.max_depth_spin.setSpecialValueText("unlimited")
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 32)
        self.concurrency_spin.setValue(4)
        self.batch_size_spin = QSpinBox()
        self.batch_size_spin.setRange(1, 500)
        self.batch_size_spin.setValue(50)
        self.max_refs_spin = QSpinBox()
        self.max_refs_spin.setRange(0, 100_000)
        self.max_refs_spin.setValue(0)
        self.max_refs_spin.setSpecialValueText("server default")

        limits_layout.addRow("Maximum nodes:", self.max_nodes_spin)
        limits_layout.addRow("Maximum depth:", self.max_depth_spin)
        limits_layout.addRow("Concurrent requests:", self.concurrency_spin)
        limits_layout.addRow("Nodes per Browse request:", self.batch_size_spin)
        limits_layout.addRow("References per node:", self.max_refs_spin)
        layout.addWidget(limits_box)

        namespaces_box = QGroupBox("Namespace filter")
        namespaces_layout = QVBoxLayout(namespaces_box)
        hint = QLabel(
            "Namespaces whose nodes are followed. Namespace 0 is always kept because the standard "
            "type system and folder structure live there."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        namespaces_layout.addWidget(hint)
        self.namespace_list = QListWidget()
        self.namespace_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.namespace_list.setMaximumHeight(150)
        namespaces_layout.addWidget(self.namespace_list)
        buttons = QHBoxLayout()
        all_button = QPushButton("All")
        all_button.clicked.connect(lambda: self._set_all_namespaces(True))
        none_button = QPushButton("None")
        none_button.clicked.connect(lambda: self._set_all_namespaces(False))
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._refresh_namespaces)
        buttons.addWidget(all_button)
        buttons.addWidget(none_button)
        buttons.addWidget(refresh_button)
        buttons.addStretch(1)
        namespaces_layout.addLayout(buttons)
        layout.addWidget(namespaces_box)

        content_box = QGroupBox("Export content")
        content_layout = QVBoxLayout(content_box)
        self.include_values_check = QCheckBox("Include variable values (Value, SourceTimestamp, ServerTimestamp)")
        content_layout.addWidget(self.include_values_check)
        self.include_description_check = QCheckBox("Include node descriptions (Description attribute)")
        self.include_description_check.setChecked(True)
        content_layout.addWidget(self.include_description_check)
        content_hint = QLabel(
            "Values are off by default: enabling them makes the export larger and reads live data from the "
            "process; unsupported encodings are flagged, never silently dropped. JSON/CSV contain exactly "
            "what the download collected, while NodeSet2 reads the written nodes' attributes from the live "
            "session at export time. Standard attributes that only the Address space panel shows (event "
            "notifier, write/role masks, historizing, sampling interval, abstract/symmetric/inverse name, "
            "data type definition, access restrictions, executable) are never part of a snapshot - see "
            "Export coverage."
        )
        content_hint.setObjectName("hintLabel")
        content_hint.setWordWrap(True)
        content_layout.addWidget(content_hint)
        layout.addWidget(content_box)

        nodeset_box = QGroupBox("NodeSet2 XML")
        nodeset_layout = QFormLayout(nodeset_box)
        self.dependencies_check = QCheckBox("Collect required dependencies")
        self.dependencies_check.setChecked(True)
        nodeset_layout.addRow(self.dependencies_check)
        self.max_dependencies_spin = QSpinBox()
        self.max_dependencies_spin.setRange(0, DEFAULT_MAX_NODES)
        self.max_dependencies_spin.setValue(DEFAULT_MAX_DEPENDENCIES)
        self.max_dependencies_spin.setSpecialValueText("none")
        nodeset_layout.addRow("Maximum dependency nodes:", self.max_dependencies_spin)
        schema_row = QHBoxLayout()
        self.schema_edit = QLineEdit()
        self.schema_edit.setPlaceholderText("optional NodeSet2 XSD for schema validation")
        schema_button = QPushButton("Browse…")
        schema_button.clicked.connect(self._on_browse_schema)
        schema_row.addWidget(self.schema_edit, 1)
        schema_row.addWidget(schema_button)
        nodeset_layout.addRow("Schema file:", schema_row)
        self.export_nodeset_btn = QPushButton("Export NodeSet2…")
        self.export_nodeset_btn.clicked.connect(self._on_export_nodeset)
        nodeset_layout.addRow(self.export_nodeset_btn)
        nodeset_hint = QLabel(
            "Writes parents, type definitions, encodings and method arguments as dependencies. Namespace 0 "
            "nodes are referenced, not duplicated. Missing dependencies and custom DataTypes without a "
            "definition are listed instead of being silently dropped."
        )
        nodeset_hint.setObjectName("hintLabel")
        nodeset_hint.setWordWrap(True)
        nodeset_layout.addRow(nodeset_hint)
        layout.addWidget(nodeset_box)

        actions = QVBoxLayout()
        self.download_btn = QPushButton("Download")
        self.download_btn.setObjectName("primary")
        self.download_btn.clicked.connect(self._on_download)
        self.cancel_btn = QPushButton("Cancel download")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.download_btn)
        row.addWidget(self.cancel_btn)
        actions.addLayout(row)

        export_row = QHBoxLayout()
        self.export_json_btn = QPushButton("Export JSON…")
        self.export_json_btn.clicked.connect(self._on_export_json)
        self.export_csv_btn = QPushButton("Export CSV…")
        self.export_csv_btn.clicked.connect(self._on_export_csv)
        export_row.addWidget(self.export_json_btn)
        export_row.addWidget(self.export_csv_btn)
        actions.addLayout(export_row)
        layout.addLayout(actions)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _build_output(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        progress_box = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_box)
        self.status_label = QLabel("Not connected.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        progress_layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)

        self.counters_label = QLabel("")
        self.counters_label.setObjectName("hintLabel")
        progress_layout.addWidget(self.counters_label)
        layout.addWidget(progress_box, 0)

        coverage_box = QGroupBox("Export coverage")
        coverage_layout = QVBoxLayout(coverage_box)
        self.coverage_view = QPlainTextEdit()
        self.coverage_view.setObjectName("coverage")
        self.coverage_view.setReadOnly(True)
        self.coverage_view.setFont(mono_font(9))
        self.coverage_view.setPlaceholderText(
            "Download an address space to see exactly what the snapshot contains and what the exporters "
            "can and cannot carry."
        )
        coverage_layout.addWidget(self.coverage_view, 1)
        layout.addWidget(coverage_box, 1)

        summary_box = QGroupBox("Export summary")
        summary_layout = QVBoxLayout(summary_box)
        self.summary_view = QPlainTextEdit()
        self.summary_view.setObjectName("summary")
        self.summary_view.setReadOnly(True)
        self.summary_view.setFont(mono_font(9))
        self.summary_view.setPlaceholderText("Run a download to see node counts, limits and limitations.")
        summary_layout.addWidget(self.summary_view, 1)
        layout.addWidget(summary_box, 1)
        return container

    # -- state -------------------------------------------------------------
    def set_connected(self, connected: bool) -> None:
        self.download_btn.setEnabled(connected)
        if not connected:
            self._set_busy(False)
            self.status_label.setText("Not connected. Open a session on the Connection tab first.")
            self.namespace_list.clear()

    def set_selected_node(self, node_id: str | None) -> None:
        if not node_id:
            return
        self.subtree_radio.setChecked(True)
        self.subtree_edit.setText(node_id)

    def _sync_scope(self) -> None:
        self.subtree_edit.setEnabled(self.subtree_radio.isChecked())

    def _set_all_namespaces(self, checked: bool) -> None:
        for index in range(self.namespace_list.count()):
            item = self.namespace_list.item(index)
            if item is not None:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _refresh_namespaces(self) -> None:
        if not self._service.is_connected:
            return
        self.status_label.setText("Reading the namespace array…")
        self._runner.submit(
            self._service.refresh_namespaces,
            on_done=self._on_namespaces,
            on_error=lambda message: self._show_error(f"Reading namespaces failed: {message}"),
        )

    def refresh_namespaces(self) -> None:
        """Public entry point used by the main window after connecting."""
        self._refresh_namespaces()

    def _on_namespaces(self, namespaces: list[NamespaceEntry]) -> None:
        self.namespace_list.clear()
        for entry in namespaces:
            item = QListWidgetItem(f"[{entry.index}] {entry.uri}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, entry.index)
            self.namespace_list.addItem(item)
        if self._pending_namespace_checks is not None:
            self._apply_namespace_checks(self._pending_namespace_checks)
            self._pending_namespace_checks = None
        self.status_label.setText(f"{len(namespaces)} namespace(s) available.")

    def _apply_namespace_checks(self, indexes: list[int] | None) -> None:
        wanted = None if indexes is None else {int(index) for index in indexes}
        for position in range(self.namespace_list.count()):
            item = self.namespace_list.item(position)
            if item is None:
                continue
            checked = wanted is None or int(item.data(Qt.UserRole)) in wanted
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    # -- project state -----------------------------------------------------
    def export_state(self) -> ExportState:
        """Editor state for a project file (no downloaded data)."""
        total = self.namespace_list.count()
        if total:
            checked: set[int] = set()
            for position in range(total):
                item = self.namespace_list.item(position)
                if item is not None and item.checkState() == Qt.Checked:
                    checked.add(int(item.data(Qt.UserRole)))
            namespaces = None if len(checked) == total else sorted(checked)
        else:
            # Not connected yet: keep whatever a project restored earlier.
            namespaces = (
                None if self._pending_namespace_checks is None else list(self._pending_namespace_checks)
            )
        return ExportState(
            scope="subtree" if self.subtree_radio.isChecked() else "whole",
            subtree_node_id=self.subtree_edit.text().strip() or None,
            max_nodes=self.max_nodes_spin.value(),
            max_depth=self.max_depth_spin.value(),
            concurrency=self.concurrency_spin.value(),
            batch_size=self.batch_size_spin.value(),
            max_references_per_node=self.max_refs_spin.value(),
            include_values=self.include_values_check.isChecked(),
            include_description=self.include_description_check.isChecked(),
            namespaces=namespaces,
            include_dependencies=self.dependencies_check.isChecked(),
            max_dependency_nodes=self.max_dependencies_spin.value(),
            schema_path=self.schema_edit.text().strip() or None,
        )

    def apply_export_state(self, state: ExportState) -> None:
        """Restore the export editors; nothing is downloaded."""
        if state.scope == "subtree":
            self.subtree_radio.setChecked(True)
        else:
            self.whole_radio.setChecked(True)
        self.subtree_edit.setText(state.subtree_node_id or "")
        self.max_nodes_spin.setValue(int(state.max_nodes))
        self.max_depth_spin.setValue(int(state.max_depth))
        self.concurrency_spin.setValue(int(state.concurrency))
        self.batch_size_spin.setValue(int(state.batch_size))
        self.max_refs_spin.setValue(int(state.max_references_per_node))
        self.include_values_check.setChecked(bool(state.include_values))
        self.include_description_check.setChecked(bool(state.include_description))
        self.dependencies_check.setChecked(bool(state.include_dependencies))
        self.max_dependencies_spin.setValue(int(state.max_dependency_nodes))
        self.schema_edit.setText(state.schema_path or "")
        self._pending_namespace_checks = None if state.namespaces is None else list(state.namespaces)
        if self.namespace_list.count():
            self._apply_namespace_checks(self._pending_namespace_checks)
            self._pending_namespace_checks = None
        self._sync_scope()

    # -- download ----------------------------------------------------------
    def _collect_config(self) -> TraversalConfig:
        checked: set[int] = set()
        total = self.namespace_list.count()
        for index in range(total):
            item = self.namespace_list.item(index)
            if item is not None and item.checkState() == Qt.Checked:
                checked.add(int(item.data(Qt.UserRole)))
        namespace_filter: frozenset[int] | None = None
        if total and len(checked) < total:
            namespace_filter = frozenset(checked)

        return TraversalConfig(
            max_nodes=self.max_nodes_spin.value() or None,
            max_depth=self.max_depth_spin.value() or None,
            concurrency=self.concurrency_spin.value(),
            batch_size=self.batch_size_spin.value(),
            max_references_per_node=self.max_refs_spin.value(),
            namespace_filter=namespace_filter,
            include_values=self.include_values_check.isChecked(),
            include_description=self.include_description_check.isChecked(),
        )

    def _roots(self) -> list[str] | None:
        if self.whole_radio.isChecked():
            return ["i=84"]
        text = self.subtree_edit.text().strip()
        if not text:
            self._show_error("Enter the NodeId of the subtree to download.")
            return None
        try:
            return [parse_node_id(text).to_string()]
        except InvalidNodeIdError as exc:
            self._show_error(str(exc))
            return None

    def _on_download(self) -> None:
        roots = self._roots()
        if roots is None:
            return
        config = self._collect_config()

        self.error_label.hide()
        self._set_busy(True)
        self.status_label.setText("Preparing download…")
        self.summary_view.clear()
        self.coverage_view.clear()

        key = self._runner.new_key()
        progress_callback = self._runner.progress_callback(key)
        self._handle = self._runner.submit(
            lambda: self._service.download(roots, config, progress=progress_callback),
            key=key,
            on_done=self._on_download_done,
            on_error=self._on_download_error,
            on_progress=self._on_progress,
        )

    def _on_progress(self, payload: Any) -> None:
        if not isinstance(payload, TraversalProgress):
            return
        self.status_label.setText(payload.message or payload.phase)
        self.counters_label.setText(
            f"nodes {payload.nodes_collected} · browsed {payload.nodes_browsed} · "
            f"pending {payload.nodes_pending} · references {payload.references_found} · depth {payload.depth}"
        )

    def _on_download_done(self, snapshot: AddressSpace) -> None:
        self._handle = None
        self._snapshot = snapshot
        self._set_busy(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.status_label.setText(
            "Download cancelled (partial snapshot)." if snapshot.cancelled else "Download complete."
        )
        self.summary_view.setPlainText(snapshot.summary_text())
        self.coverage_view.setPlainText(snapshot.coverage_text() + "\n\n" + _FORMAT_NOTES)
        self.snapshot_ready.emit(snapshot)
        self.export_finished.emit(
            f"Download from {snapshot.endpoint_url}: {len(snapshot.nodes)} node(s), "
            f"{len(snapshot.references)} reference(s)"
            + (" (cancelled)" if snapshot.cancelled else "")
        )

    def _on_download_error(self, message: str) -> None:
        self._handle = None
        self._set_busy(False)
        self._show_error(f"Download failed: {message}")

    def _on_cancel(self) -> None:
        if self._handle is not None:
            self.status_label.setText("Cancelling…")
            self._handle.cancel()
            self._handle = None

    # -- export ------------------------------------------------------------
    def _default_path(self, suffix: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = Path(self._settings.last_export_dir) if self._settings.last_export_dir else Path.home()
        return base / f"address-space-{stamp}.{suffix}"

    def _on_export_json(self) -> None:
        self._export("json")

    def _on_export_csv(self) -> None:
        self._export("csv")

    def _export(self, kind: str) -> None:
        if self._snapshot is None:
            self._show_error("Download an address space before exporting.")
            return
        suffix = "json" if kind == "json" else "csv"
        filter_text = "JSON files (*.json)" if kind == "json" else "CSV files (*.csv)"
        path, _ = QFileDialog.getSaveFileName(self, f"Export {suffix.upper()}", str(self._default_path(suffix)), filter_text)
        if not path:
            return

        snapshot = self._snapshot
        target = Path(path)
        self.status_label.setText(f"Writing {target.name}…")

        if kind == "json":

            def job() -> Any:
                return export_json(snapshot, target)

        else:

            def job() -> Any:
                return export_csv(snapshot, target)

        self._export_handle = self._runner.submit(
            job,
            on_done=lambda result: self._on_export_done(result, target),
            on_error=lambda message: self._show_error(f"Export failed: {message}"),
        )

    def _on_export_done(self, result: Any, target: Path) -> None:
        self._export_handle = None
        self._settings.last_export_dir = str(target.parent)
        self.status_label.setText(f"Wrote {target}")
        nodes = getattr(result, "nodes", None)
        references = getattr(result, "references", None)
        rows = getattr(result, "rows", None)
        details = (
            f"{nodes} node(s), {references} reference(s)"
            if nodes is not None
            else f"{rows} row(s)"
        )
        text = f"Exported {target.name} ({details})"
        self.export_finished.emit(text)
        _logger.info(text)

    # -- NodeSet2 ----------------------------------------------------------
    def _on_browse_schema(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "NodeSet2 schema", str(Path.home()), "XML Schema (*.xsd);;All files (*)")
        if path:
            self.schema_edit.setText(path)

    def _nodeset_config(self) -> NodeSet2Config:
        return NodeSet2Config(
            include_values=self.include_values_check.isChecked(),
            include_dependencies=self.dependencies_check.isChecked(),
            max_dependency_nodes=self.max_dependencies_spin.value(),
            schema_path=self.schema_edit.text().strip() or None,
        )

    def _on_export_nodeset(self) -> None:
        if self._snapshot is None:
            self._show_error("Download an address space before exporting NodeSet2.")
            return
        config = self._nodeset_config()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export NodeSet2 XML", str(self._default_path("xml")), "NodeSet2 XML (*.xml)"
        )
        if not path:
            return

        snapshot = self._snapshot
        target = Path(path)
        self.error_label.hide()
        self.status_label.setText(f"Building {target.name}…")
        self._set_busy(True)

        key = self._runner.new_key()
        progress_callback = self._runner.progress_callback(key)
        self._export_handle = self._runner.submit(
            lambda: self._service.export_nodeset2(
                target, snapshot, config, progress=progress_callback
            ),
            key=key,
            on_done=self._on_nodeset_done,
            on_error=self._on_nodeset_error,
            on_progress=self._on_progress,
        )

    def _on_nodeset_done(self, result: NodeSet2Result) -> None:
        self._export_handle = None
        self._set_busy(False)
        self._settings.last_export_dir = str(result.path.parent)
        self.status_label.setText(f"Wrote {result.path}")
        self.summary_view.setPlainText(result.summary_text())
        text = (
            f"NodeSet2 {result.path.name}: {result.nodes_written} node(s) "
            f"(+{result.dependency_nodes_added} dependency), validation {result.validation.status}"
        )
        self.export_finished.emit(text)
        _logger.info(text)

    def _on_nodeset_error(self, message: str) -> None:
        self._export_handle = None
        self._set_busy(False)
        self._show_error(f"NodeSet2 export failed: {message}")

    # -- helpers -----------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.download_btn.setEnabled(not busy and self._service.is_connected)
        self.cancel_btn.setEnabled(busy)
        self.export_json_btn.setEnabled(not busy and self._snapshot is not None)
        self.export_csv_btn.setEnabled(not busy and self._snapshot is not None)
        self.export_nodeset_btn.setEnabled(not busy and self._snapshot is not None)
        if busy:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
        self.busy_changed.emit(busy)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()
        self.status_label.setText(message)
        _logger.warning(message)

    def _on_use_selection(self) -> None:
        # The workspace injects this session's browser tab, so two sessions never
        # exchange selections through MainWindow's compatibility properties.
        node_id = self._selection_provider() if self._selection_provider is not None else None
        if node_id:
            self.set_selected_node(node_id)
        else:
            self._show_error("No node is selected in the address space browser.")

    def shutdown(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._export_handle is not None:
            self._export_handle.cancel()
            self._export_handle = None

    @property
    def snapshot(self) -> AddressSpace | None:
        return self._snapshot
