"""Security dialogs of the Connection tab.

Keeping them out of ``connection_tab.py`` leaves that module focused on the
connection form, discovery/connect flow and the reconnect state machine.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...models import EndpointInfo
from ...opcua.client_service import OpcUaService
from ...opcua.security import CertificateSummary
from ..theme import mono_font

_logger = logging.getLogger(__name__)


def _selectable(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setWordWrap(True)
    return label


class CertificateReviewDialog(QDialog):
    """Blocking, explicit approval of an unknown server certificate."""

    def __init__(
        self,
        parent: QWidget | None,
        endpoint: EndpointInfo,
        summary: CertificateSummary,
        *,
        replacing_fingerprint: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review server certificate")
        self.setModal(True)
        self.resize(720, 520)
        self._summary = summary

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Unknown server certificate")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        intro = QLabel(
            "This server presented a certificate that has not been approved on this computer. "
            "Compare the SHA-256 fingerprint with the value published by the server's operator. "
            "If it does not match, do not connect."
        )
        intro.setWordWrap(True)
        intro.setObjectName("warningLabel")
        layout.addWidget(intro)

        if replacing_fingerprint:
            changed = QLabel(
                "This endpoint was trusted before with a different certificate. "
                "A changed certificate can be legitimate (renewal) or a sign of interception."
            )
            changed.setWordWrap(True)
            changed.setObjectName("errorLabel")
            layout.addWidget(changed)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow("Endpoint:", _selectable(endpoint.endpoint_url))
        form.addRow("Security:", _selectable(endpoint.label()))
        form.addRow("Subject:", _selectable(summary.subject or "(could not be parsed)"))
        form.addRow("Issuer:", _selectable(summary.issuer or "(could not be parsed)"))
        form.addRow("Valid from:", _selectable(summary.not_before or "unknown"))
        form.addRow("Valid until:", _selectable(summary.not_after or "unknown"))
        if summary.dns_names:
            form.addRow("DNS names:", _selectable(", ".join(summary.dns_names)))
        if summary.parse_error:
            form.addRow("Parse error:", _selectable(summary.parse_error))
        layout.addLayout(form)

        fingerprint_label = QLabel("SHA-256 fingerprint")
        fingerprint_label.setObjectName("subTitle")
        layout.addWidget(fingerprint_label)

        fingerprint = QLabel(summary.fingerprint_colon)
        fingerprint.setObjectName("fingerprint")
        fingerprint.setFont(mono_font(9))
        fingerprint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fingerprint.setWordWrap(True)
        layout.addWidget(fingerprint)

        if summary.is_expired:
            expired = QLabel("This certificate is expired. Prefer fixing the server certificate first.")
            expired.setObjectName("errorLabel")
            expired.setWordWrap(True)
            layout.addWidget(expired)

        self._confirm = QCheckBox("I verified this fingerprint out-of-band and trust this server")
        self._confirm.toggled.connect(self._sync_buttons)
        layout.addWidget(self._confirm)

        buttons = QDialogButtonBox()
        self._trust_button = buttons.addButton("Trust and connect", QDialogButtonBox.AcceptRole)
        self._trust_button.setObjectName("primary")
        cancel = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._trust_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        cancel.setDefault(True)
        layout.addWidget(buttons)

    def _sync_buttons(self, checked: bool) -> None:
        self._trust_button.setEnabled(checked)

    @classmethod
    def ask(
        cls,
        parent: QWidget | None,
        endpoint: EndpointInfo,
        summary: CertificateSummary,
        *,
        replacing_fingerprint: str | None = None,
    ) -> bool:
        dialog = cls(parent, endpoint, summary, replacing_fingerprint=replacing_fingerprint)
        return dialog.exec() == QDialog.Accepted


class TrustStoreDialog(QDialog):
    """Review and remove previously approved server certificates."""

    def __init__(self, parent: QWidget | None, service: OpcUaService) -> None:
        super().__init__(parent)
        self._service = service
        self.setWindowTitle("Trusted server certificates")
        self.resize(860, 420)

        layout = QVBoxLayout(self)
        note = QLabel(
            "Certificates listed here were approved explicitly by an operator. "
            "Removing an entry means the next connection to that server requires a new review."
        )
        note.setWordWrap(True)
        note.setObjectName("hintLabel")
        layout.addWidget(note)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["SHA-256 (first 32)", "Subject", "Endpoint", "Approved", "Valid until"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        for index in range(4):
            header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        remove = QPushButton("Remove selected")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_selected)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._reload()

    def _reload(self) -> None:
        entries = self._service.trust_store.entries
        self._table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = [
                entry.sha256[:32],
                entry.subject,
                entry.endpoint_url,
                entry.added_utc,
                entry.not_after or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setToolTip(entry.sha256)
                self._table.setItem(row, column, item)

    def _remove_selected(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        sha_item = self._table.item(row, 0)
        if sha_item is None:
            return
        entries = self._service.trust_store.entries
        if row >= len(entries):
            return
        entry = entries[row]
        if QMessageBox.question(
            self,
            "Remove trusted certificate",
            f"Remove the approval for\n{entry.subject or 'this certificate'}\n\n{entry.endpoint_url}?",
        ) == QMessageBox.Yes:
            self._service.trust_store.remove(entry.sha256)
            _logger.info("Removed trusted server certificate %s…", entry.sha256[:16])
            self._reload()
