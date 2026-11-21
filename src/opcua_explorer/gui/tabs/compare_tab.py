"""Comparison of two sessions' address spaces — read-only, no client of its own.

The tab observes the sessions that already exist in the window: it runs one
bounded download per side through that session's own :class:`OpcUaService` and
hands both snapshots to :func:`opcua_explorer.compare.compare_address_spaces`.
It never connects, writes, subscribes or creates a client, and it owns no
subscription — the session tabs remain the only owners of their state.

Results are shown as one row per difference (kind, browse path, node ids,
attributes) with a filter; the selected row shows its full attribute payload.
JSON and HTML reports are produced by the same writers the ``opcua-compare``
command uses, so a GUI result and a CLI result are identical documents.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
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

from ...compare import (
    KIND_CHANGED,
    KIND_EXTRA_IN_CANDIDATE,
    KIND_LABELS,
    KIND_MISSING_IN_CANDIDATE,
    KIND_NOT_COMPARABLE,
    ComparisonConfig,
    ComparisonResult,
    NodeDifference,
    compare_address_spaces,
)
from ...config import AppSettings
from ...defaults import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_COMPARISON_DIFFERENCES,
)
from ...exporters import load_json_snapshot
from ...models import TraversalConfig, TraversalProgress
from ...reporting import write_comparison_html, write_comparison_json
from ..async_runner import AsyncRunner, TaskHandle
from ..tab_notice import TabNotice
from ..theme import ERROR, mono_font

_logger = logging.getLogger(__name__)

FILTER_ALL = "all"
#: Combo value that selects a saved JSON export instead of a live session.
FILE_SOURCE = "__saved_document__"

TOLERANCE_LABELS = (("functional", "functional (structure)"), ("strict", "strict (+ display text)"))

_COLUMNS = ("Kind", "Browse path", "Node class", "Reference NodeId", "Candidate NodeId", "Attributes")


# -- pure presentation helpers (no Qt, unit-tested without a window) --------


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def attribute_summary(difference: NodeDifference) -> str:
    """One-line summary of a row's attribute differences."""
    if difference.attributes:
        return ", ".join(item.attribute for item in difference.attributes)
    return difference.note


def difference_row(difference: NodeDifference) -> list[str]:
    """Table cells for one difference, in :data:`_COLUMNS` order."""
    return [
        kind_label(difference.kind),
        difference.browse_path or "—",
        difference.node_class or "—",
        difference.reference_node_id or "—",
        difference.candidate_node_id or "—",
        attribute_summary(difference),
    ]


def difference_payload(difference: NodeDifference) -> str:
    """Full JSON of one difference for the details pane (attribute values included)."""
    return json.dumps(difference.to_dict(), indent=2, ensure_ascii=False, default=str)


def filtered_differences(result: ComparisonResult, kind: str) -> list[NodeDifference]:
    """Rows for the selected bucket (``all`` keeps every difference)."""
    items = result.differences
    if kind and kind != FILTER_ALL:
        items = [item for item in items if item.kind == kind]
    return items


def comparison_headline(result: ComparisonResult) -> str:
    verdict = "No difference found" if result.is_equal else f"{result.difference_count} difference(s) found"
    parts = [
        verdict,
        f"matched {result.matched}",
        f"missing {len(result.missing_in_candidate)}",
        f"extra {len(result.extra_in_candidate)}",
        f"changed {len(result.changed)}",
        f"not comparable {len(result.not_comparable)}",
    ]
    if result.ignored:
        parts.append(f"ignored {result.ignored}")
    if result.namespace_differences:
        parts.append(f"namespace differences {len(result.namespace_differences)}")
    if result.reference_differences:
        parts.append(f"reference differences {len(result.reference_differences)}")
    return " · ".join(parts)


def comparison_warnings_text(result: ComparisonResult) -> str:
    return "\n".join(f"• {warning}" for warning in result.warnings)


