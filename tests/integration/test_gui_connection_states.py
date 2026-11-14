"""Headless GUI connection-state tests: link loss, reconnect and session independence."""

from __future__ import annotations

import time

from PySide6.QtCore import QEventLoop, QTimer

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server, wait_for_port


def test_connection_loss_updates_the_ui_and_clears_the_session(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        # Exercise the manual path: no background retries, the session is released.
        window.connection_tab.auto_reconnect_check.setChecked(False)
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()
        assert window.browser_tab.load_btn.isEnabled() is True

        # Kill the server: the session must not stay "Connected" in the UI.
        stop_demo_server(server)

        deadline = time.monotonic() + 40
        while window.connection_chip.text() != "Link lost" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.05)

        assert window.connection_chip.text() == "Link lost", window.statusBar().currentMessage()
        assert service.is_connected is False

        # The queued cleanup callback disables the tabs once the dead client is gone.
        deadline = time.monotonic() + 5
        while window.monitor_tab.node_edit.isEnabled() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)

        assert window.browser_tab.load_btn.isEnabled() is False
        assert window.monitor_tab.node_edit.isEnabled() is False
        assert window.tests_tab.run_btn.isEnabled() is False
        # The Connection tab is ready to reconnect, and the loss is explained.
        assert window.connection_tab.connect_btn.isEnabled() is True
        assert "Link lost" in window.connection_tab.status_chip.text()
        assert "lost" in window.logs_tab.report_view.toPlainText().lower()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_auto_reconnect_restores_the_session_after_a_server_restart(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # keep the real settings untouched
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        assert window.connection_tab.auto_reconnect_check.isChecked() is True
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()

        # Stop the server: the session is kept and the client retries in the background.
        stop_demo_server(server)

        deadline = time.monotonic() + 40
        while window.connection_chip.text() != "Reconnecting…" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.05)
        assert window.connection_chip.text() == "Reconnecting…", window.statusBar().currentMessage()
        # Still connected as far as the service is concerned: no session teardown.
        assert service.is_connected is True
        assert window.browser_tab.load_btn.isEnabled() is True
        assert "retrying in the background" in window.logs_tab.report_view.toPlainText().lower()

        # Bring the server back on the same port; the client must recover on its own.
        server = start_demo_server(port)
        wait_for_port(port)

        deadline = time.monotonic() + 90
        while not window.connection_chip.text().startswith("Connected") and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.05)
        assert window.connection_chip.text().startswith("Connected"), window.statusBar().currentMessage()
        assert window.connection_tab._reconnecting is False
        assert "restored" in window.logs_tab.report_view.toPlainText().lower()

        # The restored session is usable for real work.
        loop = QEventLoop()
        results: list[object] = []
        runner.submit(
            lambda: service.browse_children("i=85"),
            on_done=lambda value: (results.append(value), loop.quit()),
            on_error=lambda message: (results.append(message), loop.quit()),
        )
        QTimer.singleShot(30000, loop.quit)
        loop.exec()
        assert isinstance(results[0], list) and results[0], f"browse after reconnect failed: {results[0]!r}"
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_supervisor_retry_states_are_transient_while_auto_reconnect_is_on(qapp, tmp_path, monkeypatch) -> None:
    """A real device reports retries as 'connecting'/'disconnected', not only 'reconnecting'.

    Regression test: treating any of those as a final loss released the client and
    killed the supervisor's retry loop.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    tab = window.connection_tab
    try:
        lost: list[str] = []
        restored: list[str] = []
        tab.connection_lost.connect(lambda: lost.append("lost"))
        tab.connection_restored.connect(lambda: restored.append("restored"))

        # With the supervisor enabled every intermediate state means "still trying".
        tab._auto_reconnect_active = True
        tab._set_connected_ui(True)
        for state in ("connecting", "socket_open", "channel_open", "disconnected", "reconnecting"):
            tab._on_state(state)
        assert lost == [], "no retry state may be reported as a final link loss"
        assert tab._reconnecting is True
        assert "reconnecting" in tab.status_chip.text().lower()

        tab._on_state("connected")
        assert restored == ["restored"]
        assert tab._reconnecting is False
        assert tab.status_chip.text().startswith("Connected")

        # Without the supervisor the same state is final, as before.
        tab._auto_reconnect_active = False
        tab._set_connected_ui(True)
        tab._on_state("connecting")
        assert lost == ["lost"]
        assert tab._reconnecting is False
        assert "Link lost" in tab.status_chip.text()
    finally:
        window.shutdown()
        runner.stop()


def test_two_sessions_to_the_same_server_are_independent(qapp, tmp_path, monkeypatch) -> None:
    """A second session tab (same or another server) must not share state."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    first = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, first, AppSettings())
    second_workspace = None
    try:
        url = f"opc.tcp://127.0.0.1:{port}"
        connect_window(window, port, qapp)

        # Open a second session to the same endpoint.
        second_workspace = window.add_session()
        assert window.tabs.count() == 4  # session 1, session 2, All monitors, logs
        assert second_workspace.service is not first
        second_workspace.connection_tab.url_edit.setText(url)
        second_workspace.connection_tab._on_connect()

        first_workspace = window.workspaces[0]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            qapp.processEvents()
            if (
                first_workspace.chip_state == "connected"
                and second_workspace.chip_state == "connected"
                and first.is_connected
                and second_workspace.service.is_connected
            ):
                break
            time.sleep(0.03)
        assert first.is_connected, window.connection_tab.error_label.text()
        assert second_workspace.service.is_connected, second_workspace.connection_tab.error_label.text()

        assert first_workspace.chip_state == "connected"
        assert second_workspace.chip_state == "connected"
        assert first_workspace.browser_tab is not second_workspace.browser_tab
        assert first_workspace.monitor_tab is not second_workspace.monitor_tab
        assert window.sessions_label.text().startswith("2 sessions")
        # Duplicate server names stay distinguishable in the tab bar.
        assert window.tabs.tabText(0) != window.tabs.tabText(1)

        # Disconnecting one session must not touch the other.
        second_workspace.connection_tab._on_disconnect()
        deadline = time.monotonic() + 30
        while second_workspace.service.is_connected and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert second_workspace.service.is_connected is False
        assert first.is_connected is True
        assert first_workspace.chip_state == "connected"
        assert first_workspace.browser_tab.load_btn.isEnabled() is True

        # Closing the session removes only its tab.
        window.close_session(second_workspace)
        assert len(window.workspaces) == 1
        assert window.tabs.count() == 3  # session, All monitors, Logs & reports
        assert first.is_connected is True
        second_workspace = None
    finally:
        window.shutdown()
        for session_service in window.services:
            runner.run_until_complete(session_service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
