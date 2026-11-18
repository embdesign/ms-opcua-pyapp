"""Write & call tab: one confirmed variable write and one confirmed method call.

Both actions are operator-driven and go through ``opcua/write_service.py`` with
``WriteAuthority("operator-dialog", …)``: exactly one write (plus an optional
verified restore) and exactly one call, each read back / reported, never retried.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...errors import InvalidNodeIdError
from ...formatting import flatten_for_csv
from ...models import NodeDetails, ValueSnapshot
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...opcua.write_service import CallOutcome, Inspection, WriteResult
from ...operator_text import method_call_confirmation, write_value_confirmation
from ..async_runner import AsyncRunner
from ..theme import ERROR, mono_font

_logger = logging.getLogger(__name__)


def ask_confirmation(parent: QWidget, title: str, text: str) -> bool:
    """Modal yes/no with the exact operation text; kept as a function so tests can stub it."""
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


class WriteCallTab(QWidget):
    """Interactive write and method call for one node, each behind a confirmation."""

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
        self._busy = False
        self._closed = False
        self._current_node: str = ""
        self._current_value_text: str = ""

        self._build_ui()
        self.set_connected(False)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Write & call")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "One write and one method call, each sent once after you confirm the exact target. A write is read "
            "back and compared; the previous value is restored only if you tick that box (and then as one more "
            "verified write). A call uses the server's InputArguments and is never retried."
        )
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Target ------------------------------------------------------------
        target_box = QGroupBox("Target node")
        target_layout = QVBoxLayout(target_box)
        node_row = QHBoxLayout()
        self.node_edit = QLineEdit()
        self.node_edit.setPlaceholderText("ns=2;s=Temperature")
        self.use_selection_btn = QPushButton("Use browser selection")
        self.use_selection_btn.clicked.connect(self._on_use_selection)
        self.inspect_btn = QPushButton("Read node")
        self.inspect_btn.clicked.connect(self._on_inspect)
        node_row.addWidget(QLabel("Node:"))
        node_row.addWidget(self.node_edit, 2)
        node_row.addWidget(self.use_selection_btn)
        node_row.addWidget(self.inspect_btn)
        target_layout.addLayout(node_row)
        self.info_label = QLabel("Select a node and press Read node to see its type, access level and value.")
        self.info_label.setObjectName("hintLabel")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        target_layout.addWidget(self.info_label)
        layout.addWidget(target_box)

        # Write -------------------------------------------------------------
        write_box = QGroupBox("Write value")
        write_layout = QVBoxLayout(write_box)
        value_row = QHBoxLayout()
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("new value, parsed with the node's server-side DataType")
        self.load_value_btn = QPushButton("Use current value")
        self.load_value_btn.clicked.connect(self._on_load_current_value)
        value_row.addWidget(QLabel("Value:"))
        value_row.addWidget(self.value_edit, 1)
        value_row.addWidget(self.load_value_btn)
        write_layout.addLayout(value_row)
        action_row = QHBoxLayout()
        self.restore_check = QCheckBox("Restore the previous value after the read-back")
        self.write_btn = QPushButton("Write value…")
        self.write_btn.setObjectName("primary")
        self.write_btn.clicked.connect(self._on_write)
        action_row.addWidget(self.restore_check)
        action_row.addStretch(1)
        action_row.addWidget(self.write_btn)
        write_layout.addLayout(action_row)
        self.write_status_label = QLabel("Nothing written.")
        self.write_status_label.setObjectName("hintLabel")
        self.write_status_label.setWordWrap(True)
        write_layout.addWidget(self.write_status_label)
        layout.addWidget(write_box)

        # Method call -------------------------------------------------------
        call_box = QGroupBox("Call method")
        call_layout = QVBoxLayout(call_box)
        method_row = QHBoxLayout()
        self.object_edit = QLineEdit()
        self.object_edit.setPlaceholderText("object NodeId, e.g. ns=2;s=Motor")
        self.method_edit = QLineEdit()
        self.method_edit.setPlaceholderText("method NodeId, e.g. ns=2;s=Start")
        self.signature_btn = QPushButton("Read signature")
        self.signature_btn.clicked.connect(self._on_read_signature)
        method_row.addWidget(QLabel("Object:"))
        method_row.addWidget(self.object_edit, 1)
        method_row.addWidget(QLabel("Method:"))
        method_row.addWidget(self.method_edit, 1)
        method_row.addWidget(self.signature_btn)
        call_layout.addLayout(method_row)

        self.args_table = QTableWidget(0, 3)
        self.args_table.setHorizontalHeaderLabels(["Argument", "Type", "Value"])
        self.args_table.verticalHeader().setVisible(False)
        self.args_table.setAlternatingRowColors(True)
        header = self.args_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.args_table.setMaximumHeight(150)
        call_layout.addWidget(self.args_table)

        call_row = QHBoxLayout()
        self.call_btn = QPushButton("Call method…")
        self.call_btn.setObjectName("primary")
        self.call_btn.clicked.connect(self._on_call)
        call_row.addStretch(1)
        call_row.addWidget(self.call_btn)
        call_layout.addLayout(call_row)
        self.call_status_label = QLabel("Nothing called.")
        self.call_status_label.setObjectName("hintLabel")
        self.call_status_label.setWordWrap(True)
        call_layout.addWidget(self.call_status_label)
        self.result_view = QLabel("")
        self.result_view.setObjectName("hintLabel")
        self.result_view.setWordWrap(True)
        self.result_view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_view.setFont(mono_font(9))
        call_layout.addWidget(self.result_view)
        layout.addWidget(call_box)

        self.status_label = QLabel("Not connected.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    # -- connection state --------------------------------------------------
    def set_connected(self, connected: bool) -> None:
        if not connected:
            self._busy = False
        self._update_controls()

    def shutdown(self) -> None:
        self._closed = True

    # -- target ------------------------------------------------------------
    def _on_use_selection(self) -> None:
        if self._selection_provider is None:
            return
        node_id = self._selection_provider()
        if not node_id:
            self._set_status("The address-space tab has no selected node.", error=True)
            return
        self.node_edit.setText(node_id)
        self._on_inspect()

    def _canonical(self, text: str, field: str) -> str | None:
        value = (text or "").strip()
        if not value:
            self._set_status(f"Enter a NodeId in {field}.", error=True)
            return None
        try:
            return parse_node_id(value).to_string()
        except InvalidNodeIdError as exc:
            self._set_status(str(exc), error=True)
            return None

    def _on_inspect(self) -> None:
        if self._busy or not self._service.is_connected:
            return
        canonical = self._canonical(self.node_edit.text(), "the node field")
        if canonical is None:
            return
        self._start(f"Reading {canonical}…")
        self._runner.submit(
            lambda: self._read_target(canonical),
            on_done=self._show_target,
            on_error=lambda message: self._failed("Reading the node", message),
        )

    async def _read_target(self, node_id: str) -> tuple[str, NodeDetails, ValueSnapshot]:
        if not self._service.is_connected:
            raise RuntimeError("Not connected.")
        details = await self._service.read_node_details(node_id)
        value = await self._service.read_value(node_id)
        return node_id, details, value

    def _show_target(self, payload: tuple[str, NodeDetails, ValueSnapshot]) -> None:
        node_id, details, value = payload
        record = details.record
        self._current_node = node_id
        lines = [
            f"{record.display_name or record.browse_name or node_id}  ({record.node_class})",
            f"DataType: {record.data_type_name or record.data_type or 'unknown'}"
            f"   ValueRank: {'' if record.value_rank is None else record.value_rank}",
        ]
        if record.access_level_names:
            lines.append(f"AccessLevel: {', '.join(record.access_level_names)}")
        if record.type_definition_name:
            lines.append(f"Type definition: {record.type_definition_name}")
        lines.append(f"Value: {flatten_for_csv(value.value)}  [{value.status}]")
        self._current_value_text = "" if value.value is None else flatten_for_csv(value.value)
        self.info_label.setText("\n".join(lines))
        self._finish(f"Read {node_id}. Write a value or call a method below.")

    def _on_load_current_value(self) -> None:
        if not self._current_value_text:
            self._set_status("Read the node first; there is no value to copy.", error=True)
            return
        self.value_edit.setText(self._current_value_text)
        self._set_status("The current value was put in the value field; edit it before writing.")

    # -- write -------------------------------------------------------------
    def _on_write(self) -> None:
        if self._busy or not self._service.is_connected:
            return
        canonical = self._canonical(self.node_edit.text(), "the node field")
        if canonical is None:
            return
        value_text = self.value_edit.text().strip()
        if not value_text:
            self._set_status("Enter the value to write.", error=True)
            return
        self._start(f"Checking whether {canonical} accepts {value_text}…")
        self._runner.submit(
            lambda: self._service.write_preview(canonical, value_text),
            on_done=lambda inspection: self._confirm_write(canonical, value_text, inspection),
            on_error=lambda message: self._failed("Checking the write", message),
        )

    def _confirm_write(self, node_id: str, value_text: str, inspection: Inspection) -> None:
        if not inspection.ok:
            self._write_status(False, f"Nothing was written: {inspection.detail}")
            self._finish("The write was refused before sending.")
            return
        previous = self._current_value_text if self._current_node == node_id else ""
        restore = self.restore_check.isChecked()
        text = write_value_confirmation(
            endpoint_url=self._service.endpoint_url,
            node_id=node_id,
            value_preview=inspection.preview,
            data_type_label=inspection.data_type_label or "unknown",
            value_rank=inspection.value_rank,
            restore_previous=restore,
            previous_value=previous,
        )
        if not ask_confirmation(self, "Confirm write", text):
            self._write_status(False, "Write cancelled; nothing was sent.")
            self._finish("Cancelled.")
            return
        self._start(f"Writing {inspection.preview} to {node_id}…")
        self._runner.submit(
            lambda: self._service.write_node_value(
                node_id,
                value_text,
                restore_previous=restore,
                reference=f"write {node_id}",
            ),
            on_done=self._show_write,
            on_error=lambda message: self._failed("The write", message),
        )

    def _show_write(self, result: WriteResult) -> None:
        outcome = result.write
        passed = result.status == "passed"
        self._write_status(passed, result.summary_text())
        lines = [
            f"write: {outcome.write_status_code or outcome.status}",
            f"written: {outcome.new_value}",
            f"read back: {outcome.read_back_value or '-'} [{outcome.read_back_status_code or '-'}]",
            f"previous: {outcome.previous_value or '-'}",
            f"sent: {'yes' if outcome.sent else 'no'}",
            f"duration: {outcome.duration_ms:.0f} ms",
            f"authority: {outcome.authorization.describe() if outcome.authorization else '-'}",
        ]
        if result.restore is not None:
            lines.append(f"restore: {result.restore.status} — {result.restore.detail}")
        self._set_result(lines)
        _logger.info("Interactive write %s: %s", outcome.node_id, result.summary_text())
        self._finish("Write completed.")
        if self._current_node and self._current_node == outcome.node_id:
            self._current_value_text = outcome.read_back_value if passed else self._current_value_text

    # -- method call -------------------------------------------------------
    def _on_read_signature(self) -> None:
        if self._busy or not self._service.is_connected:
            return
        method = self._canonical(self.method_edit.text(), "the method field")
        if method is None:
            return
        self._start(f"Reading the signature of {method}…")
        self._runner.submit(
            lambda: self._service.method_signature(method),
            on_done=lambda rows: self._show_signature(method, rows),
            on_error=lambda message: self._failed("Reading the method signature", message),
        )

    def _show_signature(self, method: str, rows: list[dict[str, Any]]) -> None:
        self.args_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            name = row.get("name") or f"argument {index + 1}"
            label = QTableWidgetItem(str(name))
            label.setFlags(label.flags() & ~Qt.ItemIsEditable)
            type_text = f"{row.get('data_type_name') or row.get('data_type') or 'unknown'}"
            type_item = QTableWidgetItem(type_text)
            type_item.setFlags(type_item.flags() & ~Qt.ItemIsEditable)
            type_item.setToolTip(
                f"{row.get('data_type') or ''}  ValueRank {row.get('value_rank')}"
                + (f"\n{row['description']}" if row.get("description") else "")
            )
            value_item = QTableWidgetItem("")
            value_item.setToolTip("Value for this argument, parsed with the server's InputArguments type.")
            self.args_table.setItem(index, 0, label)
            self.args_table.setItem(index, 1, type_item)
            self.args_table.setItem(index, 2, value_item)
        if not rows:
            self._finish(f"{method} takes no input arguments; press Call method… to call it.")
        else:
            self._finish(f"{method} expects {len(rows)} argument(s). Fill the Value column.")

    def _argument_texts(self) -> list[str]:
        return [
            (self.args_table.item(row, 2).text() if self.args_table.item(row, 2) else "")
            for row in range(self.args_table.rowCount())
        ]

    def _on_call(self) -> None:
        if self._busy or not self._service.is_connected:
            return
        owner = self._canonical(self.object_edit.text(), "the object field")
        if owner is None:
            return
        method = self._canonical(self.method_edit.text(), "the method field")
        if method is None:
            return
        arguments = self._argument_texts()
        self._start(f"Checking {method} on {owner}…")
        self._runner.submit(
            lambda: self._service.call_preview(owner, method, arguments),
            on_done=lambda inspection: self._confirm_call(owner, method, arguments, inspection),
            on_error=lambda message: self._failed("Checking the call", message),
        )

    def _confirm_call(
        self, owner: str, method: str, arguments: list[str], inspection: Inspection
    ) -> None:
        if not inspection.ok:
            self._call_status(False, f"Nothing was called: {inspection.detail}")
            self._finish("The call was refused before sending.")
            return
        text = method_call_confirmation(
            endpoint_url=self._service.endpoint_url,
            object_node_id=owner,
            method_node_id=method,
            arguments=arguments,
        )
        if not ask_confirmation(self, "Confirm method call", text):
            self._call_status(False, "Method call cancelled; nothing was sent.")
            self._finish("Cancelled.")
            return
        self._start(f"Calling {method} on {owner}…")
        self._runner.submit(
            lambda: self._service.invoke_method(owner, method, arguments, reference=f"call {method}"),
            on_done=self._show_call,
            on_error=lambda message: self._failed("The method call", message),
        )

    def _show_call(self, outcome: CallOutcome) -> None:
        passed = outcome.status == "passed"
        self._call_status(passed, outcome.detail or outcome.status)
        lines = [
            f"status: {outcome.status_code or outcome.status}",
            f"arguments: {', '.join(outcome.arguments) or 'none'}",
            f"output: {outcome.output or '-'}",
            f"sent: {'yes' if outcome.sent else 'no'}",
            f"duration: {outcome.duration_ms:.0f} ms",
            f"authority: {outcome.authorization.describe() if outcome.authorization else '-'}",
        ]
        self._set_result(lines, with_json={"output_values": outcome.output_values})
        _logger.info("Method call %s on %s: %s", outcome.method_node_id, outcome.object_node_id, outcome.detail)
        self._finish("Method call completed.")

    # -- status helpers ----------------------------------------------------
    def _start(self, message: str) -> None:
        self._busy = True
        self._update_controls()
        self._set_status(message)

    def _finish(self, message: str) -> None:
        self._busy = False
        self._update_controls()
        self._set_status(message)

    def _failed(self, what: str, message: str) -> None:
        self._busy = False
        self._update_controls()
        _logger.warning("%s failed: %s", what, message)
        self._set_status(f"{what} failed: {message}", error=True)

    def _write_status(self, passed: bool, message: str) -> None:
        self.write_status_label.setText(("" if passed else "⚠ ") + message)
        self.write_status_label.setObjectName("hintLabel" if passed else "errorLabel")
        self._restyle(self.write_status_label)
        if not passed:
            self.write_status_label.setStyleSheet(f"color: {ERROR};")

    def _call_status(self, passed: bool, message: str) -> None:
        self.call_status_label.setText(("" if passed else "⚠ ") + message)
        self.call_status_label.setObjectName("hintLabel" if passed else "errorLabel")
        self._restyle(self.call_status_label)
        if not passed:
            self.call_status_label.setStyleSheet(f"color: {ERROR};")

    def _set_result(self, lines: list[str], *, with_json: dict[str, Any] | None = None) -> None:
        text = "\n".join(lines)
        if with_json:
            try:
                text = text + "\n" + json.dumps(with_json, indent=2, ensure_ascii=False, default=str)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                text = text + "\n" + repr(with_json)
        self.result_view.setText(text)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(("⚠ " if error else "") + message)
        self.status_label.setObjectName("errorLabel" if error else "hintLabel")
        self._restyle(self.status_label)

    @staticmethod
    def _restyle(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def _update_controls(self) -> None:
        connected = self._service.is_connected
        enabled = connected and not self._busy
        for widget in (
            self.node_edit,
            self.use_selection_btn,
            self.inspect_btn,
            self.value_edit,
            self.load_value_btn,
            self.restore_check,
            self.write_btn,
            self.object_edit,
            self.method_edit,
            self.signature_btn,
            self.args_table,
            self.call_btn,
        ):
            widget.setEnabled(enabled)