def split_node_id_list(text: str) -> frozenset[str]:
    """Comma- or newline-separated NodeIds from one edit field."""
    parts = text.replace("\n", ",").split(",")
    return frozenset(part.strip() for part in parts if part.strip())


# -- widget ----------------------------------------------------------------


class CompareTab(QWidget):
    """Compare the address spaces of two existing sessions (read-only)."""

    #: Status-bar message (text, timeout in ms).
    message = Signal(str, int)
    #: One-line summary for the shared "Logs & reports" tab.
    report = Signal(str)

    def __init__(
        self,
        runner: AsyncRunner,
        settings: AppSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._settings = settings
        self._sessions: list[tuple[Any, str]] = []
        self._handle: TaskHandle | None = None
        self._result: ComparisonResult | None = None
        #: Saved JSON export per side (used when the combo selects FILE_SOURCE).
        self._file_paths: dict[str, str | None] = {"reference": None, "candidate": None}
        #: Last live-session index per side, restored when a file dialog is cancelled.
        self._last_index: dict[str, int] = {"reference": 0, "candidate": 0}

        self._build_ui()
        self._set_busy(False)

    # -- construction ------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Compare two sessions")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addWidget(
            TabNotice(
                "Structural comparison of two address spaces; read-only, and a side may be a saved JSON export.",
                "Downloads the address space of both sessions and compares them structurally. Read-only: the "
                "comparison writes, calls and subscribes nothing. Both sessions should use the same identity, "
                "otherwise access levels cannot be compared. A side can also be a saved JSON export "
                "(Export tab → Export JSON…), which needs no connection.",
            )
        )

        sessions_box = QGroupBox("Sessions")
        sessions_layout = QFormLayout(sessions_box)
        reference_row = QHBoxLayout()
        self.reference_combo = QComboBox()
        self.reference_combo.setMinimumWidth(320)
        self.candidate_combo = QComboBox()
        self.candidate_combo.setMinimumWidth(320)
        self.swap_button = QPushButton("Swap")
        self.swap_button.setToolTip("Exchange the reference and the candidate")
        self.swap_button.clicked.connect(self._on_swap)
        reference_row.addWidget(self.reference_combo, 1)
        sessions_layout.addRow("Reference (baseline):", reference_row)
        candidate_row = QHBoxLayout()
        candidate_row.addWidget(self.candidate_combo, 1)
        candidate_row.addWidget(self.swap_button)
        sessions_layout.addRow("Candidate (new device):", candidate_row)
        self.reference_combo.currentIndexChanged.connect(lambda _index: self._on_source_changed("reference"))
        self.candidate_combo.currentIndexChanged.connect(lambda _index: self._on_source_changed("candidate"))
        layout.addWidget(sessions_box)

        settings_box = QGroupBox("Download scope and comparison")
        settings_layout = QFormLayout(settings_box)
        self.roots_edit = QLineEdit("i=85")
        self.roots_edit.setToolTip("Start NodeIds, comma separated (default i=85 = Objects folder)")
        self.max_nodes_spin = QSpinBox()
        self.max_nodes_spin.setRange(0, 1_000_000)
        self.max_nodes_spin.setValue(0)
        self.max_nodes_spin.setSpecialValueText("unlimited")
        self.max_nodes_spin.setToolTip(
            "Node budget per side. A truncated side cannot be compared, so the default is unlimited."
        )
        self.tolerance_combo = QComboBox()
        for value, label in TOLERANCE_LABELS:
            self.tolerance_combo.addItem(label, value)
        self.references_check = QCheckBox("Also compare the reference graph")
        self.ignore_edit = QLineEdit()
        self.ignore_edit.setPlaceholderText("i=2258, ns=2;s=SerialNumber")
        self.ignore_edit.setToolTip("Nodes expected to differ (current time, counters, serial numbers)")
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("ns=2;s=Setpoint")
        self.value_edit.setToolTip(
            "Compare the values of these nodes (status, type, payload). Values are read only for them."
        )
        self.float_tolerance_spin = QDoubleSpinBox()
        self.float_tolerance_spin.setDecimals(6)
        self.float_tolerance_spin.setRange(0.0, 1_000_000.0)
        self.float_tolerance_spin.setSingleStep(0.1)
        self.float_tolerance_spin.setToolTip("Absolute tolerance for compared numbers; 0 means exact")

        settings_layout.addRow("Start NodeIds:", self.roots_edit)
        settings_layout.addRow("Maximum nodes per side:", self.max_nodes_spin)
        settings_layout.addRow("Tolerance:", self.tolerance_combo)
        settings_layout.addRow("", self.references_check)
        settings_layout.addRow("Ignore (comma separated):", self.ignore_edit)
        settings_layout.addRow("Compare values of:", self.value_edit)
        settings_layout.addRow("Float tolerance:", self.float_tolerance_spin)
        layout.addWidget(settings_box)

        buttons = QHBoxLayout()
        self.compare_button = QPushButton("Compare")
        self.compare_button.clicked.connect(self._on_compare)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        self.export_html_button = QPushButton("Export HTML report…")
        self.export_html_button.clicked.connect(lambda: self._export("html"))
        self.export_json_button = QPushButton("Export JSON…")
        self.export_json_button.clicked.connect(lambda: self._export("json"))
        buttons.addWidget(self.compare_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.export_html_button)
        buttons.addWidget(self.export_json_button)
        layout.addLayout(buttons)

        self.status_label = QLabel("Select two sessions and press Compare.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.headline_label = QLabel("")
        self.headline_label.setObjectName("sectionTitle")
        self.headline_label.setWordWrap(True)
        layout.addWidget(self.headline_label)

        self.warnings_label = QLabel("")
        self.warnings_label.setObjectName("hintLabel")
        self.warnings_label.setWordWrap(True)
        layout.addWidget(self.warnings_label)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet(f"color: {ERROR};")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All differences", FILTER_ALL)
        self.filter_combo.addItem(kind_label(KIND_MISSING_IN_CANDIDATE), KIND_MISSING_IN_CANDIDATE)
        self.filter_combo.addItem(kind_label(KIND_EXTRA_IN_CANDIDATE), KIND_EXTRA_IN_CANDIDATE)
        self.filter_combo.addItem(kind_label(KIND_CHANGED), KIND_CHANGED)
        self.filter_combo.addItem(kind_label(KIND_NOT_COMPARABLE), KIND_NOT_COMPARABLE)
        self.filter_combo.currentIndexChanged.connect(self._populate)
        filter_row.addWidget(self.filter_combo)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter rows (browse path, NodeId, attribute)…")
        self.search_edit.textChanged.connect(self._populate)
        filter_row.addWidget(self.search_edit, 1)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setFont(mono_font())
        self.details_view.setPlaceholderText("Select a row to see its attribute differences.")
        splitter.addWidget(self.table)
        splitter.addWidget(self.details_view)
        splitter.setSizes([700, 420])
        layout.addWidget(splitter, 1)

        self._rows: list[NodeDifference] = []

    # -- sessions ----------------------------------------------------------
    def set_sessions(self, sessions: list[tuple[Any, str]], descriptions: dict[Any, str] | None = None) -> None:
        """Rebuild the two session selectors; existing selections are kept by identity."""
        previous = (self.reference_combo.currentData(), self.candidate_combo.currentData())
        self._sessions = list(sessions)
        descriptions = descriptions or {}
        for side, combo in self._combos():
            combo.blockSignals(True)
            combo.clear()
            for workspace, title in self._sessions:
                combo.addItem(title, workspace)
                if workspace in descriptions:
                    combo.setItemData(combo.count() - 1, descriptions[workspace], Qt.ToolTipRole)
            combo.addItem(self._file_item_text(side), FILE_SOURCE)
            combo.blockSignals(False)

        if not self._sessions:
            self.status_label.setText("No session is open. Open one with File → New connection.")
            self.compare_button.setEnabled(False)
            return

        self.compare_button.setEnabled(True)
        self._restore_selection(previous)
        self.status_label.setText(
            "Both sides are downloaded again so the comparison uses one scope and one point in time."
        )

    def _combos(self) -> tuple[tuple[str, QComboBox], tuple[str, QComboBox]]:
        return (("reference", self.reference_combo), ("candidate", self.candidate_combo))

    def _combo(self, side: str) -> QComboBox:
        return self.reference_combo if side == "reference" else self.candidate_combo

    def _file_item_text(self, side: str) -> str:
        path = self._file_paths[side]
        if path:
            return f"Saved JSON: {Path(path).name}"
        return "Saved JSON export…"

    def _on_source_changed(self, side: str) -> None:
        """Pick a document when the sentinel item is chosen; restore the session otherwise."""
        combo = self._combo(side)
        if combo.currentData() != FILE_SOURCE:
            self._file_paths[side] = None
            self._last_index[side] = max(0, combo.currentIndex())
            return

        start = self._file_paths[side] or (self._settings.last_export_dir if self._settings else "") or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, f"Saved JSON export for the {side}", start, "JSON files (*.json);;All files (*)"
        )
        combo.blockSignals(True)
        if not path:
            combo.setCurrentIndex(self._last_index[side])
            combo.blockSignals(False)
            return
        self._file_paths[side] = path
        combo.setItemText(combo.currentIndex(), self._file_item_text(side))
        combo.setItemData(combo.currentIndex(), path, Qt.ToolTipRole)
        combo.blockSignals(False)
        self.status_label.setText(
            f"The {side} side is the saved document {path}; only the other side is downloaded."
        )

    def _source(self, side: str) -> tuple[Any | None, str | None]:
        """Resolve one side into ``(workspace, None)`` or ``(None, document path)``."""
        data = self._combo(side).currentData()
        if data == FILE_SOURCE:
            return None, self._file_paths[side]
        if data is None:
            return None, None
        return data, None

    def _restore_selection(self, previous: tuple[Any, Any]) -> None:
        for _side, combo in self._combos():
            combo.blockSignals(True)
        try:
            for side, combo, wanted in zip(
                ("reference", "candidate"), (self.reference_combo, self.candidate_combo), previous, strict=True
            ):
                if wanted == FILE_SOURCE and self._file_paths[side]:
                    combo.setCurrentIndex(combo.findData(FILE_SOURCE))
                    continue
                index = combo.findData(wanted) if wanted is not None else -1
                combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            for _side, combo in self._combos():
                combo.blockSignals(False)
        if (
            self.candidate_combo.count() > 2
            and self.candidate_combo.currentIndex() == 0
            and len(self._sessions) > 1
        ):
            self.candidate_combo.setCurrentIndex(1)

    def _on_swap(self) -> None:
        reference = self.reference_combo.currentIndex()
        candidate = self.candidate_combo.currentIndex()
        paths = dict(self._file_paths)
        for _side, combo in self._combos():
            combo.blockSignals(True)
        try:
            self.reference_combo.setCurrentIndex(candidate)
            self.candidate_combo.setCurrentIndex(reference)
            self._file_paths["reference"], self._file_paths["candidate"] = (
                paths["candidate"],
                paths["reference"],
            )
            for side, combo in self._combos():
                if combo.currentData() == FILE_SOURCE:
                    combo.setItemText(combo.currentIndex(), self._file_item_text(side))
                    combo.setItemData(combo.currentIndex(), self._file_paths[side], Qt.ToolTipRole)
        finally:
            for _side, combo in self._combos():
                combo.blockSignals(False)

    # -- inputs ------------------------------------------------------------
    def _roots(self) -> list[str]:
        roots = [part.strip() for part in self.roots_edit.text().replace("\n", ",").split(",") if part.strip()]
        return roots or ["i=85"]

    def _traversal_config(self) -> TraversalConfig:
        values = split_node_id_list(self.value_edit.text())
        max_nodes = self.max_nodes_spin.value()
        return TraversalConfig(
            max_nodes=None if max_nodes <= 0 else int(max_nodes),
            concurrency=DEFAULT_CONCURRENCY,
            batch_size=DEFAULT_BATCH_SIZE,
            include_values=bool(values),
        ).normalised()

    def _comparison_config(self) -> ComparisonConfig:
        return ComparisonConfig(
            tolerance=str(self.tolerance_combo.currentData() or "functional"),
            compare_references=self.references_check.isChecked(),
            value_node_ids=split_node_id_list(self.value_edit.text()),
            ignore_node_ids=split_node_id_list(self.ignore_edit.text()),
            float_tolerance=float(self.float_tolerance_spin.value()),
            max_differences=DEFAULT_MAX_COMPARISON_DIFFERENCES,
        ).normalised()

    # -- run ---------------------------------------------------------------
    def _on_compare(self) -> None:
        reference = self._source("reference")
        candidate = self._source("candidate")
        if reference == (None, None) or candidate == (None, None):
            self._show_error("Choose a session or a saved JSON export for both sides.")
            return
        for role, (workspace, document) in (("reference", reference), ("candidate", candidate)):
            if document is None and not workspace.service.is_connected:
                self._show_error(f"The {role} session is not connected. Connect it on its Connection tab first.")
                return

        roots = self._roots()
        traversal = self._traversal_config()
        config = self._comparison_config()
        self.error_label.hide()
        self._result = None
        self._rows = []
        self.table.setRowCount(0)
        self.details_view.clear()
        self.headline_label.clear()
        self.warnings_label.clear()
        self._set_busy(True)
        self.status_label.setText(
            "Reading both address spaces…"
            if reference[1] is None and candidate[1] is None
            else "Reading the live side and the saved document…"
        )

        key = self._runner.new_key()
        progress = self._runner.progress_callback(key)
        self._handle = self._runner.submit(
            lambda: self._compare_sources(reference, candidate, roots, traversal, config, progress),
            key=key,
            on_done=self._on_done,
            on_error=self._on_error,
            on_progress=self._on_progress,
        )

    async def _compare_sources(
        self,
        reference: tuple[Any | None, str | None],
        candidate: tuple[Any | None, str | None],
        roots: list[str],
        traversal: TraversalConfig,
        config: ComparisonConfig,
        progress: Any,
    ) -> ComparisonResult:
        reference_snapshot = await self._snapshot_of(reference, roots, traversal, progress)
        candidate_snapshot = await self._snapshot_of(candidate, roots, traversal, progress)
        return compare_address_spaces(reference_snapshot, candidate_snapshot, config)

    async def _snapshot_of(
        self,
        source: tuple[Any | None, str | None],
        roots: list[str],
        traversal: TraversalConfig,
        progress: Any,
    ) -> Any:
        workspace, document = source
        if document is not None:
            return load_json_snapshot(document)
        return await workspace.service.download(roots, traversal, progress=progress)

    def _on_progress(self, payload: Any) -> None:
        if not isinstance(payload, TraversalProgress):
            return
        self.status_label.setText(
            f"{payload.phase}: {payload.message or 'working'} · {payload.nodes_collected} node(s) collected"
        )

    def _on_done(self, result: ComparisonResult) -> None:
        self._handle = None
        self._set_busy(False)
        self.apply_result(result)

    def _on_error(self, message: str) -> None:
        self._handle = None
        self._set_busy(False)
        self._show_error(f"Comparison failed: {message}")

    def _on_cancel(self) -> None:
        if self._handle is not None:
            self.status_label.setText("Cancelling…")
            self._handle.cancel()
            self._handle = None
            self._set_busy(False)

    # -- results -----------------------------------------------------------
    def apply_result(self, result: ComparisonResult) -> None:
        """Show a completed comparison (also used by tests and by the CLI-only path)."""
        self._result = result
        self.headline_label.setText(comparison_headline(result))
        self.warnings_label.setText(comparison_warnings_text(result))
        self.warnings_label.setVisible(bool(result.warnings))
        self.status_label.setText(
            "Comparison finished. Select a row to see its attribute differences."
            if not result.is_equal
            else "Comparison finished without differences in the compared scope."
        )
        self._populate()
        self.export_html_button.setEnabled(True)
        self.export_json_button.setEnabled(True)
        one_line = (
            f"Comparison {result.reference_endpoint} → {result.candidate_endpoint}: "
            f"{result.difference_count} difference(s), {result.matched} matched"
        )
        self.report.emit(one_line)
        self.message.emit(one_line, 10000)
        _logger.info(one_line)

    def _populate(self) -> None:
        if self._result is None:
            return
        kind = self.filter_combo.currentData() or FILTER_ALL
        needle = self.search_edit.text().strip().lower()
        rows = filtered_differences(self._result, str(kind))
        if needle:
            rows = [row for row in rows if needle in " ".join(difference_row(row)).lower()]
        self._rows = rows
        self.table.setRowCount(len(rows))
        for position, difference in enumerate(rows):
            for column, text in enumerate(difference_row(difference)):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setToolTip(_bucket_hint(difference.kind))
                self.table.setItem(position, column, item)
        self.table.resizeColumnsToContents()
        if rows:
            self.table.selectRow(0)
        else:
            self.details_view.clear()

    def _on_row_selected(self) -> None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            self.details_view.clear()
            return
        position = selected[0].row()
        if 0 <= position < len(self._rows):
            self.details_view.setPlainText(difference_payload(self._rows[position]))

    # -- export ------------------------------------------------------------
    def _default_path(self, suffix: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = Path(self._settings.last_export_dir) if self._settings and self._settings.last_export_dir else Path.home()
        return base / f"comparison-{stamp}.{suffix}"

    def _export(self, kind: str) -> None:
        if self._result is None:
            self._show_error("Run a comparison before exporting a report.")
            return
        filter_text = "HTML report (*.html)" if kind == "html" else "JSON document (*.json)"
        path, _ = QFileDialog.getSaveFileName(self, f"Export {kind.upper()}", str(self._default_path(kind)), filter_text)
        if not path:
            return
        target = Path(path)
        result = self._result
        self.status_label.setText(f"Writing {target.name}…")

        if kind == "html":

            def job() -> Any:
                return write_comparison_html(result, target)

        else:

            def job() -> Any:
                return write_comparison_json(result, target)

        self._runner.submit(
            job,
            on_done=lambda report: self._on_export_done(report, target),
            on_error=lambda message: self._show_error(f"Writing the report failed: {message}"),
        )

    def _on_export_done(self, report: Any, target: Path) -> None:
        if self._settings is not None:
            self._settings.last_export_dir = str(target.parent)
        self.status_label.setText(f"Wrote {target}")
        self.report.emit(f"Comparison report written: {target}")
        self.message.emit(f"Comparison report written to {target}.", 8000)

    # -- state -------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.compare_button.setEnabled(not busy and bool(self._sessions))
        self.cancel_button.setEnabled(busy)
        self.export_html_button.setEnabled(not busy and self._result is not None)
        self.export_json_button.setEnabled(not busy and self._result is not None)

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.show()
        self.status_label.setText("Comparison not started." if "failed" not in text.lower() else "See the message above.")
        _logger.warning("%s", text)

    def shutdown(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


def _bucket_hint(kind: str) -> str:
    return {
        KIND_MISSING_IN_CANDIDATE: "Present on the reference, absent on the candidate.",
        KIND_EXTRA_IN_CANDIDATE: "Present on the candidate, absent on the reference.",
        KIND_CHANGED: "Matched node with different functional attributes.",
        KIND_NOT_COMPARABLE: "An attribute or node could not be read on one side — this is not equality.",
    }.get(kind, "")
