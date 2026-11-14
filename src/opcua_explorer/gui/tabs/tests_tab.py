"""Functional test tab: run the check suite and write JSON/HTML reports."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import AppSettings
from ...defaults import (
    DEFAULT_LATENCY_SAMPLES,
    DEFAULT_NOTIFICATION_WAIT_SECONDS,
    DEFAULT_PUBLISHING_INTERVAL_MS,
    DEFAULT_SAMPLING_INTERVAL_MS,
)
from ...errors import InvalidNodeIdError
from ...models import (
    FUNCTIONAL_TEST_DISCLAIMER,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    MethodCallTarget,
    TestCheck,
    TestConfig,
    TestProgress,
    TestRun,
    WriteTarget,
    iso_utc,
)
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...operator_text import EQUIPMENT_WARNING, guarded_operation_confirmation
from ...project import TestsState
from ...reporting import write_html_report, write_json_report
from ..async_runner import AsyncRunner, TaskHandle
from ..theme import mono_font

_logger = logging.getLogger(__name__)

_STATUS_COLORS = {
    STATUS_PASSED: "#17603a",
    STATUS_FAILED: "#b3261e",
    STATUS_SKIPPED: "#8a5a00",
}


class TestsTab(QWidget):
    """Runs the bounded read-only suite and writes JSON/HTML reports."""

    report_ready = Signal(str)

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
        self._handle: TaskHandle | None = None
        self._export_handle: TaskHandle | None = None
        self._run: TestRun | None = None
        self._checks: list[TestCheck] = []
        self._write_targets: list[WriteTarget] = []
        self._method_calls: list[MethodCallTarget] = []

        self._build_ui()
        self.set_connected(False)

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

        title = QLabel("Functional tests")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Bounded, read-only checks against the current session: discovery, browsing, attribute and value "
            "reads, subscriptions, latency and (opt-in) connection recovery."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        notice = QLabel(
            "The read-only checks never write and never call a method. Guarded writes and method calls exist "
            "only behind the opt-in below, for the single targets you list and confirm. "
            "These are functional and interoperability checks, not official OPC UA certification."
        )
        notice.setObjectName("warningLabel")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        target_box = QGroupBox("Target")
        target_layout = QFormLayout(target_box)
        target_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.root_edit = QLineEdit("i=85")
        self.root_edit.setPlaceholderText("i=85")
        self.variable_edit = QLineEdit()
        self.variable_edit.setPlaceholderText("ns=2;s=Temperature (empty = auto-detect near the root)")
        variable_row = QHBoxLayout()
        variable_row.addWidget(self.variable_edit, 1)
        self.use_selection_btn = QPushButton("Use browser selection")
        self.use_selection_btn.clicked.connect(self._on_use_selection)
        variable_row.addWidget(self.use_selection_btn)
        target_layout.addRow("Browse root:", self.root_edit)
        target_layout.addRow("Variable node:", variable_row)
        layout.addWidget(target_box)

        timing_box = QGroupBox("Timing & sampling")
        timing_layout = QFormLayout(timing_box)
        self.publishing_spin = QDoubleSpinBox()
        self.publishing_spin.setRange(50.0, 60_000.0)
        self.publishing_spin.setDecimals(0)
        self.publishing_spin.setSingleStep(100.0)
        self.publishing_spin.setValue(DEFAULT_PUBLISHING_INTERVAL_MS)
        self.publishing_spin.setSuffix(" ms")
        self.sampling_spin = QDoubleSpinBox()
        self.sampling_spin.setRange(0.0, 60_000.0)
        self.sampling_spin.setDecimals(0)
        self.sampling_spin.setSingleStep(50.0)
        self.sampling_spin.setValue(DEFAULT_SAMPLING_INTERVAL_MS)
        self.sampling_spin.setSuffix(" ms")
        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(0.5, 120.0)
        self.window_spin.setDecimals(1)
        self.window_spin.setSingleStep(0.5)
        self.window_spin.setValue(DEFAULT_NOTIFICATION_WAIT_SECONDS)
        self.window_spin.setSuffix(" s")
        self.latency_spin = QSpinBox()
        self.latency_spin.setRange(1, 200)
        self.latency_spin.setValue(DEFAULT_LATENCY_SAMPLES)
        timing_layout.addRow("Publishing interval:", self.publishing_spin)
        timing_layout.addRow("Sampling interval:", self.sampling_spin)
        timing_layout.addRow("Notification window:", self.window_spin)
        timing_layout.addRow("Latency samples:", self.latency_spin)
        layout.addWidget(timing_box)

        options_box = QGroupBox("Options")
        options_layout = QVBoxLayout(options_box)
        self.recovery_check = QCheckBox("Include connection-interruption check (closes the secure channel)")
        options_layout.addWidget(self.recovery_check)
        recovery_note = QLabel(
            "Opt-in and disruptive: the check closes the secure channel and reconnects, which interrupts any "
            "active subscription. It runs last and is skipped unless enabled."
        )
        recovery_note.setObjectName("hintLabel")
        recovery_note.setWordWrap(True)
        options_layout.addWidget(recovery_note)
        layout.addWidget(options_box)

        layout.addWidget(self._build_guarded_box())

        actions = QHBoxLayout()
        self.run_btn = QPushButton("Run tests")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._on_run)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        actions.addWidget(self.run_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Not connected. Open a session on the Connection tab first.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _build_guarded_box(self) -> QWidget:
        """Opt-in write / method-call configuration (phase 4)."""
        box = QGroupBox("Guarded write & method call (optional)")
        layout = QVBoxLayout(box)

        warning = QLabel(EQUIPMENT_WARNING)
        warning.setObjectName("warningLabel")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        self.allow_writes_check = QCheckBox(
            "I understand and allow these operations (confirmation is requested again before the run)"
        )
        layout.addWidget(self.allow_writes_check)

        write_form = QFormLayout()
        write_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.write_node_edit = QLineEdit()
        self.write_node_edit.setPlaceholderText("ns=2;s=Temperature")
        self.write_value_edit = QLineEdit()
        self.write_value_edit.setPlaceholderText("21.5  ·  true  ·  text  ·  [1, 2, 3] for arrays")
        self.write_restore_check = QCheckBox("Restore the previous value afterwards (not automatic)")
        write_row = QHBoxLayout()
        self.write_use_selection_btn = QPushButton("Use browser selection")
        self.write_use_selection_btn.clicked.connect(lambda: self._use_browser_selection(self.write_node_edit))
        self.add_write_btn = QPushButton("Add write target")
        self.add_write_btn.clicked.connect(self._on_add_write)
        write_row.addWidget(self.write_use_selection_btn)
        write_row.addWidget(self.add_write_btn)
        write_row.addStretch(1)
        write_form.addRow("Node:", self.write_node_edit)
        write_form.addRow("Value:", self.write_value_edit)
        write_form.addRow("", self.write_restore_check)
        write_form.addRow("", write_row)
        layout.addLayout(write_form)

        self.write_list = QListWidget()
        self.write_list.setMaximumHeight(84)
        self.write_list.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.write_list)
        remove_write = QPushButton("Remove selected write target")
        remove_write.clicked.connect(self._on_remove_write)
        layout.addWidget(remove_write)

        call_form = QFormLayout()
        call_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.call_object_edit = QLineEdit()
        self.call_object_edit.setPlaceholderText("ns=2;s=Motor")
        self.call_method_edit = QLineEdit()
        self.call_method_edit.setPlaceholderText("ns=2;s=Start")
        self.call_args_edit = QLineEdit()
        self.call_args_edit.setPlaceholderText("comma-separated, in server order: 1450")
        call_row = QHBoxLayout()
        self.call_use_selection_btn = QPushButton("Use browser selection")
        self.call_use_selection_btn.clicked.connect(lambda: self._use_browser_selection(self.call_object_edit))
        self.add_call_btn = QPushButton("Add method call")
        self.add_call_btn.clicked.connect(self._on_add_call)
        call_row.addWidget(self.call_use_selection_btn)
        call_row.addWidget(self.add_call_btn)
        call_row.addStretch(1)
        call_form.addRow("Object node:", self.call_object_edit)
        call_form.addRow("Method node:", self.call_method_edit)
        call_form.addRow("Arguments:", self.call_args_edit)
        call_form.addRow("", call_row)
        layout.addLayout(call_form)

        self.call_list = QListWidget()
        self.call_list.setMaximumHeight(84)
        self.call_list.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.call_list)
        remove_call = QPushButton("Remove selected method call")
        remove_call.clicked.connect(self._on_remove_call)
        layout.addWidget(remove_call)

        self.guarded_hint = QLabel(
            "Arguments are validated against the server's InputArguments before anything is sent. Nothing is "
            "discovered, scanned or repeated: one target, one operation, one check."
        )
        self.guarded_hint.setObjectName("hintLabel")
        self.guarded_hint.setWordWrap(True)
        layout.addWidget(self.guarded_hint)
        return box

    def _build_results_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.summary_label = QLabel(FUNCTIONAL_TEST_DISCLAIMER)
        self.summary_label.setObjectName("hintLabel")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        splitter = QSplitter(Qt.Vertical)

        self.results_table = QTableWidget(0, 6)
        self.results_table.setHorizontalHeaderLabels(
            ["#", "Check", "Category", "Status", "Duration (ms)", "Detail"]
        )
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_table.setAlternatingRowColors(True)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.results_table.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.results_table)

        self.details_view = QPlainTextEdit()
        self.details_view.setObjectName("summary")
        self.details_view.setReadOnly(True)
        self.details_view.setFont(mono_font(9))
        self.details_view.setPlaceholderText(
            "Run the tests and select a check to see StatusCodes, measurements and notes."
        )
        splitter.addWidget(self.details_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        export_row = QHBoxLayout()
        self.export_json_btn = QPushButton("Export JSON report…")
        self.export_json_btn.clicked.connect(lambda: self._export("json"))
        self.export_html_btn = QPushButton("Export HTML report…")
        self.export_html_btn.clicked.connect(lambda: self._export("html"))
        self.copy_btn = QPushButton("Copy summary")
        self.copy_btn.clicked.connect(self._copy_summary)
        export_row.addWidget(self.export_json_btn)
        export_row.addWidget(self.export_html_btn)
        export_row.addWidget(self.copy_btn)
        export_row.addStretch(1)
        layout.addLayout(export_row)

        self._set_export_enabled(False)
        return container

    # -- state -------------------------------------------------------------
    def set_connected(self, connected: bool) -> None:
        self.run_btn.setEnabled(connected)
        self.use_selection_btn.setEnabled(connected)
        self.write_use_selection_btn.setEnabled(connected)
        self.call_use_selection_btn.setEnabled(connected)
        self.add_write_btn.setEnabled(connected)
        self.add_call_btn.setEnabled(connected)
        if not connected:
            self._set_busy(False)
            self.status_label.setText("Not connected. Open a session on the Connection tab first.")
        elif self._run is None:
            self.status_label.setText("Ready. Adjust the target and run the checks.")

    def set_selected_node(self, node_id: str | None) -> None:
        if node_id:
            self.variable_edit.setText(node_id)

    # -- project state -----------------------------------------------------
    def tests_state(self) -> TestsState:
        """Editor state for a project file. ``allow_writes`` is never stored."""
        return TestsState(
            root_node_id=self.root_edit.text().strip() or "i=85",
            variable_node_id=self.variable_edit.text().strip() or None,
            publishing_interval_ms=float(self.publishing_spin.value()),
            sampling_interval_ms=float(self.sampling_spin.value()),
            notification_wait_seconds=float(self.window_spin.value()),
            latency_samples=int(self.latency_spin.value()),
            include_channel_recovery=self.recovery_check.isChecked(),
            write_targets=list(self._write_targets),
            method_calls=list(self._method_calls),
        )

    def apply_tests_state(self, state: TestsState) -> None:
        """Restore the editors; no check is run and guarded writes stay disabled."""
        self.root_edit.setText(state.root_node_id or "i=85")
        self.variable_edit.setText(state.variable_node_id or "")
        self.publishing_spin.setValue(float(state.publishing_interval_ms))
        self.sampling_spin.setValue(float(state.sampling_interval_ms))
        self.window_spin.setValue(float(state.notification_wait_seconds))
        self.latency_spin.setValue(int(state.latency_samples))
        self.recovery_check.setChecked(bool(state.include_channel_recovery))
        # Safety: the guarded-operation master switch is opt-in for every run.
        self.allow_writes_check.setChecked(False)
        self._write_targets = list(state.write_targets)
        self._method_calls = list(state.method_calls)
        self._refresh_guarded_lists()
        if self._write_targets or self._method_calls:
            self.status_label.setText(
                "Guarded targets restored. Tick 'I understand and allow these operations' before running."
            )

    def _selected_browser_node(self) -> str | None:
        """The NodeId selected in this session's browser (injected by the workspace)."""
        return self._selection_provider() if self._selection_provider is not None else None

    def _on_use_selection(self) -> None:
        node_id = self._selected_browser_node()
        if node_id:
            self.set_selected_node(node_id)
            self.status_label.setText(f"Target variable set to {node_id}.")
        else:
            self._show_error("No node is selected in the address space browser.")

    def _use_browser_selection(self, edit: QLineEdit) -> None:
        node_id = self._selected_browser_node()
        if node_id:
            edit.setText(node_id)
        else:
            self._show_error("No node is selected in the address space browser.")

    # -- guarded targets ---------------------------------------------------
    def _on_add_write(self) -> None:
        node_text = self.write_node_edit.text().strip()
        if not node_text:
            self._show_error("Enter the NodeId of the variable to write.")
            return
        try:
            node_id = parse_node_id(node_text).to_string()
        except InvalidNodeIdError as exc:
            self._show_error(f"Write target: {exc}")
            return
        if not self.write_value_edit.text().strip():
            self._show_error("Enter the value to write.")
            return
        self._write_targets.append(
            WriteTarget(
                node_id=node_id,
                value_text=self.write_value_edit.text().strip(),
                restore_previous_value=self.write_restore_check.isChecked(),
            )
        )
        self._refresh_guarded_lists()
        self.write_node_edit.clear()
        self.write_value_edit.clear()
        self.write_restore_check.setChecked(False)

    def _on_remove_write(self) -> None:
        row = self.write_list.currentRow()
        if 0 <= row < len(self._write_targets):
            del self._write_targets[row]
            self._refresh_guarded_lists()

    def _on_add_call(self) -> None:
        object_text = self.call_object_edit.text().strip()
        method_text = self.call_method_edit.text().strip()
        if not object_text or not method_text:
            self._show_error("Enter both the object node and the method node.")
            return
        try:
            object_id = parse_node_id(object_text).to_string()
            method_id = parse_node_id(method_text).to_string()
        except InvalidNodeIdError as exc:
            self._show_error(f"Method call: {exc}")
            return
        arguments = [part.strip() for part in self.call_args_edit.text().split(",") if part.strip()]
        self._method_calls.append(
            MethodCallTarget(object_node_id=object_id, method_node_id=method_id, arguments=arguments)
        )
        self._refresh_guarded_lists()
        self.call_object_edit.clear()
        self.call_method_edit.clear()
        self.call_args_edit.clear()

    def _on_remove_call(self) -> None:
        row = self.call_list.currentRow()
        if 0 <= row < len(self._method_calls):
            del self._method_calls[row]
            self._refresh_guarded_lists()

    def _refresh_guarded_lists(self) -> None:
        self.write_list.clear()
        for target in self._write_targets:
            restore = " +restore" if target.restore_previous_value else ""
            item = QListWidgetItem(f"{target.node_id} ← {target.value_text}{restore}")
            item.setToolTip("The previous value is restored only because you asked for it.")
            self.write_list.addItem(item)
        self.call_list.clear()
        for call in self._method_calls:
            arguments = ", ".join(call.arguments) if call.arguments else "no arguments"
            item = QListWidgetItem(f"{call.object_node_id} → {call.method_node_id}({arguments})")
            self.call_list.addItem(item)
        self.guarded_hint.setText(
            f"{len(self._write_targets)} write target(s), {len(self._method_calls)} method call(s) configured. "
            "Arguments are validated against the server's InputArguments before anything is sent. Nothing is "
            "discovered, scanned or repeated: one target, one operation, one check."
        )

    # -- run ---------------------------------------------------------------
    def _collect_config(self) -> TestConfig | None:
        root_text = self.root_edit.text().strip() or "i=85"
        try:
            root = parse_node_id(root_text).to_string()
        except InvalidNodeIdError as exc:
            self._show_error(f"Browse root: {exc}")
            return None

        variable_text = self.variable_edit.text().strip()
        variable: str | None = None
        if variable_text:
            try:
                variable = parse_node_id(variable_text).to_string()
            except InvalidNodeIdError as exc:
                self._show_error(f"Variable node: {exc}")
                return None

        return TestConfig(
            root_node_id=root,
            variable_node_id=variable,
            publishing_interval_ms=float(self.publishing_spin.value()),
            sampling_interval_ms=float(self.sampling_spin.value()),
            notification_wait_seconds=float(self.window_spin.value()),
            latency_samples=int(self.latency_spin.value()),
            include_channel_recovery=self.recovery_check.isChecked(),
            allow_writes=self.allow_writes_check.isChecked(),
            write_targets=list(self._write_targets),
            method_calls=list(self._method_calls),
        )

    def _confirm_operations(self, config: TestConfig) -> bool:
        """Ask for explicit approval; a silent run is never allowed."""
        if not (config.write_targets or config.method_calls):
            return True
        answer = QMessageBox.question(
            self,
            "Confirm guarded write / method call",
            guarded_operation_confirmation(endpoint_url=self._service.endpoint_url, config=config),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _on_run(self) -> None:
        if not self._service.is_connected:
            self._show_error("Connect to a server before running the tests.")
            return
        config = self._collect_config()
        if config is None:
            return
        if config.allow_writes and not self._confirm_operations(config):
            self.status_label.setText("Cancelled: the guarded operations were not confirmed. Nothing was sent.")
            return
        if not config.allow_writes and (config.write_targets or config.method_calls):
            self.status_label.setText(
                "Writes are disabled: the configured targets will be reported as skipped, not performed."
            )

        self.error_label.hide()
        self.results_table.setRowCount(0)
        self.details_view.clear()
        self._run = None
        self._checks = []
        self._set_export_enabled(False)
        self._set_busy(True)
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("Running checks…")
        self.summary_label.setText(FUNCTIONAL_TEST_DISCLAIMER)

        key = self._runner.new_key()
        progress_callback = self._runner.progress_callback(key)
        self._handle = self._runner.submit(
            lambda: self._service.run_functional_tests(config, progress=progress_callback),
            key=key,
            on_done=self._on_run_done,
            on_error=self._on_run_error,
            on_progress=self._on_progress,
        )

    def _on_progress(self, payload: Any) -> None:
        if not isinstance(payload, TestProgress):
            return
        if payload.total:
            self.progress_bar.setRange(0, payload.total)
            self.progress_bar.setValue(min(payload.current, payload.total))
        self.status_label.setText(payload.message or payload.phase)

    def _on_run_done(self, run: TestRun) -> None:
        self._handle = None
        self._run = run
        self._checks = list(run.checks)
        self._set_busy(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self._fill_results()
        self._set_export_enabled(True)
        summary = run.summary()
        self.summary_label.setText(
            f"{FUNCTIONAL_TEST_DISCLAIMER}\n"
            f"{summary['passed']} passed · {summary['failed']} failed · {summary['skipped']} skipped "
            f"· {summary['duration_seconds']} s"
            + (" · CANCELLED (partial)" if run.cancelled else "")
        )
        self.status_label.setText(
            "Run finished." if not run.cancelled else "Run cancelled; partial results are shown."
        )
        self.report_ready.emit(
            f"Test run {iso_utc(run.started_at)}: {summary['passed']} passed, {summary['failed']} failed, "
            f"{summary['skipped']} skipped ({summary['duration_seconds']} s)"
            + (" - cancelled" if run.cancelled else "")
        )
        first_issue = next(
            (index for index, check in enumerate(run.checks) if check.status == STATUS_FAILED), None
        )
        if run.checks:
            self.results_table.selectRow(first_issue if first_issue is not None else 0)
            self._on_selection_changed()

    def _on_run_error(self, message: str) -> None:
        self._handle = None
        self._set_busy(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self._show_error(f"Test run failed: {message}")

    def _on_cancel(self) -> None:
        if self._handle is not None:
            self.status_label.setText("Cancelling…")
            self._handle.cancel()
            self._handle = None

    # -- results -----------------------------------------------------------
    def _fill_results(self) -> None:
        table = self.results_table
        table.setRowCount(len(self._checks))
        for row, check in enumerate(self._checks):
            values = [
                str(row + 1),
                check.name,
                check.category,
                check.status,
                f"{check.duration_ms:.1f}",
                check.detail,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setToolTip(check.detail or check.error or "")
                table.setItem(row, column, item)
            status_item = table.item(row, 3)
            color = _STATUS_COLORS.get(check.status)
            if status_item is not None and color:
                status_item.setForeground(QColor(color))
                font = status_item.font()
                font.setBold(True)
                status_item.setFont(font)

    def _on_selection_changed(self) -> None:
        row = self.results_table.currentRow()
        if row < 0 or row >= len(self._checks):
            self.details_view.clear()
            return
        self.details_view.setPlainText(self._render_details(self._checks[row]))

    def _render_details(self, check: TestCheck) -> str:
        lines = [
            f"{check.name}  [{check.status}]",
            f"Category: {check.category}",
            f"Duration: {check.duration_ms:.1f} ms",
            f"Started:  {iso_utc(check.started_at)}",
        ]
        if check.detail:
            lines += ["", check.detail]
        if check.error:
            lines += ["", f"Error: {check.error}"]
        if check.notes:
            lines += ["", "Notes:"] + [f"  - {note}" for note in check.notes]
        if check.status_codes:
            lines += ["", "StatusCodes:"]
            for entry in check.status_codes:
                node = entry.get("node_id", "")
                lines.append(f"  {str(entry.get('operation', '')):<30} {str(entry.get('status', '')):<32} {node}")
        if check.data:
            lines += ["", "Data:", json.dumps(check.data, indent=2, ensure_ascii=False, default=str)]
        return "\n".join(lines)

    def _copy_summary(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._run is None:
            self._show_error("Run the tests first.")
            return
        QApplication.clipboard().setText(self._run.summary_text())
        self.status_label.setText("Summary copied to the clipboard.")

    # -- reports -----------------------------------------------------------
    def _set_export_enabled(self, enabled: bool) -> None:
        self.export_json_btn.setEnabled(enabled)
        self.export_html_btn.setEnabled(enabled)
        self.copy_btn.setEnabled(enabled)

    def _default_path(self, suffix: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = Path(self._settings.last_export_dir) if self._settings.last_export_dir else Path.home()
        return base / f"opcua-test-report-{stamp}.{suffix}"

    def _export(self, kind: str) -> None:
        if self._run is None:
            self._show_error("Run the tests before exporting a report.")
            return
        suffix = "json" if kind == "json" else "html"
        filter_text = "JSON files (*.json)" if kind == "json" else "HTML files (*.html)"
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {suffix.upper()} report", str(self._default_path(suffix)), filter_text
        )
        if not path:
            return

        run = self._run
        target = Path(path)
        self.status_label.setText(f"Writing {target.name}…")
        writer = write_json_report if kind == "json" else write_html_report
        self._export_handle = self._runner.submit(
            lambda: writer(run, target),
            on_done=lambda result: self._on_export_done(result, target),
            on_error=lambda message: self._show_error(f"Writing the report failed: {message}"),
        )

    def _on_export_done(self, result: Any, target: Path) -> None:
        self._export_handle = None
        self._settings.last_export_dir = str(target.parent)
        self.status_label.setText(f"Wrote {target}")
        self.report_ready.emit(f"Test report written: {target} ({getattr(result, 'checks', 0)} checks)")
        _logger.info("Test report written to %s", target)

    # -- helpers -----------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.run_btn.setEnabled(not busy and self._service.is_connected)
        self.cancel_btn.setEnabled(busy)
        self.recovery_check.setEnabled(not busy)
        self.root_edit.setEnabled(not busy)
        self.variable_edit.setEnabled(not busy)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()
        self.status_label.setText(message)
        _logger.warning(message)

    def shutdown(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._export_handle is not None:
            self._export_handle.cancel()
            self._export_handle = None

    @property
    def run_result(self) -> TestRun | None:
        return self._run

