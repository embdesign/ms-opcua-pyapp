"""Server-directory dialog of the Connection tab.

Separate module for the same reason as the security dialogs: ``connection_tab``
keeps the form and the connect state machine, this dialog keeps one bounded
directory (FindServers) interaction.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...models import DiscoveredServer, ServerDiscoveryResult
from ...opcua.client_service import OpcUaService
from ..async_runner import AsyncRunner

_logger = logging.getLogger(__name__)

SERVER_ROLE = Qt.UserRole + 41


def split_uris(text: str) -> list[str]:
    """Split an operator-entered filter into application URIs."""
    separators = [",", ";", "\n", "\t"]
    parts = [text or ""]
    for separator in separators:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [part.strip() for part in parts if part.strip()]


class ServerDirectoryDialog(QDialog):
    """Read-only FindServers browser for a discovery endpoint.

    FindServers needs no session and no trust decision, so this dialog works
    while the session is disconnected. It only reads and never connects: the
    operator picks an advertised discovery URL, which the Connection tab then
    puts into the endpoint field.
    """

    def __init__(
        self,
        parent: QWidget | None,
        runner: AsyncRunner,
        service: OpcUaService,
        *,
        discovery_url: str = "",
        server_uris: str = "",
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        self._busy = False
        self._closed = False
        self.selected_url = ""

        self.setWindowTitle("Find servers (directory)")
        self.setModal(True)
        self.resize(900, 460)

        layout = QVBoxLayout(self)
        note = QLabel(
            "Ask a discovery endpoint (a local discovery server, a GDS, or any OPC UA server) which "
            "applications it knows. This is the FindServers service: it reads a list and sends nothing else. "
            "An application-URI filter narrows the answer; leave it empty to see every entry."
        )
        note.setWordWrap(True)
        note.setObjectName("hintLabel")
        layout.addWidget(note)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Discovery endpoint:"))
        self.url_edit = QLineEdit(discovery_url)
        self.url_edit.setPlaceholderText("opc.tcp://192.168.0.10:4840")
        self.url_edit.returnPressed.connect(self._on_find)
        url_row.addWidget(self.url_edit, 1)
        self.find_btn = QPushButton("Find servers")
        self.find_btn.clicked.connect(self._on_find)
        url_row.addWidget(self.find_btn)
        layout.addLayout(url_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Application URI filter:"))
        self.filter_edit = QLineEdit(server_uris)
        self.filter_edit.setPlaceholderText("optional, e.g. urn:example:  (comma-separated, prefix match)")
        self.filter_edit.returnPressed.connect(self._on_find)
        filter_row.addWidget(self.filter_edit, 1)
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Application", "Application URI", "Type", "Discovery URL"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync_controls)
        self.table.itemDoubleClicked.connect(lambda *_: self._use_selected())
        layout.addWidget(self.table, 1)

        self.summary_label = QLabel("No directory read yet.")
        layout.addWidget(self.summary_label)

        self.status_label = QLabel("Enter a discovery endpoint and press Find servers.")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("hintLabel")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.use_btn = QPushButton("Use selected URL")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.use_btn)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    # -- read --------------------------------------------------------------
    def _on_find(self) -> None:
        if self._busy:
            return
        url = self.url_edit.text().strip()
        if not url:
            self._set_status("Enter a discovery endpoint such as opc.tcp://192.168.0.10:4840.", error=True)
            return
        uris = split_uris(self.filter_edit.text())
        self._busy = True
        self._set_status(f"Asking {url} for its server list…")
        self._sync_controls()

        def done(result: ServerDiscoveryResult) -> None:
            self._busy = False
            if self._closed:
                return
            self._fill(result)
            self._sync_controls()

        def failed(message: str) -> None:
            self._busy = False
            if self._closed:
                return
            _logger.warning("FindServers at %s failed: %s", url, message)
            self._set_status(f"Directory read failed: {message}", error=True)
            self._sync_controls()

        self._runner.submit(
            lambda: self._service.discover_servers(url, server_uris=uris),
            on_done=done,
            on_error=failed,
        )

    def _fill(self, result: ServerDiscoveryResult) -> None:
        self.table.setRowCount(len(result.servers))
        for row, server in enumerate(result.servers):
            values = [
                server.label(),
                server.application_uri,
                server.application_type,
                server.discovery_urls[0] if server.discovery_urls else "(none published)",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setData(SERVER_ROLE, server)
                    item.setToolTip(self._server_tooltip(server))
                if column == 1 or column == 3:
                    item.setToolTip(text)
                self.table.setItem(row, column, item)
        self.summary_label.setText(result.summary_text())
        if not result.servers:
            self._set_status(result.summary_text())
        else:
            self._set_status(
                f"Read {len(result.servers)} application(s) in {result.duration_ms:.0f} ms. "
                "Select one and use its URL, or copy the URI for a filtered lookup."
            )
        self._sync_controls()

    @staticmethod
    def _server_tooltip(server: DiscoveredServer) -> str:
        lines = [f"Application: {server.application_name or '(unnamed)'}", f"URI: {server.application_uri}"]
        if server.product_uri:
            lines.append(f"Product URI: {server.product_uri}")
        if server.gateway_server_uri:
            lines.append(f"Gateway: {server.gateway_server_uri}")
        if server.discovery_profile_uri:
            lines.append(f"Discovery profile: {server.discovery_profile_uri}")
        for url in server.discovery_urls:
            lines.append(f"URL: {url}")
        return "\n".join(lines)

    # -- selection ---------------------------------------------------------
    def _selected_server(self) -> DiscoveredServer | None:
        items = self.table.selectedItems()
        if not items:
            return None
        server = items[0].data(SERVER_ROLE)
        return server if isinstance(server, DiscoveredServer) else None

    def _use_selected(self) -> None:
        server = self._selected_server()
        if server is None or self._busy:
            return
        if not server.discovery_urls:
            self._set_status(
                "This application published no discovery URL. Enter its endpoint manually instead.",
                error=True,
            )
            return
        self.selected_url = server.discovery_urls[0]
        _logger.info("Directory selection: %s (%s)", self.selected_url, server.application_uri)
        self.accept()

    # -- helpers -----------------------------------------------------------
    def _sync_controls(self) -> None:
        self.url_edit.setEnabled(not self._busy)
        self.filter_edit.setEnabled(not self._busy)
        self.find_btn.setEnabled(not self._busy)
        server = self._selected_server()
        self.use_btn.setEnabled(not self._busy and server is not None and bool(server.discovery_urls))

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(("⚠ " if error else "") + message)
        self.status_label.setObjectName("errorLabel" if error else "hintLabel")
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        """Mark the dialog closed so late runner results are ignored."""
        self._closed = True
