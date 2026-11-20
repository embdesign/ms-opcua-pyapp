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
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...errors import InvalidNodeIdError
from ...formatting import flatten_for_csv
from ...models import (
    CREATABLE_NODE_CLASSES,
    WRITABLE_TEXT_ATTRIBUTES,
    NodeCreateOutcome,
    NodeCreateRequest,
    NodeDeleteOutcome,
    NodeDetails,
    ValueSnapshot,
)
from ...opcua.browse import parse_node_id
from ...opcua.client_service import OpcUaService
from ...opcua.write_service import AttributeWriteOutcome, CallOutcome, Inspection, WriteResult
from ...operator_text import (
    attribute_write_confirmation,
    method_call_confirmation,
    node_create_confirmation,
    node_delete_confirmation,
    write_value_confirmation,
)
from ..async_runner import AsyncRunner
from ..theme import ERROR, mono_font
from .base import SessionTab

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


class WriteCallTab(SessionTab):
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
        #: GUI-side connection state pushed by ``set_connected``.
        self._connected = False
        self._endpoint_url = ""
        self._selection_provider = selection_provider
        self._busy = False
        self._closed = False
        self._current_node: str = ""
        self._current_value_text: str = ""
        self._current_attribute_text: dict[str, str] = {}

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

        # Attribute write ---------------------------------------------------
        attribute_box = QGroupBox("Write metadata attribute")
        attribute_layout = QVBoxLayout(attribute_box)
        attribute_row = QHBoxLayout()
        self.attribute_combo = QComboBox()
        self.attribute_combo.addItems(list(WRITABLE_TEXT_ATTRIBUTES))
        self.attribute_edit = QLineEdit()
        self.attribute_edit.setPlaceholderText("new text for the selected attribute")
        self.load_attribute_btn = QPushButton("Use current text")
        self.load_attribute_btn.clicked.connect(self._on_load_attribute)
        attribute_row.addWidget(QLabel("Attribute:"))
        attribute_row.addWidget(self.attribute_combo)
        attribute_row.addWidget(self.attribute_edit, 1)
        attribute_row.addWidget(self.load_attribute_btn)
        attribute_layout.addLayout(attribute_row)
        attribute_action = QHBoxLayout()
        note = QLabel(
            "Only DisplayName and Description are written; servers usually allow that for privileged users "
            "only, and a refusal is reported with its StatusCode."
        )
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        self.attribute_write_btn = QPushButton("Write attribute…")
        self.attribute_write_btn.clicked.connect(self._on_attribute_write)
        attribute_action.addWidget(note, 1)
        attribute_action.addWidget(self.attribute_write_btn)
        attribute_layout.addLayout(attribute_action)
        self.attribute_status_label = QLabel("Nothing written.")
        self.attribute_status_label.setObjectName("hintLabel")
        self.attribute_status_label.setWordWrap(True)
        attribute_layout.addWidget(self.attribute_status_label)
        layout.addWidget(attribute_box)

        # Node management ---------------------------------------------------
        node_box = QGroupBox("Node management (structure)")
        node_layout = QVBoxLayout(node_box)
        parent_row = QHBoxLayout()
        self.parent_edit = QLineEdit()
        self.parent_edit.setPlaceholderText("parent NodeId, e.g. i=85 or ns=2;s=Plant")
        self.node_class_combo = QComboBox()
        self.node_class_combo.addItems(list(CREATABLE_NODE_CLASSES))
        self.browse_name_edit = QLineEdit()
        self.browse_name_edit.setPlaceholderText("BrowseName of the new node")
        self.display_name_edit = QLineEdit()
        self.display_name_edit.setPlaceholderText("DisplayName (default: the BrowseName)")
        parent_row.addWidget(QLabel("Parent:"))
        parent_row.addWidget(self.parent_edit, 2)
        parent_row.addWidget(QLabel("Class:"))
        parent_row.addWidget(self.node_class_combo)
        parent_row.addWidget(QLabel("BrowseName:"))
        parent_row.addWidget(self.browse_name_edit, 1)
        node_layout.addLayout(parent_row)

        detail_row = QHBoxLayout()
        self.namespace_spin = QSpinBox()
        self.namespace_spin.setRange(-1, 255)
        self.namespace_spin.setValue(-1)
        self.namespace_spin.setSpecialValueText("parent namespace")
        self.namespace_spin.setToolTip("Namespace of the BrowseName; the NodeId is always assigned by the server.")
        self.node_data_type_edit = QLineEdit("i=11")
        self.node_data_type_edit.setToolTip("DataType NodeId of a new Variable, e.g. i=11 (Double).")
        self.node_value_edit = QLineEdit("0")
        self.node_value_edit.setToolTip("Initial value of a new Variable, parsed with the server's DataType.")
        self.create_node_btn = QPushButton("Create node…")
        self.create_node_btn.clicked.connect(self._on_create_node)
        self.delete_node_btn = QPushButton("Delete target node…")
        self.delete_node_btn.setToolTip("Delete the NodeId in the target field above.")
        self.delete_node_btn.clicked.connect(self._on_delete_node)
        detail_row.addWidget(QLabel("DisplayName:"))
        detail_row.addWidget(self.display_name_edit, 1)
        detail_row.addWidget(QLabel("Namespace:"))
        detail_row.addWidget(self.namespace_spin)
        detail_row.addWidget(QLabel("DataType:"))
        detail_row.addWidget(self.node_data_type_edit)
        detail_row.addWidget(QLabel("Value:"))
        detail_row.addWidget(self.node_value_edit)
        node_layout.addLayout(detail_row)

        node_action = QHBoxLayout()
        node_note = QLabel(
            "One node per request, with a NodeId the server assigns and a read-back. Servers usually allow node "
            "management for administrators only; a deletion is neither checked for children nor undoable."
        )
        node_note.setObjectName("hintLabel")
        node_note.setWordWrap(True)
        node_action.addWidget(node_note, 1)
        node_action.addWidget(self.create_node_btn)
        node_action.addWidget(self.delete_node_btn)
        node_layout.addLayout(node_action)
        self.node_status_label = QLabel("Nothing created or deleted.")
        self.node_status_label.setObjectName("hintLabel")
        self.node_status_label.setWordWrap(True)
        node_layout.addWidget(self.node_status_label)
        layout.addWidget(node_box)

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
    def set_connected(self, connected: bool, connection: Any = None) -> None:
        self._connected = bool(connected)
        if connection is not None:
            self._endpoint_url = str(getattr(connection, "endpoint_url", "") or "")
        if not connected:
            self._busy = False
        self._update_controls()

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
        if self._busy or not self._connected:
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
        if not self._connected:
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
        self._current_attribute_text = {
            "DisplayName": record.display_name or "",
            "Description": record.description or "",
        }
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
        if self._busy or not self._connected:
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
            endpoint_url=self._endpoint_url,
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

    # -- attribute write ---------------------------------------------------
    def _on_load_attribute(self) -> None:
        name = self.attribute_combo.currentText()
        text = self._current_attribute_text.get(name, "")
        if not text:
            self._set_status("Read the node first; there is no current text to copy.", error=True)
            return
        self.attribute_edit.setText(text)
        self._set_status(f"The current {name} was put in the text field; edit it before writing.")

    def _on_attribute_write(self) -> None:
        if self._busy or not self._connected:
            return
        canonical = self._canonical(self.node_edit.text(), "the node field")
        if canonical is None:
            return
        name = self.attribute_combo.currentText()
        text = self.attribute_edit.text().strip()
        if not text:
            self._set_status("Enter the new attribute text.", error=True)
            return
        self._start(f"Checking {name} of {canonical}…")
        self._runner.submit(
            lambda: self._service.attribute_write_preview(canonical, name, text),
            on_done=lambda inspection: self._confirm_attribute_write(canonical, name, text, inspection),
            on_error=lambda message: self._failed("Checking the attribute write", message),
        )

    def _confirm_attribute_write(
        self, node_id: str, name: str, text: str, inspection: Inspection
    ) -> None:
        if not inspection.ok:
            self._attribute_status(False, f"Nothing was written: {inspection.detail}")
            self._finish("The attribute write was refused before sending.")
            return
        previous = self._current_attribute_text.get(name, "") if self._current_node == node_id else ""
        confirmation = attribute_write_confirmation(
            endpoint_url=self._endpoint_url,
            node_id=node_id,
            attribute_name=name,
            text=text,
            previous_text=previous,
        )
        if not ask_confirmation(self, "Confirm attribute write", confirmation):
            self._attribute_status(False, "Attribute write cancelled; nothing was sent.")
            self._finish("Cancelled.")
            return
        self._start(f"Writing {name} of {node_id}…")
        self._runner.submit(
            lambda: self._service.write_attribute_value(
                node_id, name, text, reference=f"write {name} of {node_id}"
            ),
            on_done=self._show_attribute_write,
            on_error=lambda message: self._failed("The attribute write", message),
        )

    def _show_attribute_write(self, outcome: AttributeWriteOutcome) -> None:
        passed = outcome.status == "passed"
        self._attribute_status(passed, outcome.detail or outcome.status)
        self._set_result(
            [
                f"attribute: {outcome.attribute_name}",
                f"write: {outcome.write_status_code or outcome.status}",
                f"written: {outcome.text!r}",
                f"read back: {outcome.read_back_text!r} [{outcome.read_back_status_code or '-'}]",
                f"previous: {outcome.previous_text!r}",
                f"sent: {'yes' if outcome.sent else 'no'}",
                f"duration: {outcome.duration_ms:.0f} ms",
                f"authority: {outcome.authorization.describe() if outcome.authorization else '-'}",
            ]
        )
        _logger.info(
            "Attribute write %s of %s: %s", outcome.attribute_name, outcome.node_id, outcome.detail
        )
        self._finish("Attribute write completed.")
        if passed and self._current_node == outcome.node_id:
            self._current_attribute_text[outcome.attribute_name] = outcome.read_back_text

    def _attribute_status(self, passed: bool, message: str) -> None:
        self.attribute_status_label.setText(("" if passed else "⚠ ") + message)
        self.attribute_status_label.setObjectName("hintLabel" if passed else "errorLabel")
        self._restyle(self.attribute_status_label)
        if not passed:
            self.attribute_status_label.setStyleSheet(f"color: {ERROR};")

    # -- node management ---------------------------------------------------
    def _node_request(self) -> NodeCreateRequest:
        return NodeCreateRequest(
            parent_node_id=self.parent_edit.text(),
            browse_name=self.browse_name_edit.text(),
            node_class=self.node_class_combo.currentText(),
            display_name=self.display_name_edit.text(),
            data_type=self.node_data_type_edit.text(),
            value_text=self.node_value_edit.text(),
            namespace_index=int(self.namespace_spin.value()),
        ).normalised()

    def _on_create_node(self) -> None:
        if self._busy or not self._connected:
            return
        request = self._node_request()
        if not request.parent_node_id:
            self._set_status("Enter the parent NodeId for the new node.", error=True)
            return
        if not request.browse_name:
            self._set_status("Enter a BrowseName for the new node.", error=True)
            return
        self._start(f"Checking whether {request.browse_name!r} can be created…")
        self._runner.submit(
            lambda: self._service.node_create_preview(request),
            on_done=lambda inspection: self._confirm_create_node(request, inspection),
            on_error=lambda message: self._failed("Checking the node creation", message),
        )

    def _confirm_create_node(self, request: NodeCreateRequest, inspection: Inspection) -> None:
        if not inspection.ok:
            self._node_status(False, f"Nothing was created: {inspection.detail}")
            self._finish("The node creation was refused before sending.")
            return
        text = node_create_confirmation(
            endpoint_url=self._endpoint_url,
            node_class=request.node_class,
            browse_name=request.browse_name,
            parent_node_id=request.parent_node_id,
            display_name=request.display_name,
            data_type=request.data_type,
            value_text=request.value_text,
        )
        if not ask_confirmation(self, "Confirm node creation", text):
            self._node_status(False, "Node creation cancelled; nothing was sent.")
            self._finish("Cancelled.")
            return
        self._start(f"Creating {request.browse_name!r} below {request.parent_node_id}…")
        self._runner.submit(
            lambda: self._service.create_node(
                request, reference=f"create {request.node_class} {request.browse_name}"
            ),
            on_done=self._show_create_node,
            on_error=lambda message: self._failed("The node creation", message),
        )

    def _show_create_node(self, outcome: NodeCreateOutcome) -> None:
        passed = outcome.status == "passed"
        self._node_status(passed, outcome.summary_text())
        self._set_result(
            [
                f"create: {outcome.status_code or outcome.status}",
                f"new node: {outcome.node_id or '-'}",
                f"read back: {outcome.read_back_name or '-'}",
                f"verified: {outcome.verified}",
                f"sent: {'yes' if outcome.sent else 'no'}",
                f"duration: {outcome.duration_ms:.0f} ms",
                f"authority: {outcome.authorization.describe() if outcome.authorization else '-'}",
            ]
        )
        _logger.info("Node creation: %s", outcome.summary_text())
        self._finish("Node creation completed.")
        if passed and outcome.node_id:
            self.node_edit.setText(outcome.node_id)

    def _on_delete_node(self) -> None:
        if self._busy or not self._connected:
            return
        canonical = self._canonical(self.node_edit.text(), "the node field")
        if canonical is None:
            return
        text = node_delete_confirmation(endpoint_url=self._endpoint_url, node_id=canonical)
        if not ask_confirmation(self, "Confirm node deletion", text):
            self._node_status(False, "Node deletion cancelled; nothing was sent.")
            self._finish("Cancelled.")
            return
        self._start(f"Deleting {canonical}…")
        self._runner.submit(
            lambda: self._service.delete_node(canonical, reference=f"delete {canonical}"),
            on_done=self._show_delete_node,
            on_error=lambda message: self._failed("The node deletion", message),
        )

    def _show_delete_node(self, outcome: NodeDeleteOutcome) -> None:
        passed = outcome.status == "passed"
        self._node_status(passed, outcome.summary_text())
        self._set_result(
            [
                f"delete: {outcome.status_code or outcome.status}",
                f"node: {outcome.node_id}",
                f"verified gone: {outcome.verified}",
                f"sent: {'yes' if outcome.sent else 'no'}",
                f"duration: {outcome.duration_ms:.0f} ms",
                f"authority: {outcome.authorization.describe() if outcome.authorization else '-'}",
            ]
        )
        _logger.info("Node deletion: %s", outcome.summary_text())
        self._finish("Node deletion completed.")

    def _node_status(self, passed: bool, message: str) -> None:
        self.node_status_label.setText(("" if passed else "⚠ ") + message)
        self.node_status_label.setObjectName("hintLabel" if passed else "errorLabel")
        self._restyle(self.node_status_label)
        if not passed:
            self.node_status_label.setStyleSheet(f"color: {ERROR};")

    # -- method call -------------------------------------------------------
    def _on_read_signature(self) -> None:
        if self._busy or not self._connected:
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
        if self._busy or not self._connected:
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
            endpoint_url=self._endpoint_url,
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

    def _update_controls(self) -> None:
        connected = self._connected
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
            self.attribute_combo,
            self.attribute_edit,
            self.load_attribute_btn,
            self.attribute_write_btn,
            self.parent_edit,
            self.node_class_combo,
            self.browse_name_edit,
            self.display_name_edit,
            self.namespace_spin,
            self.node_data_type_edit,
            self.node_value_edit,
            self.create_node_btn,
            self.delete_node_btn,
        ):
            widget.setEnabled(enabled)
