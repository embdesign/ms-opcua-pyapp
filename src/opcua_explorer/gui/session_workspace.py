"""One OPC UA session: its own service, connection state and ten tabs.

The window shell (:class:`~opcua_explorer.gui.main_window.MainWindow`) hosts any
number of these workspaces as top-level tabs. Each workspace owns an independent
:class:`~opcua_explorer.opcua.client_service.OpcUaService`, so sessions never
share a client, namespace table, address space or live-monitor subscription -
including two sessions to the same server.

The workspace keeps the Qt wiring that used to live in ``MainWindow``: it reacts
to its own connection tab and forwards only summaries (title, chip state, status
message, report text) to the shell.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from ..config import AppSettings
from ..models import ConnectionInfo
from ..opcua.client_service import OpcUaService
from ..project import ProjectSession
from .async_runner import AsyncRunner
from .tabs import (
    BrowserTab,
    ConnectionTab,
    EventsTab,
    ExportTab,
    HistoryTab,
    MonitorTab,
    SequencesTab,
    ServerTab,
    TestsTab,
    WriteCallTab,
)

_logger = logging.getLogger(__name__)


class SessionWorkspace(QWidget):
    """Tabs and connection state of exactly one OPC UA session."""

    #: Emitted (with ``self``) when the title, chip state or server label changed.
    changed = Signal(object)
    #: A short status-bar message (text, timeout in ms).
    message = Signal(str, int)
    #: An operational summary for the global "Logs & reports" tab.
    report = Signal(str)

    def __init__(
        self,
        runner: AsyncRunner,
        service: OpcUaService,
        settings: AppSettings,
        *,
        name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._service = service
        self._settings = settings
        self._title = name
        self._chip_state = "disconnected"
        self._chip_text = "Disconnected"
        self._server_text = ""
        self._connection: ConnectionInfo | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        layout.addWidget(self.tabs)

        self.connection_tab = ConnectionTab(runner, service, settings)
        self.browser_tab = BrowserTab(runner, service)
        # "Use browser selection" must read this session's tree, never the shell's
        # first-session compatibility property.
        self.export_tab = ExportTab(
            runner, service, settings, selection_provider=self.browser_tab.current_node_id
        )
        self.monitor_tab = MonitorTab(runner, service)
        self.events_tab = EventsTab(
            runner, service, selection_provider=self.browser_tab.current_node_id
        )
        self.history_tab = HistoryTab(
            runner, service, selection_provider=self.browser_tab.current_node_id
        )
        self.write_tab = WriteCallTab(
            runner, service, selection_provider=self.browser_tab.current_node_id
        )
        self.server_tab = ServerTab(runner, service)
        self.tests_tab = TestsTab(
            runner, service, settings, selection_provider=self.browser_tab.current_node_id
        )
        self.sequences_tab = SequencesTab(runner, service, settings)

        self.tabs.addTab(self.connection_tab, "Connection")
        self.tabs.addTab(self.browser_tab, "Address space")
        self.tabs.addTab(self.export_tab, "Export")
        self.tabs.addTab(self.monitor_tab, "Live monitor")
        self.tabs.addTab(self.events_tab, "Events")
        self.tabs.addTab(self.history_tab, "History")
        # Tab labels are drawn with Qt::TextShowMnemonic: '&&' is one literal '&'.
        self.tabs.addTab(self.write_tab, "Write && call")
        self.tabs.addTab(self.server_tab, "Server")
        self.tabs.addTab(self.tests_tab, "Tests")
        self.tabs.addTab(self.sequences_tab, "Sequences")

        self._wire()

    # -- state consumed by the shell ---------------------------------------
    @property
    def service(self) -> OpcUaService:
        return self._service

    @property
    def title(self) -> str:
        return self._title

    @property
    def chip_state(self) -> str:
        """One of ``disconnected``, ``connected``, ``connecting``, ``error``."""
        return self._chip_state

    @property
    def chip_text(self) -> str:
        return self._chip_text

    @property
    def server_text(self) -> str:
        return self._server_text

    def describe(self) -> str:
        """Tooltip text: endpoint, security, identity and session name."""
        info = self._connection
        if info is None:
            return f"{self._title}: not connected"
        return (
            f"{info.endpoint_url}\n"
            f"Security: {info.security_policy_label} / {info.security_mode}\n"
            f"Identity: {info.auth_mode}\n"
            f"Server: {info.server_application_name or 'unknown'}"
        )

    # -- project state -----------------------------------------------------
    def project_state(self, name: str | None = None) -> ProjectSession:
        """Capture this session's configuration and editor state for a project file."""
        return ProjectSession(
            name=(name or self._title).strip() or self._title,
            connection=self.connection_tab.connection_profile(),
            start_node_id=self.browser_tab.start_node(),
            export=self.export_tab.export_state(),
            monitor=self.monitor_tab.monitor_state(),
            tests=self.tests_tab.tests_state(),
            sequence=self.sequences_tab.sequence_state(),
        )

    def apply_project_state(self, session: ProjectSession) -> None:
        """Restore a project session without connecting, subscribing or running anything."""
        self.connection_tab.apply_profile(session.connection)
        self.browser_tab.apply_start_node(session.start_node_id)
        self.export_tab.apply_export_state(session.export)
        self.monitor_tab.apply_monitor_state(session.monitor)
        self.tests_tab.apply_tests_state(session.tests)
        self.sequences_tab.apply_sequence_state(session.sequence)

    def set_project_title(self, title: str) -> None:
        """Use the project's own session name for the tab until a server name is known."""
        cleaned = (title or "").strip()
        if not cleaned or cleaned == self._title:
            return
        self._title = cleaned
        self.changed.emit(self)

    # -- wiring ------------------------------------------------------------
    def _wire(self) -> None:
        self.connection_tab.connected.connect(self._on_connected)
        self.connection_tab.disconnected.connect(self._on_disconnected)
        self.connection_tab.connection_lost.connect(self._on_connection_lost)
        self.connection_tab.reconnect_started.connect(self._on_reconnecting)
        self.connection_tab.connection_restored.connect(self._on_connection_restored)
        self.browser_tab.download_requested.connect(self._on_download_requested)
        self.browser_tab.monitor_requested.connect(self._on_monitor_requested)
        self.export_tab.snapshot_ready.connect(self._on_snapshot_ready)
        self.export_tab.busy_changed.connect(self._on_export_busy)
        self.export_tab.export_finished.connect(self._report)
        self.tests_tab.report_ready.connect(self._report)
        self.sequences_tab.report_ready.connect(self._report)
        self.events_tab.report_ready.connect(self._report)
        self.monitor_tab.items_changed.connect(self._on_monitored_items_changed)

    def _on_monitored_items_changed(self) -> None:
        """Keep the address-space markers in sync with this session's monitor list."""
        self.browser_tab.set_monitored_nodes(
            self.monitor_tab.monitored_node_ids(),
            active=self.monitor_tab.is_running,
        )

    def _set_state(self, state: str, chip_text: str, server_text: str | None = None) -> None:
        self._chip_state = state
        self._chip_text = chip_text
        if server_text is not None:
            self._server_text = server_text
        self.changed.emit(self)

    def _set_title(self, title: str) -> None:
        if title == self._title:
            return
        self._title = title
        self.changed.emit(self)

    def _report(self, text: Any) -> None:
        """Prefix operator-facing summaries with the session name."""
        self.report.emit(f"[{self._title}] {text}")

    @property
    def _session_tabs(self) -> tuple[Any, ...]:
        """Every page that follows the connection state, in tab order."""
        return (
            self.browser_tab,
            self.export_tab,
            self.monitor_tab,
            self.events_tab,
            self.history_tab,
            self.write_tab,
            self.server_tab,
            self.tests_tab,
            self.sequences_tab,
        )

    # -- connection state --------------------------------------------------
    def _on_connected(self, info: ConnectionInfo) -> None:
        self._connection = info
        for tab in self._session_tabs:
            tab.set_connected(True, info)
        self.export_tab.refresh_namespaces()

        server = info.server_application_name or "server"
        self._set_state(
            "connected",
            f"Connected · {info.security_policy_label} / {info.security_mode}",
            f"{server} · {info.endpoint_url}",
        )
        self._set_title(server)
        self.message.emit(
            f"{server}: session established ({info.auth_mode}, {len(info.namespaces)} namespace(s)).", 8000
        )
        self._report(
            f"Connected to {info.endpoint_url} using {info.security_policy_label}/{info.security_mode} "
            f"({info.auth_mode})."
        )
        self.tabs.setCurrentWidget(self.browser_tab)

    def _on_disconnected(self) -> None:
        self._connection = None
        self._set_tabs_connected(False)
        self._set_state("disconnected", "Disconnected", "")
        self.message.emit(f"{self._title}: session closed.", 5000)
        self._report("Session closed.")

    def _set_tabs_connected(self, connected: bool) -> None:
        for tab in self._session_tabs:
            tab.set_connected(connected)

    def _on_connection_lost(self) -> None:
        """The session disappeared without the operator pressing Disconnect."""
        self._set_tabs_connected(False)
        self._set_state("error", "Link lost", "")
        self.message.emit(f"{self._title}: the connection to the server was lost.", 15000)
        self._report(
            "Connection lost: the server closed the session or the network dropped. "
            "Reconnect on the Connection tab."
        )
        # Release the dead client inside the loop, so later calls fail with a clear
        # "not connected" error instead of a transport error, and the monitor
        # subscription is torn down. Only this session is affected.
        self._runner.submit(
            self._service.disconnect,
            on_done=lambda _result: self._set_tabs_connected(False),
            on_error=lambda message: self._report(f"Cleaning up the lost session reported: {message}"),
        )

    def _on_reconnecting(self) -> None:
        """The link dropped but the client is retrying; the tabs stay usable."""
        self._set_state("connecting", "Reconnecting…")
        self.message.emit(f"{self._title}: connection lost — reconnecting automatically…", 0)
        self._report(
            "Connection lost: the client is retrying in the background (backoff up to 30 s). "
            "Press Disconnect on the Connection tab to stop."
        )

    def _on_connection_restored(self) -> None:
        """An automatic reconnect succeeded; refresh views that depend on the session."""
        self._set_state("connected", "Connected · session restored")
        self.message.emit(f"{self._title}: the connection was restored automatically.", 10000)
        self._report("Connection restored automatically.")
        self.export_tab.refresh_namespaces()
        self.monitor_tab.resubscribe_after_reconnect()
        self.events_tab.resubscribe_after_reconnect()

    # -- intra-session navigation -----------------------------------------
    def _on_download_requested(self, node_id: str) -> None:
        self.export_tab.set_selected_node(node_id)
        self.tabs.setCurrentWidget(self.export_tab)
        self.message.emit(f"Subtree {node_id} selected for download.", 5000)

    def _on_monitor_requested(self, node_id: str) -> None:
        # Deliberately keep the current tab so the operator can queue up more
        # nodes without losing their place in the address space.
        if self.monitor_tab.add_node(node_id):
            self.message.emit(f"{node_id} added to the live monitor.", 5000)

    def _on_snapshot_ready(self, snapshot: Any) -> None:
        self.browser_tab.set_snapshot(snapshot)
        self.message.emit(
            f"Download finished: {len(snapshot.nodes)} node(s), {len(snapshot.references)} reference(s).", 8000
        )

    def _on_export_busy(self, busy: bool) -> None:
        self.message.emit(f"{self._title}: downloading address space…" if busy else "", 0)

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        """Stop UI work for this session (the service is released by the shell)."""
        self.connection_tab.shutdown()
        for tab in self._session_tabs:
            tab.shutdown()
