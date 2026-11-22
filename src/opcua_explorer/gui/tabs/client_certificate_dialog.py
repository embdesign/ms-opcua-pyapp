"""Create the client certificate this application needs, from the Connection tab.

The dialog only collects the request: it writes nothing. The tab confirms the
exact files and runs the pure generator off the GUI thread, so the operator sees
which key and which certificate (or signing request) appear on disk before they do.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...certificate_creation import CertificateCreationError, planned_paths
from ...defaults import (
    CERTIFICATE_KEY_SIZES,
    DEFAULT_CERTIFICATE_VALIDITY_DAYS,
    MAX_CERTIFICATE_VALIDITY_DAYS,
)
from ...models import CERTIFICATE_MODES, CertificateSpec

_logger = logging.getLogger(__name__)

_MODE_LABELS = {
    "self-signed": "Self-signed certificate (no CA)",
    "csr": "Certificate signing request (CSR) for a CA",
}


class ClientCertificateDialog(QDialog):
    """Collect the common name, mode, validity and folder of one certificate."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        common_name: str = "",
        application_uri: str = "",
        output_dir: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create client certificate")
        self.setModal(True)
        self.resize(680, 380)
        self._application_uri = application_uri

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Create a client certificate on this computer")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        intro = QLabel(
            "The server needs a certificate whose subjectAltName contains this application's URI. "
            "Create a self-signed certificate when you can add it to the server's trust list, or a "
            "signing request when a CA issues certificates for this application."
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.mode_combo = QComboBox()
        for mode in CERTIFICATE_MODES:
            self.mode_combo.addItem(_MODE_LABELS.get(mode, mode), mode)
        self.mode_combo.currentIndexChanged.connect(self._sync_state)
        form.addRow("Mode:", self.mode_combo)

        self.common_name_edit = QLineEdit(common_name or "opcua-explorer-client")
        self.common_name_edit.textChanged.connect(self._sync_state)
        form.addRow("Common name (CN):", self.common_name_edit)

        uri_label = QLabel(application_uri or "(no application URI configured)")
        uri_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        uri_label.setWordWrap(True)
        form.addRow("Application URI:", uri_label)

        self.validity_spin = QSpinBox()
        self.validity_spin.setRange(1, MAX_CERTIFICATE_VALIDITY_DAYS)
        self.validity_spin.setValue(DEFAULT_CERTIFICATE_VALIDITY_DAYS)
        self.validity_spin.setSuffix(" day(s)")
        self.validity_spin.setToolTip("Lifetime of a self-signed certificate; a CSR has none until a CA signs it.")
        form.addRow("Validity:", self.validity_spin)

        self.key_size_combo = QComboBox()
        for size in CERTIFICATE_KEY_SIZES:
            self.key_size_combo.addItem(f"RSA {size} bit", size)
        form.addRow("Key:", self.key_size_combo)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(output_dir)
        self.folder_edit.textChanged.connect(self._sync_state)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("Folder:", folder_row)

        layout.addLayout(form)

        self.files_label = QLabel()
        self.files_label.setObjectName("hintLabel")
        self.files_label.setWordWrap(True)
        self.files_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.files_label)

        layout.addStretch(1)

        buttons = QDialogButtonBox()
        self._create_button = buttons.addButton("Create…", QDialogButtonBox.AcceptRole)
        self._create_button.setObjectName("primary")
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync_state()

    # -- state -------------------------------------------------------------
    @property
    def mode(self) -> str:
        return str(self.mode_combo.currentData() or CERTIFICATE_MODES[0])

    def spec(self) -> CertificateSpec:
        """The request this dialog describes (already normalised by the model)."""
        return CertificateSpec(
            mode=self.mode,
            common_name=self.common_name_edit.text(),
            application_uri=self._application_uri,
            validity_days=int(self.validity_spin.value()),
            key_size=int(self.key_size_combo.currentData() or CERTIFICATE_KEY_SIZES[0]),
            output_dir=self.folder_edit.text(),
        ).normalised()

    def _sync_state(self) -> None:
        self.validity_spin.setEnabled(self.mode != "csr")
        spec = self.spec()
        try:
            main, key = planned_paths(spec)
        except CertificateCreationError as exc:
            self.files_label.setText(str(exc))
            self._create_button.setEnabled(False)
            return
        existing = [str(path) for path in (main, key) if path.exists()]
        if existing:
            self.files_label.setText(
                "These files already exist and are not replaced:\n  " + "\n  ".join(existing)
            )
            self._create_button.setEnabled(False)
            return
        kind = "request" if spec.is_request else "certificate"
        self.files_label.setText(
            f"Will write the {kind} and its key:\n  {main}\n  {key}\n"
            "The key is unencrypted (file mode 600); nothing is sent to a server."
        )
        self._create_button.setEnabled(True)

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Certificate folder", self.folder_edit.text() or str(Path.home())
        )
        if folder:
            self.folder_edit.setText(folder)

    # -- entry point -------------------------------------------------------
    @classmethod
    def ask(
        cls,
        parent: QWidget | None,
        *,
        common_name: str = "",
        application_uri: str = "",
        output_dir: str = "",
    ) -> CertificateSpec | None:
        dialog = cls(
            parent,
            common_name=common_name,
            application_uri=application_uri,
            output_dir=output_dir,
        )
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.spec()


__all__ = ["ClientCertificateDialog"]
