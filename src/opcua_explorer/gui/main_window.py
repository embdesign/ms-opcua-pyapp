"""Main window: any number of OPC UA sessions plus a shared log/report view.

Each session is a :class:`~opcua_explorer.gui.session_workspace.SessionWorkspace`
with its own ten tabs (Connection, Address space, Export, Live monitor, Events,
History, Write & call, Server, Tests, Sequences) and its own ``OpcUaService``. The window shell owns the tab bar, the menus and
the status bar, and keeps one global "Logs & reports" tab shared by all
sessions (operator summaries are prefixed with the session name).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabBar,
    QTabWidget,
)

from .. import __version__
from ..config import AppSettings, TrustStore
from ..opcua.client_service import OpcUaService
from ..project import PROJECT_FILE_FILTER, PROJECT_SUFFIX, ProjectError, ProjectFile
from .async_runner import AsyncRunner
from .session_workspace import SessionWorkspace
from .tabs import CompareTab, LogsTab, UnifiedMonitorTab
from .theme import set_chip_state

_logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        runner: AsyncRunner,
        service: OpcUaService,
        settings: AppSettings,
        *,
        trust_store: TrustStore | None = None,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._settings = settings
        # New sessions reuse the same trust store, so a certificate approved in one
        # session is trusted in all of them.
        self._trust_store = trust_store if trust_store is not None else service.trust_store
        self._session_counter = 0
        self._shut_down = False

        self.setWindowTitle("OPC UA Explorer")
        self.resize(1320, 880)
        self.setMinimumSize(1100, 720)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tabs.currentChanged.connect(self._on_current_tab_changed)
        self.setCentralWidget(self.tabs)

        self.logs_tab = LogsTab()
        self.unified_monitor_tab = UnifiedMonitorTab()
        #: Read-only comparison of two sessions; it owns no client of its own.
        self.compare_tab = CompareTab(self._runner, self._settings)
        self.workspaces: list[SessionWorkspace] = []

        self._build_status_bar()
        self._build_menu()

        self.add_session(service)
        self.tabs.addTab(self.unified_monitor_tab, "All monitors")
        self.tabs.addTab(self.compare_tab, "Compare")
        self.tabs.addTab(self.logs_tab, "Logs & reports")
        for fixed in (self.unified_monitor_tab, self.compare_tab, self.logs_tab):
            self._disable_close_button(self.tabs.indexOf(fixed))
        self.compare_tab.message.connect(self._on_message)
        self.compare_tab.report.connect(self.logs_tab.append_report)

        _logger.info("OPC UA Explorer %s started", __version__)
        self.statusBar().showMessage("Ready. Enter an endpoint URL on the Connection tab.")

    # -- sessions ----------------------------------------------------------
    def add_session(self, service: OpcUaService | None = None) -> SessionWorkspace:
        """Open a new, independent session tab (same server more than once is fine)."""
        self._session_counter += 1
        session_service = service if service is not None else OpcUaService(trust_store=self._trust_store)
        workspace = SessionWorkspace(
            self._runner,
            session_service,
            self._settings,
            name=f"Session {self._session_counter}",
        )
        workspace.changed.connect(self._on_workspace_changed)
        workspace.message.connect(self._on_message)
        workspace.report.connect(self.logs_tab.append_report)
        self.workspaces.append(workspace)
        self.unified_monitor_tab.register(workspace, workspace.title, workspace.monitor_tab)
        logs_index = self.tabs.indexOf(self.logs_tab)
        anchor = self.tabs.indexOf(self.unified_monitor_tab)
        if anchor < 0:
            anchor = logs_index
        index = anchor if anchor >= 0 else self.tabs.count()
        self.tabs.insertTab(index, workspace, f"Session {self._session_counter}")
        for opened in self.workspaces:
            self.unified_monitor_tab.set_session_title(opened, self._display_title(opened))
        self._refresh_compare_sessions()
        self._update_tab_text(workspace)
        self.tabs.setCurrentIndex(index)
        self._refresh_status()
        _logger.info("Opened a new session tab")
        return workspace

    def close_session(self, workspace: SessionWorkspace) -> None:
        """Close one session: stop its UI work and release only its client."""
        if workspace not in self.workspaces:
            return
        self._discard_workspace(workspace)
        _logger.info("Closed a session tab")
        if not self.workspaces:
            self.add_session()
        else:
            self._refresh_status()

    def _discard_workspace(self, workspace: SessionWorkspace) -> None:
        """Remove one session tab and release its client (never adds a replacement)."""
        if workspace not in self.workspaces:
            return
        if workspace.service.is_connected:
            # Best effort: the client is released inside the loop; the tab is gone
            # immediately and no other session is touched.
            self._runner.submit(
                workspace.service.disconnect,
                on_error=lambda message: _logger.warning("Closing the session reported: %s", message),
            )
        workspace.shutdown()
        self.unified_monitor_tab.unregister(workspace)
        index = self.tabs.indexOf(workspace)
        self.workspaces.remove(workspace)
        if index >= 0:
            self.tabs.removeTab(index)
        self._refresh_compare_sessions()
        workspace.deleteLater()

    @property
    def active_workspace(self) -> SessionWorkspace | None:
        current = self.tabs.currentWidget()
        return current if isinstance(current, SessionWorkspace) else None

    @property
    def workspace(self) -> SessionWorkspace:
        """The session whose tab is currently visible (falls back to the first)."""
        return self.active_workspace or self.workspaces[0]

    @property
    def services(self) -> list[OpcUaService]:
        """Every session's service, for orderly shutdown."""
        return [workspace.service for workspace in self.workspaces]

    # -- compatibility accessors (first session) ---------------------------
    #: Existing single-session code and tests use these directly.
    @property
    def connection_tab(self) -> Any:
        return self.workspaces[0].connection_tab

    @property
    def browser_tab(self) -> Any:
        return self.workspaces[0].browser_tab

    @property
    def export_tab(self) -> Any:
        return self.workspaces[0].export_tab

    @property
    def monitor_tab(self) -> Any:
        return self.workspaces[0].monitor_tab

    @property
    def events_tab(self) -> Any:
        return self.workspaces[0].events_tab

    @property
    def server_tab(self) -> Any:
        return self.workspaces[0].server_tab

    @property
    def history_tab(self) -> Any:
        return self.workspaces[0].history_tab

    @property
    def write_tab(self) -> Any:
        return self.workspaces[0].write_tab

    @property
    def tests_tab(self) -> Any:
        return self.workspaces[0].tests_tab

    def show_tab(self, widget: Any) -> None:
        """Make the session page containing ``widget`` (or a fixed tab) visible."""
        if widget in (self.logs_tab, self.unified_monitor_tab, self.compare_tab):
            self.tabs.setCurrentWidget(widget)
            return
        for workspace in self.workspaces:
            index = workspace.tabs.indexOf(widget)
            if index >= 0:
                self.tabs.setCurrentWidget(workspace)
                workspace.tabs.setCurrentIndex(index)
                return

    # -- projects ----------------------------------------------------------
    def project_state(self) -> ProjectFile:
        """Everything a project file stores: the configuration of every session."""
        return ProjectFile(
            sessions=[
                workspace.project_state(name=self._display_title(workspace))
                for workspace in self.workspaces
            ]
        )

    def save_project(self, path: str | Path) -> None:
        """Write the project to ``path`` (raises ProjectError when invalid)."""
        project = self.project_state()
        project.save(path)
        self.logs_tab.append_report(f"Project saved: {path} ({len(project.sessions)} session(s)).")
        self.statusBar().showMessage(f"Project saved to {path}.", 8000)

    def load_project(self, project: ProjectFile | str | Path) -> None:
        """Replace the sessions with the project's sessions. Nothing is connected or run."""
        if not isinstance(project, ProjectFile):
            project = ProjectFile.load(project)
        for workspace in list(self.workspaces):
            self._discard_workspace(workspace)
        for session in project.sessions:
            workspace = self.add_session()
            workspace.apply_project_state(session)
            workspace.set_project_title(session.name)
        self._update_tab_text()
        self.tabs.setCurrentWidget(self.workspaces[0])
        self._refresh_status()
        self.logs_tab.append_report(
            f"Project loaded: {len(project.sessions)} session(s). Nothing is connected, subscribed or run; "
            "press Connect on a session to resume."
        )
        self.statusBar().showMessage(
            "Project loaded. Press Connect on the session that should be used next.", 12000
        )

    # -- construction ------------------------------------------------------
    def _build_status_bar(self) -> None:
        self.sessions_label = QLabel("")
        self.sessions_label.setObjectName("hintLabel")
        self.sessions_label.hide()

        self.connection_chip = QLabel("Disconnected")
        self.connection_chip.setObjectName("chip")
        set_chip_state(self.connection_chip, "disconnected")

        self.server_label = QLabel("")
        self.server_label.setObjectName("hintLabel")

        bar = self.statusBar()
        bar.addPermanentWidget(self.sessions_label)
        bar.addPermanentWidget(self.server_label)
        bar.addPermanentWidget(self.connection_chip)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_project = QAction("Open project…", self)
        open_project.setShortcut(QKeySequence.Open)
        open_project.triggered.connect(self._on_open_project)
        file_menu.addAction(open_project)

        save_project = QAction("Save project…", self)
        save_project.setShortcut(QKeySequence.Save)
        save_project.triggered.connect(self._on_save_project)
        file_menu.addAction(save_project)

        file_menu.addSeparator()
        new_session = QAction("New connection", self)
        new_session.setShortcut(QKeySequence.New)
        new_session.triggered.connect(lambda: self.add_session())
        file_menu.addAction(new_session)

        close_session = QAction("Close session", self)
        close_session.setShortcut(QKeySequence.Close)
        close_session.triggered.connect(self._close_active_session)
        file_menu.addAction(close_session)

        file_menu.addSeparator()
        save_log = QAction("Save log…", self)
        save_log.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_log.triggered.connect(self._save_log)
        file_menu.addAction(save_log)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About OPC UA Explorer", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    # -- shell status ------------------------------------------------------
    def _on_current_tab_changed(self, _index: int) -> None:
        self._refresh_status()

    def _disable_close_button(self, index: int) -> None:
        """Grey out the close button of a fixed tab (the Logs tab is not a session)."""
        button = self.tabs.tabBar().tabButton(index, QTabBar.RightSide)
        if button is not None:
            button.setEnabled(False)

    def _on_workspace_changed(self, workspace: SessionWorkspace) -> None:
        self._update_tab_text(workspace)
        for opened in self.workspaces:
            self.unified_monitor_tab.set_session_title(opened, self._display_title(opened))
        self._refresh_compare_sessions()
        self._refresh_status()

    def _refresh_compare_sessions(self) -> None:
        """Keep the Compare tab's selectors in step with the open sessions."""
        self.compare_tab.set_sessions(
            [(workspace, self._display_title(workspace)) for workspace in self.workspaces],
            descriptions={workspace: workspace.describe() for workspace in self.workspaces},
        )

    def _on_message(self, text: str, timeout: int) -> None:
        if text:
            self.statusBar().showMessage(text, timeout)
        else:
            self.statusBar().clearMessage()

    def _update_tab_text(self, workspace: SessionWorkspace | None = None) -> None:
        """Refresh every session tab text (titles must stay unique after a rename)."""
        for target in self.workspaces:
            self._apply_tab_text(target)

    def _apply_tab_text(self, workspace: SessionWorkspace) -> None:
        index = self.tabs.indexOf(workspace)
        if index < 0:
            return
        suffix = {"connecting": " — reconnecting", "error": " — link lost"}.get(workspace.chip_state, "")
        self.tabs.setTabText(index, f"{self._display_title(workspace)}{suffix}")
        self.tabs.setTabToolTip(index, workspace.describe())

    def _display_title(self, workspace: SessionWorkspace) -> str:
        """Session name, disambiguated when two sessions report the same server."""
        title = workspace.title
        if any(other is not workspace and other.title == title for other in self.workspaces):
            title = f"{title} ({self.workspaces.index(workspace) + 1})"
        return title

    def _refresh_status(self) -> None:
        workspace = self.active_workspace or (self.workspaces[0] if self.workspaces else None)
        if workspace is None:
            set_chip_state(self.connection_chip, "disconnected")
            self.connection_chip.setText("Disconnected")
            self.server_label.setText("")
        else:
            set_chip_state(self.connection_chip, workspace.chip_state)
            self.connection_chip.setText(workspace.chip_text)
            self.server_label.setText(workspace.server_text)

        if len(self.workspaces) > 1:
            not_connected = sum(1 for ws in self.workspaces if ws.chip_state in ("connecting", "error"))
            text = f"{len(self.workspaces)} sessions"
            if not_connected:
                text += f" · {not_connected} not connected"
            self.sessions_label.setText(text)
            self.sessions_label.show()
        else:
            self.sessions_label.hide()

    # -- menu actions ------------------------------------------------------
    def _on_save_project(self) -> None:
        suggested = self._settings.last_project_path or str(
            Path.home() / f"opcua-explorer{PROJECT_SUFFIX}"
        )
        path, _selected = QFileDialog.getSaveFileName(
            self, "Save project", suggested, PROJECT_FILE_FILTER
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += PROJECT_SUFFIX
        try:
            self.save_project(path)
        except (ProjectError, OSError) as exc:
            QMessageBox.warning(self, "Save project", str(exc))
            return
        self._remember_project_path(path)

    def _on_open_project(self) -> None:
        start = self._settings.last_project_path or str(Path.home())
        path, _selected = QFileDialog.getOpenFileName(
            self, "Open project", start, PROJECT_FILE_FILTER
        )
        if not path:
            return
        try:
            project = ProjectFile.load(path)
        except ProjectError as exc:
            QMessageBox.warning(self, "Open project", str(exc))
            return
        if any(workspace.service.is_connected for workspace in self.workspaces):
            answer = QMessageBox.question(
                self,
                "Open project",
                "Replace the current session(s)? Connected clients will be disconnected and unsaved "
                "downloads or monitor logs will be lost.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.load_project(project)
        self._remember_project_path(path)

    def _remember_project_path(self, path: str) -> None:
        self._settings.last_project_path = str(path)
        try:
            self._settings.save()
        except OSError as exc:
            _logger.warning("Could not remember the project path: %s", exc)

    def _close_active_session(self) -> None:
        workspace = self.active_workspace
        if workspace is not None:
            self.close_session(workspace)

    def _on_tab_close_requested(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, SessionWorkspace):
            self.close_session(widget)

    def _save_log(self) -> None:
        self.tabs.setCurrentWidget(self.logs_tab)
        self.logs_tab.save_log()

    def _show_about(self) -> None:
        from importlib import metadata

        from PySide6 import __version__ as pyside_version

        try:
            asyncua_version = metadata.version("asyncua")
        except metadata.PackageNotFoundError:  # pragma: no cover - dependency is required
            asyncua_version = "unknown"

        QMessageBox.about(
            self,
            "About OPC UA Explorer",
            (
                f"<b>OPC UA Explorer {__version__}</b><br><br>"
                "Address-space browser, exporter and functional interoperability client.<br><br>"
                f"asyncua {asyncua_version} · PySide6 {pyside_version}<br><br>"
                "<b>Scope:</b> connection and security handling, on-demand browsing, bounded address-space "
                "download, JSON/CSV/NodeSet2 export, live data-change monitoring, functional checks "
                "(read-only by default) and guarded single-target write/method calls.<br><br>"
                "These checks are functional and interoperability checks; they are not OPC UA certification."
            ),
        )

    # -- shutdown ----------------------------------------------------------
    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        for workspace in self.workspaces:
            workspace.shutdown()
        self.unified_monitor_tab.shutdown()
        self.compare_tab.shutdown()
        self.logs_tab.shutdown()
        try:
            self._settings.save()
        except OSError as exc:
            _logger.warning("Could not save settings on exit: %s", exc)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self.shutdown()
        super().closeEvent(event)
