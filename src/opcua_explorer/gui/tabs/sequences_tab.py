"""Sequences tab: run an operator-written control document against this session.

The tab is deliberately device-neutral. It loads a ``.opcua-sequence.json`` document (the gate,
preconditions and body are all inside that file), lets the operator adjust the inputs, and runs,
dry-runs or stops it. There is no armed state and no per-write dialog: a write is sent only
because a sequence step asks for it, it is read back, and every step appears in the audit table.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import AppSettings
from ...models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    Sequence,
    SequenceRun,
    SequenceStep,
    SequenceStepResult,
)
from ...opcua.client_service import OpcUaService
from ...project import SequenceState
from ...sequence_document import SEQUENCE_FILE_FILTER, SequenceDocumentError, load_sequence_file
from ..async_runner import AsyncRunner, TaskHandle
from ..theme import mono_font

_logger = logging.getLogger(__name__)

_STATUS_COLORS = {
    STATUS_PASSED: "#17603a",
    STATUS_FAILED: "#b3261e",
    STATUS_SKIPPED: "#8a5a00",
    STATUS_CANCELLED: "#5f6368",
    STATUS_OUTCOME_UNKNOWN: "#8a1c9c",
}


class SequencesTab(QWidget):
    """Loads, dry-runs and runs one operator-written sequence per session."""

    report_ready = Signal(str)

    def __init__(
        self,
        runner: AsyncRunner,
        service: OpcUaService,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        self._settings = settings
        self._sequence: Sequence | None = None
        self._path: Path | None = None
        self._run: SequenceRun | None = None
        self._results: list[SequenceStepResult] = []
        self._handle: TaskHandle | None = None
        self._task_key: str | None = None
        self._pending_inputs: dict[str, str] = {}
        self._busy = False
        self._connected = False

        self._build_ui()
        self._runner.cancelled.connect(self._on_cancelled)
        self.set_connected(False)
        self._refresh_actions()

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)
        outer.addWidget(self._build_settings_column(), 5)
        outer.addWidget(self._build_results_column(), 6)

    def _build_settings_column(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)

        title = QLabel("Sequences")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "An operator-written document: preconditions (run before the gate; an assert is read-only, an "
            "ensure can write a prerequisite), an optional gate (acquire, keepalive, release) and the body. "
            "Nothing here is device-specific - the document holds the steps. A write is sent only because a "
            "step asks for it and is read back; every step is recorded."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        file_box = QGroupBox("Sequence document")
        file_layout = QVBoxLayout(file_box)
        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("No sequence loaded")
        row.addWidget(self.path_edit, 1)
        self.open_button = QPushButton("Open sequence…")
        self.open_button.clicked.connect(self._on_open)
        row.addWidget(self.open_button)
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(self._on_reload)
        row.addWidget(self.reload_button)
        file_layout.addLayout(row)
        self.name_label = QLabel("(no sequence loaded)")
        self.name_label.setStyleSheet("font-weight: 600;")
        file_layout.addWidget(self.name_label)
        self.description_label = QLabel("")
        self.description_label.setWordWrap(True)
        file_layout.addWidget(self.description_label)
        layout.addWidget(file_box)

        inputs_box = QGroupBox("Inputs (${name} placeholders)")
        inputs_layout = QVBoxLayout(inputs_box)
        self.inputs_table = QTableWidget(0, 2)
        self.inputs_table.setHorizontalHeaderLabels(["Input", "Value"])
        self.inputs_table.verticalHeader().setVisible(False)
        self.inputs_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        header = self.inputs_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        inputs_layout.addWidget(self.inputs_table)
        self.inputs_hint = QLabel("This sequence declares no inputs.")
        self.inputs_hint.setWordWrap(True)
        inputs_layout.addWidget(self.inputs_hint)
        layout.addWidget(inputs_box)

        run_box = QGroupBox("Run")
        run_layout = QVBoxLayout(run_box)
        buttons = QHBoxLayout()
        self.dry_run_button = QPushButton("Dry run")
        self.dry_run_button.setToolTip("Resolve every target and type and send nothing.")
        self.dry_run_button.clicked.connect(lambda: self._on_run(dry_run=True))
        buttons.addWidget(self.dry_run_button)
        self.run_button = QPushButton("Run")
        self.run_button.setToolTip("Execute the sequence once. Nothing is retried.")
        self.run_button.clicked.connect(lambda: self._on_run(dry_run=False))
        buttons.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._on_stop)
        buttons.addWidget(self.stop_button)
        run_layout.addLayout(buttons)

        self.status_label = QLabel("Load a sequence to begin.")
        self.status_label.setWordWrap(True)
        run_layout.addWidget(self.status_label)

        gate_row = QHBoxLayout()
        self.gate_label = QLabel("Gate: not engaged")
        self.gate_label.setWordWrap(True)
        gate_row.addWidget(self.gate_label, 1)
        self.release_button = QPushButton("Release gate now")
        self.release_button.setToolTip("Execute the document's gate release steps once.")
        self.release_button.clicked.connect(self._on_release)
        gate_row.addWidget(self.release_button)
        run_layout.addLayout(gate_row)
        layout.addWidget(run_box)

        steps_box = QGroupBox("Document steps")
        steps_layout = QVBoxLayout(steps_box)
        self.steps_table = QTableWidget(0, 4)
        self.steps_table.setHorizontalHeaderLabels(["Phase", "#", "Step", "Target / value"])
        self.steps_table.verticalHeader().setVisible(False)
        self.steps_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.steps_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        steps_header = self.steps_table.horizontalHeader()
        steps_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        steps_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        steps_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        steps_header.setSectionResizeMode(3, QHeaderView.Stretch)
        steps_layout.addWidget(self.steps_table)
        layout.addWidget(steps_box, 1)

        scroll.setWidget(container)
        return scroll

    def _build_results_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title = QLabel("Run timeline")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.summary_label = QLabel("No run yet.")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(["Phase", "Step", "Status", "ms", "Detail"])
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.results_table, 3)

        details_label = QLabel("Step detail")
        layout.addWidget(details_label)
        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setFont(mono_font())
        layout.addWidget(self.details_view, 2)
        return container

    # -- connection state ---------------------------------------------------
    def set_connected(self, connected: bool) -> None:
        self._connected = bool(connected)
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        connected = self._connected
        has_sequence = self._sequence is not None
        busy = self._busy
        self.open_button.setEnabled(not busy)
        self.reload_button.setEnabled(has_sequence and self._path is not None and not busy)
        self.dry_run_button.setEnabled(connected and has_sequence and not busy)
        self.run_button.setEnabled(connected and has_sequence and not busy)
        self.stop_button.setEnabled(busy)
        has_release = has_sequence and bool(self._sequence and self._sequence.gate.off)
        self.release_button.setEnabled(connected and has_release and not busy)

    # -- document ------------------------------------------------------------
    def _on_open(self) -> None:
        start = str(self._path.parent) if self._path is not None else str(Path.cwd())
        path, _selected = QFileDialog.getOpenFileName(self, "Open sequence document", start, SEQUENCE_FILE_FILTER)
        if not path:
            return
        self.load_sequence(path)

    def _on_reload(self) -> None:
        if self._path is not None:
            self.load_sequence(self._path)

    def load_sequence(self, path: str | Path) -> bool:
        """Load and validate a sequence document; returns ``True`` when it is usable."""
        target = Path(path)
        try:
            sequence = load_sequence_file(target)
        except SequenceDocumentError as exc:
            QMessageBox.warning(self, "Sequence not loaded", str(exc))
            self.status_label.setText(f"Sequence not loaded: {exc}")
            return False
        self._sequence = sequence
        self._path = target
        self.path_edit.setText(str(target))
        self.name_label.setText(sequence.name)
        self.description_label.setText(sequence.description or "")
        self._fill_inputs(self._pending_inputs)
        self._pending_inputs = {}
        self._fill_steps(sequence)
        self._clear_results()
        self.status_label.setText(
            "Sequence loaded. Use Dry run to validate the targets, then Run to execute it once."
        )
        self.report_ready.emit(f"Sequence loaded: {sequence.name} ({target})")
        self._refresh_actions()
        return True

    def _fill_inputs(self, values: dict[str, str]) -> None:
        sequence = self._sequence
        names = sorted(sequence.inputs) if sequence is not None else []
        self.inputs_table.setRowCount(len(names))
        for row, name in enumerate(names):
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.inputs_table.setItem(row, 0, name_item)
            value = values.get(name, sequence.inputs.get(name, "") if sequence is not None else "")
            self.inputs_table.setItem(row, 1, QTableWidgetItem(str(value)))
        self.inputs_hint.setVisible(not names)
        if not names:
            self.inputs_hint.setText("This sequence declares no inputs.")

    def current_inputs(self) -> dict[str, str]:
        inputs: dict[str, str] = {}
        for row in range(self.inputs_table.rowCount()):
            name_item = self.inputs_table.item(row, 0)
            value_item = self.inputs_table.item(row, 1)
            if name_item is None or value_item is None:
                continue
            inputs[name_item.text()] = value_item.text()
        return inputs

    def _fill_steps(self, sequence: Sequence) -> None:
        rows: list[tuple[str, int, SequenceStep]] = []
        rows.extend(("Precondition", index, step) for index, step in enumerate(sequence.preconditions, start=1))
        rows.extend(("Gate on", index, step) for index, step in enumerate(sequence.gate.on, start=1))
        if sequence.gate.verify is not None:
            rows.append(("Gate verify", 1, sequence.gate.verify))
        if sequence.gate.keepalive is not None:
            rows.append(("Keepalive", 1, sequence.gate.keepalive))
        rows.extend(("Body", index, step) for index, step in enumerate(sequence.steps, start=1))
        rows.extend(("Gate off", index, step) for index, step in enumerate(sequence.gate.off, start=1))

        self.steps_table.setRowCount(len(rows))
        for row, (phase, index, step) in enumerate(rows):
            values = [phase, str(index), f"{step.kind} {step.id}".strip(), _step_target(step)]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(step.note or "")
                self.steps_table.setItem(row, column, item)

    # -- running -------------------------------------------------------------
    def _on_run(self, *, dry_run: bool) -> None:
        sequence = self._sequence
        if sequence is None:
            self._show_error("Load a sequence document first.")
            return
        if not self._service.is_connected:
            self._show_error("Connect to a server before running a sequence.")
            return

        self._clear_results()
        self._run = None
        self.summary_label.setText("Dry run in progress…" if dry_run else "Sequence in progress…")
        self.status_label.setText("Preconditions…" if not dry_run else "Resolving targets (nothing is sent)…")
        self._set_busy(True)

        key = self._runner.new_key()
        self._task_key = key
        progress_callback = self._runner.progress_callback(key)
        inputs = self.current_inputs()

        def factory() -> Any:
            return self._service.run_sequence(
                sequence,
                inputs=inputs,
                dry_run=dry_run,
                on_step=progress_callback,
            )

        self._handle = self._runner.submit(
            factory,
            key=key,
            on_done=self._on_run_done,
            on_error=self._on_run_error,
            on_progress=self._on_progress,
        )

    def _on_release(self) -> None:
        sequence = self._sequence
        if sequence is None or not sequence.gate.off:
            self._show_error("The loaded sequence defines no gate release steps.")
            return
        if not self._service.is_connected:
            self._show_error("Connect to a server before releasing a gate.")
            return
        self._clear_results()
        self.status_label.setText("Releasing the gate…")
        self._set_busy(True)

        key = self._runner.new_key()
        self._task_key = key
        inputs = self.current_inputs()

        def factory() -> Any:
            return self._service.release_sequence_gate(sequence, inputs=inputs)

        self._handle = self._runner.submit(
            factory,
            key=key,
            on_done=self._on_run_done,
            on_error=self._on_run_error,
            on_progress=self._on_progress,
        )

    def _on_progress(self, payload: Any) -> None:
        if not isinstance(payload, SequenceStepResult):
            return
        self._append_result(payload)
        self.status_label.setText(f"{payload.step_id}: {payload.status}")

    def _on_run_done(self, run: SequenceRun) -> None:
        self._handle = None
        self._task_key = None
        self._run = run
        self._set_busy(False)
        self._fill_results(run.results)
        self.gate_label.setText(f"Gate: {run.gate_state or 'not involved'}")
        self.summary_label.setText(run.summary_text())
        self.status_label.setText("Dry run finished (nothing was sent)." if run.dry_run else "Run finished.")
        details = run.summary_text()
        if run.error:
            details += f"\n{run.error}"
        self.details_view.setPlainText(details)
        self.report_ready.emit(run.summary_text())

    def _on_run_error(self, message: str) -> None:
        self._handle = None
        self._task_key = None
        self._set_busy(False)
        self._show_error(f"Sequence failed: {message}")

    def _on_cancelled(self, key: str) -> None:
        if self._task_key is None or key != self._task_key:
            return
        self._handle = None
        self._task_key = None
        self._set_busy(False)
        run = self._service.last_sequence_run
        if run is not None:
            self._run = run
            self._fill_results(run.results)
            self.gate_label.setText(f"Gate: {run.gate_state or 'not involved'}")
            self.summary_label.setText(run.summary_text())
        self.status_label.setText("Run cancelled. The steps that ran are listed; the gate was released.")

    def _on_stop(self) -> None:
        if self._handle is not None:
            self.status_label.setText("Cancelling… the gate release still runs.")
            self._handle.cancel()
            self._handle = None
            self._refresh_actions()

    def _on_selection_changed(self) -> None:
        row = self.results_table.currentRow()
        if 0 <= row < len(self._results):
            self.details_view.setPlainText(json.dumps(self._results[row].to_dict(), indent=2))

    # -- results -------------------------------------------------------------
    def _clear_results(self) -> None:
        self._results = []
        self.results_table.setRowCount(0)
        self.details_view.clear()
        self.gate_label.setText("Gate: not engaged")
        self.summary_label.setText("No run yet.")

    def _fill_results(self, results: list[SequenceStepResult]) -> None:
        self._results = list(results)
        self.results_table.setRowCount(len(self._results))
        for row, result in enumerate(self._results):
            values = [
                result.phase,
                result.step_id,
                result.status,
                f"{result.duration_ms:.0f}",
                result.detail,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(result.detail)
                self.results_table.setItem(row, column, item)
            status_item = self.results_table.item(row, 2)
            color = _STATUS_COLORS.get(result.status)
            if color and status_item is not None:
                status_item.setForeground(QColor(color))
                status_item.setToolTip(result.detail)
        if self._results:
            self.results_table.selectRow(0)
            self._on_selection_changed()

    def _append_result(self, result: SequenceStepResult) -> None:
        self._results.append(result)
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        values = [
            result.phase,
            result.step_id,
            result.status,
            f"{result.duration_ms:.0f}",
            result.detail,
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(result.detail)
            self.results_table.setItem(row, column, item)

    # -- helpers -------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        if not busy:
            self._handle = None
        self._refresh_actions()

    def _show_error(self, message: str) -> None:
        self.status_label.setText(message)
        QMessageBox.warning(self, "Sequences", message)

    # -- shutdown ------------------------------------------------------------
    def shutdown(self) -> None:
        """Cancel a run in flight; the runner releases its gate from ``finally``.

        Closing the session must not leave a sequence on the worker loop, where its
        completion callback would target a widget that is about to be deleted.
        """
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._task_key = None

    # -- project state -------------------------------------------------------
    def sequence_state(self) -> SequenceState:
        return SequenceState(path=str(self._path) if self._path is not None else "", inputs=self.current_inputs())

    def apply_sequence_state(self, state: SequenceState) -> None:
        """Restore the document path and inputs without loading, connecting or running anything."""
        self._pending_inputs = dict(state.inputs)
        if state.path and Path(state.path).is_file() and self.load_sequence(state.path):
            return
        if state.path:
            self.path_edit.setText(state.path)
            self.status_label.setText(
                f"The project's sequence document is not available: {state.path}"
            )


def _step_target(step: SequenceStep) -> str:
    """One-line description of a step's target and payload for the document table."""
    parts: list[str] = []
    if step.node_path:
        parts.append("path " + "/".join(step.node_path))
    elif step.node_id:
        parts.append(step.node_id)
    if step.kind == "call":
        parts.append(step.method_node_id or f"method {step.method_name}")
        if step.arguments:
            parts.append("(" + ", ".join(step.arguments) + ")")
    elif step.kind in ("write", "ensure"):
        parts.append(f"= {step.value}")
    elif step.kind in ("expect", "assert", "wait_until"):
        parts.append(f"expects {step.expected or step.value}")
    elif step.kind == "delay":
        parts.append(f"{step.seconds:g} s")
    if step.verify and step.kind == "write":
        parts.append("[read back]")
    if step.optional:
        parts.append("[optional]")
    return " · ".join(part for part in parts if part)
