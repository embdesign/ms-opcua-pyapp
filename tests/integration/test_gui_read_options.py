"""Headless GUI test for the address-space value read options."""

from __future__ import annotations

import time

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server


def _pump(qapp, condition, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.03)
    return condition()


def test_value_panel_reads_with_index_range_max_age_and_timestamps(qapp, tmp_path) -> None:
    port = free_port()
    server = start_demo_server(port, "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.browser_tab
        window.show_tab(tab)
        tab.load_node("ns=2;s=Samples")
        assert _pump(qapp, lambda: tab._current_node_id == "ns=2;s=Samples")
        # Let the tree expansion and the details read settle before reading a value.
        _pump(qapp, lambda: False, seconds=0.5)

        # Defaults: no slice, MaxAge 0, both timestamps.
        assert tab.index_range_edit.text() == ""
        assert tab.max_age_spin.value() == 0
        assert tab.timestamps_combo.currentText() == "Both"

        tab.index_range_edit.setText("1:3")
        tab.max_age_spin.setValue(1000)
        tab.timestamps_combo.setCurrentText("Source")
        tab.read_value_btn.click()

        assert _pump(qapp, lambda: tab.value_meta.text().startswith("Status:")), tab.value_meta.text()
        assert "IndexRange 1:3" in tab.value_meta.text()
        assert "MaxAge 1000 ms" in tab.value_meta.text()
        assert "timestamps Source" in tab.value_meta.text()
        assert "1.0" in tab.value_view.toPlainText()
        # The demo server ignores IndexRange, so the client says what it asked for.
        assert "the server decides" in tab.value_warning.text()
        assert tab.value_warning.isVisibleTo(tab) is True or not tab.value_warning.isHidden()

        # A syntactically invalid range fails before anything is sent.
        tab.index_range_edit.setText("1:2:3")
        tab.read_value_btn.click()
        assert _pump(qapp, lambda: "NumericRange" in tab.status_label.text()), tab.status_label.text()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
