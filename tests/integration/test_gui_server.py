"""Headless GUI server-status tab tests."""

from __future__ import annotations

import time

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server


def _value_of(table, item_name: str) -> tuple[str, str] | None:
    for row in range(table.rowCount()):
        name_item = table.item(row, 1)
        if name_item is not None and name_item.text() == item_name:
            value_item = table.item(row, 2)
            status_item = table.item(row, 3)
            return (
                value_item.text() if value_item is not None else "",
                status_item.text() if status_item is not None else "",
            )
    return None


def test_server_tab_shows_status_limits_and_the_monitor(qapp, tmp_path) -> None:
    port = free_port()
    server = start_demo_server(port, "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.server_tab
        window.show_tab(tab)

        # Connected → the tab refreshes once automatically.
        deadline = time.monotonic() + 30
        while tab.values_table.rowCount() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.values_table.rowCount() > 0, tab.status_label.text()

        state = _value_of(tab.values_table, "State")
        assert state is not None and state[0] == "Running", state
        max_read = _value_of(tab.values_table, "MaxNodesPerRead")
        assert max_read is not None and max_read[0] == "10000", max_read
        session_count = _value_of(tab.values_table, "CurrentSessionCount")
        assert session_count is not None and session_count[1] == "not provided", session_count
        assert "Server Running" in tab.status_label.text()
        assert tab.refresh_btn.isEnabled() is True

        # A refresh reports this session's live monitor (client view).
        monitor = window.monitor_tab
        monitor.node_edit.setText("ns=2;s=Temperature")
        monitor.add_btn.click()
        monitor.publishing_spin.setValue(200)
        monitor._on_start()
        deadline = time.monotonic() + 30
        while not monitor._running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert monitor._running is True, monitor.status_label.text()

        tab._on_refresh()
        deadline = time.monotonic() + 30
        while tab.monitor_table.rowCount() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.monitor_table.rowCount() == 1, tab.status_label.text()
        assert tab.monitor_table.item(0, 0).text() == "ns=2;s=Temperature"
        assert "subscription" in tab.monitor_label.text().lower()

        monitor._on_stop()
        deadline = time.monotonic() + 20
        while monitor._running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        tab._on_refresh()
        deadline = time.monotonic() + 20
        while tab.monitor_table.rowCount() != 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.monitor_table.rowCount() == 0
        assert "no subscription" in tab.monitor_label.text().lower()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
