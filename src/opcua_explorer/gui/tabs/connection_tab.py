"""Connection, security and identity tab."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...certificate_creation import CertificateCreationError, create_certificate, planned_paths
from ...config import AppSettings, ConnectionProfile, ConnectionSettings, config_dir
from ...errors import EndpointNotAdvertisedError
from ...models import ConnectionInfo, DiscoveryResult
from ...opcua.client_service import OpcUaService
from ...opcua.security import (
    all_policies,
    check_client_certificate,
    is_secure,
    policy_from_uri,
    security_warnings,
    select_endpoint,
    summarize_certificate,
)
from ...operator_text import (
    client_certificate_confirmation,
    client_certificate_warning,
    connection_diagnostics_text,
)
from ..async_runner import AsyncRunner, TaskHandle
from ..tab_notice import TabNotice
from ..theme import mono_font, set_chip_state
from .client_certificate_dialog import ClientCertificateDialog
from .connection_dialogs import CertificateReviewDialog, TrustStoreDialog
from .server_directory_dialog import ServerDirectoryDialog

_logger = logging.getLogger(__name__)


class ConnectionTab(QWidget):
    """Endpoint discovery, security selection, identity and diagnostics."""

    connected = Signal(object)  # ConnectionInfo
    disconnected = Signal()
    #: Emitted once when the session is lost without the operator clicking Disconnect.
    connection_lost = Signal()
    #: Emitted when the client starts retrying a dropped link in the background.
    reconnect_started = Signal()
    #: Emitted when those retries restored the session.
    connection_restored = Signal()
    endpoints_discovered = Signal(object)  # DiscoveryResult

    def __init__(self, runner: AsyncRunner, service: OpcUaService, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        self._settings = settings
        self._handle: TaskHandle | None = None
        self._state_handle: TaskHandle | None = None
        #: Handle of a running certificate creation, so a closing tab can drop its result.
        self._certificate_handle: TaskHandle | None = None
        #: Handle of the pre-connect client-certificate check.
        self._cert_check_handle: TaskHandle | None = None
        #: Set by :meth:`shutdown`; a late result must not touch deleted widgets.
        self._closed = False
        self._pending: ConnectionSettings | None = None
        self._connect_after_discovery = False
        self._discovery: DiscoveryResult | None = None
        self._application_uri = service.application_uri
        self._link_lost_reported = False
        self._connected_info: ConnectionInfo | None = None
        self._reconnecting = False
        #: True while the connected client has asyncua's reconnect supervisor enabled.
        self._auto_reconnect_active = False

        self._state_timer = QTimer(self)
        self._state_timer.setInterval(3000)
        self._state_timer.timeout.connect(self._poll_state)

        self._build_ui()
        self._sync_security_controls()
        self._sync_identity_page()
        self._set_connected_ui(False)
        self._load_settings(settings)
        self._reload_profiles()

    # -- UI construction ---------------------------------------------------
    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)

        outer.addWidget(self._build_form_column(), 5)
        outer.addWidget(self._build_right_column(), 6)

    def _build_form_column(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)

        heading = QLabel("Connection & security")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        layout.addWidget(
            TabNotice(
                "Nothing is downgraded automatically: the exact policy and mode you choose is what gets used.",
                "Nothing is downgraded automatically: the exact policy and mode you choose is what gets used.",
            )
        )

        # Endpoint ---------------------------------------------------------
        endpoint_box = QGroupBox("Endpoint")
        endpoint_layout = QVBoxLayout(endpoint_box)

        profile_row = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(160)
        self.profile_combo.activated.connect(self._on_profile_selected)
        save_profile = QPushButton("Save profile…")
        save_profile.clicked.connect(self._on_save_profile)
        delete_profile = QPushButton("Delete")
        delete_profile.clicked.connect(self._on_delete_profile)
        profile_row.addWidget(QLabel("Profile:"))
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(save_profile)
        profile_row.addWidget(delete_profile)
        endpoint_layout.addLayout(profile_row)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("opc.tcp://192.168.0.10:4840")
        self.url_edit.returnPressed.connect(self._on_discover)
        # The endpoints this window connected to successfully before; picking one
        # only fills the field, it never connects by itself.
        self.recent_combo = QComboBox()
        self.recent_combo.setMinimumWidth(220)
        self.recent_combo.setToolTip(
            "Endpoints connected to successfully in this profile. Selecting one fills the field; "
            "nothing connects until you press Discover endpoints."
        )
        self.recent_combo.activated.connect(self._on_recent_activated)
        self._refresh_recent()
        url_row = QHBoxLayout()
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(QLabel("Recent:"))
        url_row.addWidget(self.recent_combo)
        endpoint_layout.addLayout(url_row)
        row = QHBoxLayout()
        self.discover_btn = QPushButton("Discover endpoints")
        self.discover_btn.clicked.connect(self._on_discover)
        self.find_servers_btn = QPushButton("Find servers…")
        self.find_servers_btn.setToolTip(
            "Ask a discovery endpoint (LDS/GDS or any server) which applications it knows (FindServers)."
        )
        self.find_servers_btn.clicked.connect(self._on_find_servers)
        self.trust_btn = QPushButton("Trusted certificates…")
        self.trust_btn.clicked.connect(self._on_manage_trust)
        row.addWidget(self.discover_btn)
        row.addWidget(self.find_servers_btn)
        row.addWidget(self.trust_btn)
        row.addStretch(1)
        endpoint_layout.addLayout(row)
        layout.addWidget(endpoint_box)

        # Security ---------------------------------------------------------
        security_box = QGroupBox("Security")
        security_layout = QFormLayout(security_box)
        security_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.policy_combo = QComboBox()
        for info in all_policies():
            label = info.label + (" (deprecated)" if info.deprecated else "")
            self.policy_combo.addItem(label, info.uri)
            if info.note:
                self.policy_combo.setItemData(self.policy_combo.count() - 1, info.note, Qt.ToolTipRole)
        self.policy_combo.currentIndexChanged.connect(self._sync_security_controls)
        security_layout.addRow("Security policy:", self.policy_combo)

        self.mode_combo = QComboBox()
        for name in ("None", "Sign", "SignAndEncrypt"):
            self.mode_combo.addItem(name, name)
        self.mode_combo.currentIndexChanged.connect(self._sync_security_controls)
        security_layout.addRow("Message security mode:", self.mode_combo)
        layout.addWidget(security_box)

        self.security_warning = QLabel()
        self.security_warning.setObjectName("warningLabel")
        self.security_warning.setWordWrap(True)
        self.security_warning.hide()
        layout.addWidget(self.security_warning)

        # Identity ---------------------------------------------------------
        identity_box = QGroupBox("Identity")
        identity_layout = QVBoxLayout(identity_box)
        radios = QHBoxLayout()
        self.anonymous_radio = QRadioButton("Anonymous")
        self.username_radio = QRadioButton("Username / password")
        self.certificate_radio = QRadioButton("Certificate")
        self.anonymous_radio.setChecked(True)
        for radio in (self.anonymous_radio, self.username_radio, self.certificate_radio):
            radio.toggled.connect(self._sync_identity_page)
            radios.addWidget(radio)
        radios.addStretch(1)
        identity_layout.addLayout(radios)

        self.identity_stack = QStackedWidget()
        page_anonymous = QLabel("The server decides which identity tokens it accepts; see the endpoint list.")
        page_anonymous.setObjectName("hintLabel")
        page_anonymous.setWordWrap(True)

        page_username = QWidget()
        username_layout = QFormLayout(page_username)
        username_layout.setContentsMargins(0, 4, 0, 0)
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("user name (saved in profiles)")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("password (never saved, never logged)")
        self.clear_password_check = QCheckBox("Clear the password field after connecting")
        self.clear_password_check.setChecked(True)
        username_layout.addRow("User name:", self.username_edit)
        username_layout.addRow("Password:", self.password_edit)
        username_layout.addRow("", self.clear_password_check)

        page_certificate = QLabel(
            "The client certificate and private key below are also used as the user identity "
            "(ActivateSession with a certificate token). The server must trust this certificate."
        )
        page_certificate.setObjectName("hintLabel")
        page_certificate.setWordWrap(True)

        self.identity_stack.addWidget(page_anonymous)
        self.identity_stack.addWidget(page_username)
        self.identity_stack.addWidget(page_certificate)
        identity_layout.addWidget(self.identity_stack)
        layout.addWidget(identity_box)

        # Certificates -----------------------------------------------------
        # Group-box titles are drawn with Qt::TextShowMnemonic: '&&' is one '&'.
        cert_box = QGroupBox("Client certificate && private key")
        cert_layout = QGridLayout(cert_box)
        cert_layout.setColumnStretch(1, 1)
        self.cert_edit = QLineEdit()
        self.cert_edit.setPlaceholderText("client certificate (.der or .pem)")
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("private key (.pem)")
        self.key_password_edit = QLineEdit()
        self.key_password_edit.setEchoMode(QLineEdit.Password)
        self.key_password_edit.setPlaceholderText("private-key password (optional, never saved)")

        cert_layout.addWidget(QLabel("Certificate:"), 0, 0)
        cert_layout.addWidget(self.cert_edit, 0, 1)
        cert_browse = QPushButton("Browse…")
        cert_browse.clicked.connect(lambda: self._browse_file(self.cert_edit, "Certificates (*.der *.pem *.crt);;All files (*)"))
        cert_layout.addWidget(cert_browse, 0, 2)

        cert_layout.addWidget(QLabel("Private key:"), 1, 0)
        cert_layout.addWidget(self.key_edit, 1, 1)
        key_browse = QPushButton("Browse…")
        key_browse.clicked.connect(lambda: self._browse_file(self.key_edit, "Private keys (*.pem *.key);;All files (*)"))
        cert_layout.addWidget(key_browse, 1, 2)

        cert_layout.addWidget(QLabel("Key password:"), 2, 0)
        cert_layout.addWidget(self.key_password_edit, 2, 1, 1, 2)

        self.create_cert_btn = QPushButton("Create certificate…")
        self.create_cert_btn.setToolTip(
            "Creates a self-signed client certificate or a certificate signing request on this computer, "
            "with this application URI as subjectAltName. Nothing is installed on a server."
        )
        self.create_cert_btn.clicked.connect(self._on_create_certificate)
        cert_layout.addWidget(self.create_cert_btn, 3, 0, 1, 3)
        layout.addWidget(cert_box)

        self.cert_result_label = QLabel()
        self.cert_result_label.setObjectName("hintLabel")
        self.cert_result_label.setWordWrap(True)
        self.cert_result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cert_result_label.hide()
        layout.addWidget(self.cert_result_label)

        uri_note = QLabel(
            "The client certificate must contain this application URI as a subjectAltName URI, "
            f"otherwise asyncua rejects it before connecting:\n{self._application_uri}"
        )
        uri_note.setObjectName("hintLabel")
        uri_note.setWordWrap(True)
        uri_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(uri_note)

        # Timeouts ---------------------------------------------------------
        timeout_box = QGroupBox("Timeouts")
        timeout_layout = QGridLayout(timeout_box)
        self.connect_timeout_spin = QDoubleSpinBox()
        self.connect_timeout_spin.setRange(1.0, 300.0)
        self.connect_timeout_spin.setSuffix(" s")
        self.connect_timeout_spin.setDecimals(1)
        self.request_timeout_spin = QDoubleSpinBox()
        self.request_timeout_spin.setRange(0.5, 300.0)
        self.request_timeout_spin.setSuffix(" s")
        self.request_timeout_spin.setDecimals(1)
        self.session_timeout_spin = QSpinBox()
        self.session_timeout_spin.setRange(10, 86_400)
        self.session_timeout_spin.setSuffix(" s")
        self.channel_timeout_spin = QSpinBox()
        self.channel_timeout_spin.setRange(1, 1440)
        self.channel_timeout_spin.setSuffix(" min")

        timeout_layout.addWidget(QLabel("Connect timeout:"), 0, 0)
        timeout_layout.addWidget(self.connect_timeout_spin, 0, 1)
        timeout_layout.addWidget(QLabel("Request timeout:"), 0, 2)
        timeout_layout.addWidget(self.request_timeout_spin, 0, 3)
        timeout_layout.addWidget(QLabel("Session timeout:"), 1, 0)
        timeout_layout.addWidget(self.session_timeout_spin, 1, 1)
        timeout_layout.addWidget(QLabel("Secure channel:"), 1, 2)
        timeout_layout.addWidget(self.channel_timeout_spin, 1, 3)
        layout.addWidget(timeout_box)

        # Resilience -------------------------------------------------------
        resilience_box = QGroupBox("Resilience")
        resilience_layout = QVBoxLayout(resilience_box)
        self.auto_reconnect_check = QCheckBox("Keep reconnecting when the link drops")
        self.auto_reconnect_check.setToolTip(
            "The client keeps retrying in the background when the server or the network goes away, "
            "with a backoff of up to 30 s between attempts. Untick to release the session on loss."
        )
        self.auto_reconnect_check.toggled.connect(self._on_auto_reconnect_toggled)
        resilience_layout.addWidget(self.auto_reconnect_check)
        reconnect_hint = QLabel(
            "Retries reuse the same endpoint, security policy and identity. The other tabs stay available; "
            "requests issued while no link exists fail after the request timeout. The setting applies to the "
            "next connection."
        )
        reconnect_hint.setObjectName("hintLabel")
        reconnect_hint.setWordWrap(True)
        resilience_layout.addWidget(reconnect_hint)
        layout.addWidget(resilience_box)

        # Actions ----------------------------------------------------------
        actions = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._on_connect)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        actions.addWidget(self.connect_btn)
        actions.addWidget(self.disconnect_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_chip = QLabel("Disconnected")
        self.status_chip.setObjectName("chip")
        chip_row = QHBoxLayout()
        chip_row.addWidget(self.status_chip)
        chip_row.addStretch(1)
        layout.addLayout(chip_row)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _build_right_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        endpoints_box = QGroupBox("Advertised endpoints")
        endpoints_layout = QVBoxLayout(endpoints_box)
        hint = QLabel("Select a row to use its security policy and message security mode.")
        hint.setObjectName("hintLabel")
        endpoints_layout.addWidget(hint)

        self.endpoints_table = QTableWidget(0, 6)
        self.endpoints_table.setHorizontalHeaderLabels(
            ["Endpoint URL", "Policy", "Mode", "Level", "Identity tokens", "Certificate SHA-256"]
        )
        self.endpoints_table.verticalHeader().setVisible(False)
        self.endpoints_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.endpoints_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.endpoints_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.endpoints_table.setAlternatingRowColors(True)
        header = self.endpoints_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for index in range(1, 6):
            header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
        self.endpoints_table.itemSelectionChanged.connect(self._on_endpoint_selected)
        endpoints_layout.addWidget(self.endpoints_table, 1)
        layout.addWidget(endpoints_box, 3)

        diagnostics_box = QGroupBox("Diagnostics")
        diagnostics_layout = QVBoxLayout(diagnostics_box)
        self.diagnostics_view = QPlainTextEdit()
        self.diagnostics_view.setReadOnly(True)
        self.diagnostics_view.setFont(mono_font(9))
        self.diagnostics_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diagnostics_view.setPlaceholderText("Discover an endpoint to see the server's diagnostic data.")
        diagnostics_layout.addWidget(self.diagnostics_view, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_diagnostics)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: self._copy_text(self.diagnostics_view.toPlainText()))
        row.addWidget(refresh)
        row.addWidget(copy)
        row.addStretch(1)
        diagnostics_layout.addLayout(row)
        layout.addWidget(diagnostics_box, 2)
        return container

    # -- helpers -----------------------------------------------------------
    def _browse_file(self, target: QLineEdit, filter_text: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select file", str(Path.home()), filter_text)
        if path:
            target.setText(path)

    # -- client certificate creation ---------------------------------------
    def _on_create_certificate(self) -> None:
        """Create a self-signed certificate or a CSR locally, after a confirmation."""
        default_folder = str(config_dir() / "client-certificate")
        spec = ClientCertificateDialog.ask(
            self,
            common_name="opcua-explorer-client",
            application_uri=self._application_uri,
            output_dir=default_folder,
        )
        if spec is None:
            return
        try:
            main_file, key_file = planned_paths(spec)
        except CertificateCreationError as exc:
            self._show_error(str(exc))
            return
        text = client_certificate_confirmation(
            mode=spec.mode,
            common_name=spec.common_name,
            application_uri=spec.application_uri,
            validity_days=spec.validity_days,
            key_size=spec.key_size,
            files=[str(main_file), str(key_file)],
        )
        answer = QMessageBox.question(
            self,
            "Create client certificate",
            text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._set_certificate_result("Certificate creation was cancelled; nothing was written.")
            return

        self.create_cert_btn.setEnabled(False)
        self.cert_result_label.hide()
        # RSA key generation and two file writes are short blocking work: they run
        # on the worker loop's executor, never on the GUI thread.
        self._certificate_handle = self._runner.submit_call(
            create_certificate,
            spec,
            on_done=self._on_certificate_created,
            on_error=self._on_certificate_failed,
        )

    def _on_certificate_created(self, files: Any) -> None:
        if self._closed:
            return
        self._certificate_handle = None
        self.create_cert_btn.setEnabled(True)
        if files.certificate_path:
            self.cert_edit.setText(files.certificate_path)
            self.key_edit.setText(files.key_path)
        else:
            # A CSR is not a certificate: the key is set, the certificate field is
            # left alone until the CA issued one from the request.
            self.key_edit.setText(files.key_path)
        self._set_certificate_result(files.summary_text())
        self.error_label.hide()

    def _on_certificate_failed(self, message: str) -> None:
        if self._closed:
            return
        self._certificate_handle = None
        self.create_cert_btn.setEnabled(True)
        self._show_error(f"Creating the client certificate failed: {message}")

    def _set_certificate_result(self, text: str) -> None:
        """Show the outcome (paths and SHA-256) next to the certificate fields."""
        self.cert_result_label.setText(text)
        self.cert_result_label.show()

    def _copy_text(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)

    def _refresh_recent(self) -> None:
        """Rebuild the recent-endpoint pick list from the settings."""
        current = self.recent_combo.currentText() if hasattr(self, "recent_combo") else ""
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("recent…" if not self._settings.recent_endpoints else "choose…", "")
        for url in self._settings.recent_endpoints:
            self.recent_combo.addItem(url, url)
        self.recent_combo.setCurrentIndex(0)
        self.recent_combo.blockSignals(False)
        self.recent_combo.setEnabled(bool(self._settings.recent_endpoints))
        if current and self.recent_combo.findText(current) >= 0:
            self.recent_combo.setCurrentText(current)

    def _on_recent_activated(self, index: int) -> None:
        """Fill the endpoint field from the pick list; never connects."""
        url = self.recent_combo.itemData(index)
        if not url:
            return
        self.url_edit.setText(str(url))
        self.status_chip.setText("Endpoint selected — press Discover endpoints to connect")

    def _load_settings(self, settings: AppSettings) -> None:
        self.url_edit.setText(settings.last_endpoint)
        self.connect_timeout_spin.setValue(10.0)
        self.request_timeout_spin.setValue(10.0)
        self.session_timeout_spin.setValue(600)
        self.channel_timeout_spin.setValue(60)
        self.auto_reconnect_check.blockSignals(True)
        self.auto_reconnect_check.setChecked(bool(settings.auto_reconnect))
        self.auto_reconnect_check.blockSignals(False)

    def _on_auto_reconnect_toggled(self, checked: bool) -> None:
        self._settings.auto_reconnect = bool(checked)
        _logger.info("Automatic reconnect %s", "enabled" if checked else "disabled")
        try:
            self._settings.save()
        except OSError as exc:
            _logger.warning("Saving the auto-reconnect preference failed: %s", exc)

    def _selected_policy_uri(self) -> str:
        return str(self.policy_combo.currentData())

    def _selected_mode_name(self) -> str:
        return str(self.mode_combo.currentData())

    def _sync_security_controls(self) -> None:
        info = policy_from_uri(self._selected_policy_uri())
        secure = info is not None and info.secure
        if not secure:
            index = self.mode_combo.findData("None")
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
            self.mode_combo.setEnabled(False)
        else:
            self.mode_combo.setEnabled(True)
            if self._selected_mode_name() == "None":
                index = self.mode_combo.findData("SignAndEncrypt")
                if index >= 0:
                    self.mode_combo.setCurrentIndex(index)

        warnings = security_warnings(self._selected_policy_uri(), self._selected_mode_name())
        if warnings:
            self.security_warning.setText("  ".join(f"⚠ {warning}" for warning in warnings))
            self.security_warning.show()
        else:
            self.security_warning.hide()

        needs_certificate = secure or self.certificate_radio.isChecked()
        for widget in (self.cert_edit, self.key_edit, self.key_password_edit):
            widget.setEnabled(needs_certificate)

    def _sync_identity_page(self) -> None:
        if self.anonymous_radio.isChecked():
            self.identity_stack.setCurrentIndex(0)
        elif self.username_radio.isChecked():
            self.identity_stack.setCurrentIndex(1)
        else:
            self.identity_stack.setCurrentIndex(2)
        self._sync_security_controls()
        self._update_auth_warning()

    def _update_auth_warning(self) -> None:
        if self.username_radio.isChecked() and not is_secure(self._selected_policy_uri(), self._selected_mode_name()):
            self.security_warning.setText(
                "⚠ Username/password over an unencrypted channel: the password is visible to anyone on the network."
            )
            self.security_warning.show()

    def _auth_mode(self) -> str:
        if self.username_radio.isChecked():
            return "username"
        if self.certificate_radio.isChecked():
            return "certificate"
        return "anonymous"

    def _collect_settings(self) -> ConnectionSettings:
        return ConnectionSettings(
            endpoint_url=self.url_edit.text().strip(),
            security_policy_uri=self._selected_policy_uri(),
            security_mode=self._selected_mode_name(),
            auth_mode=self._auth_mode(),
            username=self.username_edit.text().strip() or None,
            password=self.password_edit.text() or None,
            client_certificate_path=self.cert_edit.text().strip() or None,
            client_private_key_path=self.key_edit.text().strip() or None,
            private_key_password=self.key_password_edit.text() or None,
            use_certificate_identity=self.certificate_radio.isChecked(),
            connect_timeout=float(self.connect_timeout_spin.value()),
            request_timeout=float(self.request_timeout_spin.value()),
            session_timeout_ms=int(self.session_timeout_spin.value()) * 1000,
            secure_channel_timeout_ms=int(self.channel_timeout_spin.value()) * 60_000,
            application_uri=self._application_uri,
            auto_reconnect=self.auto_reconnect_check.isChecked(),
        )

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.connect_btn.setEnabled(not busy)
        self.discover_btn.setEnabled(not busy)
        self.find_servers_btn.setEnabled(not busy)
        self.disconnect_btn.setEnabled(not busy and self._service.is_connected)
        self.cancel_btn.setEnabled(busy)
        if busy:
            set_chip_state(self.status_chip, "connecting")
            self.status_chip.setText(message or "Working…")
        self.error_label.hide()

    def _set_connected_ui(self, connected: bool) -> None:
        self.disconnect_btn.setEnabled(connected)
        self.connect_btn.setEnabled(not connected)
        if connected:
            set_chip_state(self.status_chip, "connected")
            self.status_chip.setText("Connected")
        else:
            set_chip_state(self.status_chip, "disconnected")
            self.status_chip.setText("Disconnected")
            self._state_timer.stop()

    def _show_error(self, message: str) -> None:
        set_chip_state(self.status_chip, "error")
        self.status_chip.setText("Error")
        self.error_label.setText(message)
        self.error_label.show()
        self._set_busy_controls_idle()

    def _set_busy_controls_idle(self) -> None:
        self.connect_btn.setEnabled(not self._service.is_connected)
        self.discover_btn.setEnabled(True)
        self.find_servers_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(self._service.is_connected)
        self.cancel_btn.setEnabled(False)

    # -- discovery / connect ----------------------------------------------
    def _on_discover(self) -> None:
        self._start_discovery(connect_after=False)

    def _on_connect(self) -> None:
        """Check the selected client certificate locally, then discover and connect."""
        settings = self._collect_settings()
        problems = settings.validate(require_server_certificate=False)
        if problems:
            self._show_error(" ".join(problems))
            return
        if not settings.client_certificate_path:
            self._start_discovery(connect_after=True, settings=settings)
            return
        # Parsing the pair is file work, so it runs off the GUI thread like every
        # other file operation of this window.
        self._set_busy(True, "Checking the client certificate…")
        self._cert_check_handle = self._runner.submit_call(
            check_client_certificate,
            settings.client_certificate_path,
            settings.client_private_key_path,
            password=settings.private_key_password or "",
            application_uri=self._application_uri,
            on_done=self._on_client_certificate_checked,
            on_error=self._on_client_certificate_check_failed,
        )

    def _on_client_certificate_checked(self, check: Any) -> None:
        if self._closed:
            return
        self._cert_check_handle = None
        self._set_busy_controls_idle()
        self._show_certificate_note(check)
        settings = self._collect_settings()
        if not check.ok:
            answer = QMessageBox.warning(
                self,
                "Client certificate problem",
                client_certificate_warning(
                    endpoint_url=settings.endpoint_url,
                    subject=check.subject,
                    not_after=check.not_after,
                    problems=check.problems,
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                set_chip_state(self.status_chip, "disconnected")
                self.status_chip.setText("Connect cancelled")
                return
        self._start_discovery(connect_after=True, settings=settings)

    def _on_client_certificate_check_failed(self, message: str) -> None:
        if self._closed:
            return
        self._cert_check_handle = None
        self._set_busy_controls_idle()
        self._show_error(f"The client certificate could not be checked: {message}")

    def _show_certificate_note(self, check: Any) -> None:
        """Show what was parsed about the selected client certificate, next to its fields."""
        if not check.certificate_path:
            self.cert_result_label.hide()
            return
        prefix = "\u26a0 " if check.problems else ""
        self.cert_result_label.setText(prefix + check.summary_text())
        self.cert_result_label.show()

    def _start_discovery(
        self, *, connect_after: bool, settings: ConnectionSettings | None = None
    ) -> None:
        settings = settings or self._collect_settings()
        problems = settings.validate(require_server_certificate=False)
        if problems:
            self._show_error(" ".join(problems))
            return

        self._pending = settings
        self._connect_after_discovery = connect_after
        self._set_busy(True, "Discovering endpoints…")
        self._handle = self._runner.submit(
            lambda: self._service.discover(
                settings.endpoint_url,
                request_timeout=settings.request_timeout,
                connect_timeout=settings.connect_timeout,
            ),
            on_done=self._on_discovered,
            on_error=self._on_error,
        )

    def _on_discovered(self, discovery: DiscoveryResult) -> None:
        self._discovery = discovery
        self._fill_endpoints(discovery)
        self.endpoints_discovered.emit(discovery)

        if self._connect_after_discovery and self._pending is not None:
            self._continue_connect(discovery)
            return

        self._set_busy_controls_idle()
        set_chip_state(self.status_chip, "disconnected")
        self.status_chip.setText(f"{len(discovery.endpoints)} endpoint(s) discovered")
        self._render_diagnostics(discovery=discovery)

    def _continue_connect(self, discovery: DiscoveryResult) -> None:
        settings = self._pending
        if settings is None:  # pragma: no cover - defensive
            self._set_busy_controls_idle()
            return

        try:
            endpoint = select_endpoint(discovery.endpoints, settings.security_policy_uri, settings.security_mode)
        except EndpointNotAdvertisedError as exc:
            _logger.warning("Selected security configuration is not advertised")
            self._show_error(str(exc))
            return

        if settings.is_secure:
            if not endpoint.certificate_der:
                self._show_error("The server did not provide a certificate for the selected secure endpoint.")
                return
            summary = summarize_certificate(endpoint.certificate_der)
            fingerprint = summary.sha256 or ""
            if not fingerprint:
                self._show_error("The server certificate could not be fingerprinted.")
                return
            if not self._service.trust_store.is_trusted(fingerprint):
                known = self._service.trust_store.get(summary.sha256 or "")
                approved = CertificateReviewDialog.ask(
                    self,
                    endpoint,
                    summary,
                    replacing_fingerprint=known.subject if known else None,
                )
                if not approved:
                    _logger.info("Connection cancelled by the operator: server certificate not approved")
                    self._set_busy_controls_idle()
                    set_chip_state(self.status_chip, "disconnected")
                    self.status_chip.setText("Disconnected (certificate not approved)")
                    return
                self._service.trust_store.add(
                    fingerprint,
                    subject=summary.subject,
                    endpoint_url=endpoint.endpoint_url,
                    not_after=summary.not_after,
                )
                _logger.info(
                    "Approved server certificate %s… for %s", (summary.sha256 or "")[:16], endpoint.endpoint_url
                )
            settings.server_certificate_der = endpoint.certificate_der

        self._set_busy(True, f"Connecting ({endpoint.label()})…")
        self._handle = self._runner.submit(
            lambda: self._service.connect(settings),
            on_done=self._on_connected,
            on_error=self._on_error,
        )

    def _on_connected(self, info: ConnectionInfo) -> None:
        self._set_busy_controls_idle()
        self._set_connected_ui(True)
        self._link_lost_reported = False
        self._reconnecting = False
        self._connected_info = info
        # The setting that was used for this very connection decides how a loss is
        # handled; toggling the checkbox only affects the next connect.
        self._auto_reconnect_active = bool(
            self._pending.auto_reconnect if self._pending is not None else self.auto_reconnect_check.isChecked()
        )
        self.status_chip.setText(self._connected_chip_text())
        self._settings.last_endpoint = info.endpoint_url
        self._settings.remember_endpoint(info.endpoint_url)
        self._refresh_recent()
        self._render_diagnostics(info=info)

        if self.clear_password_check.isChecked():
            self.password_edit.clear()
            self.key_password_edit.clear()

        self._state_timer.start()
        self.connected.emit(info)

    def _on_disconnect(self) -> None:
        self._state_timer.stop()
        self._set_busy(True, "Disconnecting…")
        self._runner.submit(self._service.disconnect, on_done=self._on_disconnected, on_error=self._on_disconnect_error)

    def _on_disconnected(self, _result: Any) -> None:
        self._set_busy_controls_idle()
        self._set_connected_ui(False)
        self._connected_info = None
        self._reconnecting = False
        self._auto_reconnect_active = False
        self.status_chip.setText("Disconnected")
        self.diagnostics_view.appendPlainText("Disconnected by the operator.")
        self.disconnected.emit()

    def _on_disconnect_error(self, message: str) -> None:
        self._set_busy_controls_idle()
        self._set_connected_ui(False)
        self._show_error(f"Disconnect reported an error: {message}")
        self.disconnected.emit()

    def _on_cancel(self) -> None:
        if self._handle is not None:
            _logger.info("Cancelling the current connection operation")
            self._handle.cancel()
            self._handle = None

    def _on_error(self, message: str) -> None:
        _logger.warning("Connection error: %s", message)
        self._set_connected_ui(self._service.is_connected)
        self._show_error(message)

    # -- endpoints table ---------------------------------------------------
    def _fill_endpoints(self, discovery: DiscoveryResult) -> None:
        table = self.endpoints_table
        table.blockSignals(True)
        table.setRowCount(len(discovery.endpoints))
        for row, endpoint in enumerate(discovery.endpoints):
            values = [
                endpoint.endpoint_url,
                endpoint.security_policy_label,
                endpoint.security_mode,
                str(endpoint.security_level),
                ", ".join(endpoint.token_types),
                (endpoint.certificate_sha256 or "")[:32],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 5 and endpoint.certificate_sha256:
                    item.setToolTip(endpoint.certificate_sha256)
                table.setItem(row, column, item)
        table.blockSignals(False)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    def _on_endpoint_selected(self) -> None:
        row = self.endpoints_table.currentRow()
        if row < 0 or self._discovery is None or row >= len(self._discovery.endpoints):
            return
        endpoint = self._discovery.endpoints[row]
        policy_index = self.policy_combo.findData(endpoint.security_policy_uri)
        if policy_index >= 0:
            self.policy_combo.setCurrentIndex(policy_index)
        mode_index = self.mode_combo.findData(endpoint.security_mode)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        self._sync_security_controls()

    # -- diagnostics -------------------------------------------------------
    def _render_diagnostics(self, *, discovery: DiscoveryResult | None = None, info: ConnectionInfo | None = None) -> None:
        self.diagnostics_view.clear()
        self.diagnostics_view.appendPlainText(
            connection_diagnostics_text(discovery=discovery, info=info)
        )

    def _refresh_diagnostics(self) -> None:
        if self._service.is_connected:
            # Read the connection info inside the asyncio thread, never from here.
            self._runner.submit(
                self._service.connection_snapshot,
                on_done=lambda info: self._render_diagnostics(info=info),
                on_error=lambda _message: self._render_diagnostics(discovery=self._discovery),
            )
        elif self._discovery is not None:
            self._render_diagnostics(discovery=self._discovery)
        else:
            self._render_diagnostics()

    def _poll_state(self) -> None:
        if not self._service.is_connected:
            self._state_timer.stop()
            return
        if self._state_handle is not None and self._state_handle.is_active:
            return
        self._state_handle = self._runner.submit(
            self._service.connection_state,
            on_done=self._on_state,
            on_error=lambda _message: None,
        )

    def _connected_chip_text(self) -> str:
        info = self._connected_info
        if info is None:
            return "Connected"
        return f"Connected · {info.security_policy_label} / {info.security_mode}"

    def _on_state(self, state: Any) -> None:
        text = str(state)
        if text == "connected":
            if self._reconnecting:
                self._reconnecting = False
                self._set_connected_ui(True)
                self.status_chip.setText(self._connected_chip_text())
                self.error_label.hide()
                _logger.info("Connection restored automatically")
                # The session may have been re-created, so the namespace table is re-read.
                self._runner.submit(
                    self._service.refresh_namespaces,
                    on_done=self._on_namespaces_refreshed,
                    on_error=lambda message: _logger.debug(
                        "Refreshing the namespace array after a reconnect failed: %s", message
                    ),
                )
                self.connection_restored.emit()
            return
        # Not connected. Which states are transient depends on the client: with the
        # reconnect supervisor running, every intermediate state (disconnected during
        # teardown, connecting, socket_open, channel_open, reconnecting) means "still
        # trying" - only that supervisor decides when to give up (it never does).
        if self._auto_reconnect_active:
            if not self._reconnecting:
                self._reconnecting = True
                set_chip_state(self.status_chip, "connecting")
                self.status_chip.setText("Link lost · reconnecting…")
                self.error_label.setText(
                    f"The link to the server dropped (state: {text}). The client keeps retrying with a "
                    "backoff of 1 s … 30 s. Press Disconnect to stop trying."
                )
                self.error_label.show()
                _logger.warning("Connection state changed to %s; retrying in the background", text)
                self.reconnect_started.emit()
            return
        # Auto-reconnect is off: the supervisor releases the session and the loss is
        # final until the operator connects again.
        self._state_timer.stop()
        self._reconnecting = False
        self._set_connected_ui(False)
        set_chip_state(self.status_chip, "error")
        self.status_chip.setText(f"Link lost ({text})")
        self.error_label.setText(
            "The connection to the server was lost. Check the server and the network, then connect again."
        )
        self.error_label.show()
        _logger.warning("Connection state changed to %s", text)
        if not self._link_lost_reported:
            self._link_lost_reported = True
            self.connection_lost.emit()

    def _on_namespaces_refreshed(self, namespaces: Any) -> None:
        info = self._connected_info
        if info is None:
            return
        info.namespaces = namespaces
        self._render_diagnostics(info=info)

    # -- trust store -------------------------------------------------------
    def _on_manage_trust(self) -> None:
        TrustStoreDialog(self, self._service).exec()

    def _on_find_servers(self) -> None:
        """Browse a discovery endpoint's server list; fill the URL on accept."""
        dialog = ServerDirectoryDialog(
            self,
            self._runner,
            self._service,
            discovery_url=self.url_edit.text().strip(),
        )
        try:
            accepted = dialog.exec() == int(ServerDirectoryDialog.DialogCode.Accepted)
        finally:
            dialog.shutdown()
        if accepted and dialog.selected_url:
            self.url_edit.setText(dialog.selected_url)
            self.status_chip.setText("Endpoint taken from the server directory")
            if self._discovery is not None:
                self._render_diagnostics(discovery=self._discovery)

    # -- profiles ----------------------------------------------------------
    def _reload_profiles(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("(no profile)", None)
        for profile in self._settings.profiles:
            self.profile_combo.addItem(profile.name, profile.name)
        self.profile_combo.blockSignals(False)

    def _on_profile_selected(self, index: int) -> None:
        name = self.profile_combo.itemData(index)
        if not name:
            return
        profile = next((item for item in self._settings.profiles if item.name == name), None)
        if profile is not None:
            self.apply_profile(profile)

    def apply_profile(self, profile: ConnectionProfile) -> None:
        """Fill the widgets from a saved profile or a project file (no password)."""
        self.url_edit.setText(profile.endpoint_url)
        policy_index = self.policy_combo.findData(profile.security_policy_uri)
        if policy_index >= 0:
            self.policy_combo.setCurrentIndex(policy_index)
        mode_index = self.mode_combo.findData(profile.security_mode)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        self._sync_security_controls()

        self.anonymous_radio.setChecked(profile.auth_mode == "anonymous")
        self.username_radio.setChecked(profile.auth_mode == "username")
        self.certificate_radio.setChecked(profile.auth_mode == "certificate")
        self.username_edit.setText(profile.username or "")
        self.password_edit.clear()  # passwords are never stored in profiles or projects
        self.key_password_edit.clear()
        self.cert_edit.setText(profile.client_certificate_path or "")
        self.key_edit.setText(profile.client_private_key_path or "")
        self.connect_timeout_spin.setValue(profile.connect_timeout)
        self.request_timeout_spin.setValue(profile.request_timeout)
        self.session_timeout_spin.setValue(max(10, profile.session_timeout_ms // 1000))
        self.channel_timeout_spin.setValue(max(1, profile.secure_channel_timeout_ms // 60_000))
        self.auto_reconnect_check.setChecked(bool(profile.auto_reconnect))
        self._sync_identity_page()

    def connection_profile(self) -> ConnectionProfile:
        """Current editor state as a persistable profile (structurally secret-free)."""
        return self._collect_settings().to_profile("project")

    def _on_save_profile(self) -> None:
        suggested = self.url_edit.text().strip() or "New profile"
        name, accepted = QInputDialog.getText(self, "Save connection profile", "Profile name:", text=suggested)
        name = name.strip()
        if not accepted or not name:
            return
        profile = self._collect_settings().to_profile(name)
        self._settings.profiles = [item for item in self._settings.profiles if item.name != name]
        self._settings.profiles.append(profile)
        try:
            self._settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "Save profile", f"The profile could not be saved:\n{exc}")
            return
        _logger.info("Saved connection profile %r (no password, no key password)", name)
        self._reload_profiles()
        index = self.profile_combo.findData(name)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)

    def _on_delete_profile(self) -> None:
        name = self.profile_combo.currentData()
        if not name:
            return
        self._settings.profiles = [item for item in self._settings.profiles if item.name != name]
        try:
            self._settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "Delete profile", f"The profile list could not be saved:\n{exc}")
            return
        self._reload_profiles()

    # -- shutdown ----------------------------------------------------------
    def shutdown(self) -> None:
        self._closed = True
        self._state_timer.stop()
        if self._handle is not None:
            self._handle.cancel()
        if self._certificate_handle is not None:
            self._certificate_handle.cancel()
            self._certificate_handle = None
        if self._cert_check_handle is not None:
            self._cert_check_handle.cancel()
            self._cert_check_handle = None
