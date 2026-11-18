"""Headless GUI history-tab tests."""

from __future__ import annotations

import time

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server


def test_history_tab_reads_raw_history_and_reports_missing_history(qapp, tmp_path) -> None:
    port = free_port()
    server = start_demo_server(port, "--history")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.history_tab
        window.show_tab(tab)

        # Let the demo's once-per-second updates land in the in-memory history.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)

        # Take the node from the address-space selection.
        window.browser_tab.load_node("ns=2;s=Temperature")
        qapp.processEvents()
        tab.use_selection_btn.click()
        assert tab.node_edit.text() == "ns=2;s=Temperature"

        tab.read_btn.click()
        deadline = time.monotonic() + 30
        while tab.values_table.rowCount() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.values_table.rowCount() > 0, tab.status_label.text()
        assert "value(s)" in tab.summary_label.text()
        assert tab.values_table.item(0, 1).text().endswith("Z")
        assert tab.values_table.item(0, 3).text() == "Good"

        # A node the demo does not historize returns no values, not an error.
        tab.node_edit.setText("ns=2;s=Pressure")
        tab.read_btn.click()
        deadline = time.monotonic() + 30
        while "Historizing = false" not in tab.status_label.text() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert "Historizing = false" in tab.status_label.text(), tab.status_label.text()
        assert tab.values_table.rowCount() == 0
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
