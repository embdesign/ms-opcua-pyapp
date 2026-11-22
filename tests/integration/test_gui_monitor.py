"""Headless GUI monitor tests: live notifications, merged view and tree markers."""

from __future__ import annotations

import time

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server


def test_monitor_tab_streams_notifications_and_stops(qapp, tmp_path) -> None:
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()

        tab = window.monitor_tab
        window.show_tab(tab)
        assert tab.start_btn.isEnabled() is False  # no nodes yet

        tab.node_edit.setText("ns=2;s=Temperature")
        tab.add_btn.click()
        assert tab.items_table.rowCount() == 1
        assert tab.items_table.item(0, 2).text() == "pending"
        assert tab.start_btn.isEnabled() is True

        tab.publishing_spin.setValue(200)
        tab.sampling_spin.setValue(100)
        tab._on_start()

        deadline = time.monotonic() + 30
        while tab.notifications_table.rowCount() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.notifications_table.rowCount() > 0, tab.status_label.text()
        assert tab._running is True
        assert tab.stop_btn.isEnabled() is True
        # Intervals stay editable while running; "Apply intervals" changes the live subscription.
        assert tab.publishing_spin.isEnabled() is True
        assert tab.apply_intervals_btn.isEnabled() is True
        assert tab.mode_combo.isEnabled() is True
        assert tab.items_table.item(0, 2).text() == "Good"

        # The per-node row is updated in place instead of appending rows.
        assert tab.items_table.rowCount() == 1
        assert tab.items_table.item(0, 3).text() != ""
        assert tab.items_table.item(0, 5).text() == "1" or int(tab.items_table.item(0, 5).text()) >= 1
        count_before = int(tab.items_table.item(0, 5).text())

        tab.notifications_table.selectRow(0)
        qapp.processEvents()
        details = tab.details_view.toPlainText()
        assert "ns=2;s=Temperature" in details
        assert '"status"' in details

        # Selecting a monitored item shows its latest payload.
        tab.items_table.selectRow(0)
        qapp.processEvents()
        assert tab.details_view.toPlainText().startswith("Latest value for ns=2;s=Temperature")

        # The same selection draws the numbers this tab already received: one series
        # for one node, on the client's receive-time axis.
        deadline = time.monotonic() + 20
        while tab.trend_chart.series.is_empty and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        series = tab.trend_chart.series
        assert series.is_empty is False, tab.trend_summary_label.text()
        assert series.time_based is True
        # Temperature publishes numbers only, so every notification became a point.
        assert 0 < len(series.points) <= int(tab.items_table.item(0, 5).text())
        assert "numeric value(s)" in tab.trend_summary_label.text()
        assert "receive time" in tab.trend_summary_label.text()

        # Pausing freezes the notification log but keeps the in-place value current.
        tab.pause_btn.setChecked(True)
        paused_rows = tab.notifications_table.rowCount()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            qapp.processEvents()
            count_item = tab.items_table.item(0, 5)
            if count_item is not None and int(count_item.text() or 0) > count_before:
                break
            time.sleep(0.03)
        assert int(tab.items_table.item(0, 5).text()) > count_before
        assert tab.notifications_table.rowCount() == paused_rows
        assert tab.items_table.rowCount() == 1
        tab.pause_btn.setChecked(False)

        # Adding a node while running subscribes it into the same subscription.
        tab.node_edit.setText("ns=2;s=Speed")
        tab.add_btn.click()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            qapp.processEvents()
            status_item = tab.items_table.item(1, 2)
            if status_item is not None and status_item.text() == "Good":
                break
            time.sleep(0.03)
        status_item = tab.items_table.item(1, 2)
        assert status_item is not None and status_item.text() == "Good", tab.status_label.text()

        tab._on_stop()
        deadline = time.monotonic() + 20
        while tab._running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab._running is False
        assert service.is_monitoring is False
        assert tab.items_table.item(0, 2).text() == "stopped"
        assert tab.start_btn.isEnabled() is True

        # The session survives stopping the subscription.
        assert "stopped" in tab.status_label.text().lower()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_monitor_trend_counts_non_numeric_values_and_clears_with_the_log(qapp, tmp_path) -> None:
    """The live trend plots numbers only; a text value is counted, and clearing drops it."""
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.monitor_tab
        window.show_tab(tab)

        tab.node_edit.setText("ns=2;s=Recipe")
        tab.add_btn.click()
        tab.publishing_spin.setValue(200)
        tab.sampling_spin.setValue(100)
        tab._on_start()

        deadline = time.monotonic() + 30
        while tab.notifications_table.rowCount() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.notifications_table.rowCount() > 0, tab.status_label.text()

        # Recipe is a String: the chart stays empty and says how many values it skipped.
        tab.items_table.selectRow(0)
        qapp.processEvents()
        assert tab.trend_chart.series.is_empty is True
        summary = tab.trend_summary_label.text()
        assert "no numeric value received yet" in summary
        assert "non-numeric value(s) not plotted" in summary

        # Clearing the log clears the chart with it: one view of the same values.
        tab.clear_log_btn.click()
        qapp.processEvents()
        assert tab.trend_chart.series.is_empty is True
        assert tab.trend_summary_label.text().startswith("Select a monitored item")
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_unified_monitor_aggregates_every_session(qapp, tmp_path, monkeypatch) -> None:
    """The "All monitors" tab merges items and notifications of all sessions."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    first = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, first, AppSettings())
    second_workspace = None
    try:
        connect_window(window, port, qapp)
        second_workspace = window.add_session()
        second_workspace.connection_tab.url_edit.setText(f"opc.tcp://127.0.0.1:{port}")
        second_workspace.connection_tab._on_connect()

        first_workspace = window.workspaces[0]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            qapp.processEvents()
            if first_workspace.chip_state == "connected" and second_workspace.chip_state == "connected":
                break
            time.sleep(0.03)
        assert first_workspace.chip_state == "connected"
        assert second_workspace.chip_state == "connected"

        unified = window.unified_monitor_tab
        for workspace in (first_workspace, second_workspace):
            assert workspace.monitor_tab.add_node("ns=2;s=Temperature")
            workspace.monitor_tab._on_start()

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            qapp.processEvents()
            if (
                first_workspace.monitor_tab._running
                and second_workspace.monitor_tab._running
                and unified.items_table.rowCount() == 2
                and unified.notifications_table.rowCount() >= 2
            ):
                break
            time.sleep(0.05)

        assert unified.items_table.rowCount() == 2, "one aggregated row per (session, node)"
        assert unified.notifications_table.rowCount() >= 2
        session_cells = [unified.items_table.item(row, 0).text() for row in range(2)]
        assert len(set(session_cells)) == 2, session_cells
        assert {unified.items_table.item(row, 1).text() for row in range(2)} == {"ns=2;s=Temperature"}
        counts = [int(unified.items_table.item(row, 5).text()) for row in range(2)]
        assert sum(counts) >= 2

        # The session filter hides the other session's rows.
        unified.filter_combo.setCurrentIndex(1)
        qapp.processEvents()
        visible = [row for row in range(2) if not unified.items_table.isRowHidden(row)]
        assert len(visible) == 1
        unified.filter_combo.setCurrentIndex(0)

        # Closing one session drops only its aggregated rows; the other keeps working.
        window.close_session(second_workspace)
        second_workspace = None
        qapp.processEvents()
        assert len(window.workspaces) == 1
        assert unified.items_table.rowCount() == 1
        assert unified.items_table.item(0, 0).text() == session_cells[0]
        assert first_workspace.monitor_tab._running is True
    finally:
        window.shutdown()
        for session_service in window.services:
            runner.run_until_complete(session_service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_address_space_marks_nodes_of_the_live_monitor(qapp, tmp_path, monkeypatch) -> None:
    """A node added to the live monitor is marked (●, bold) in the address-space tree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)

        browser = window.browser_tab
        browser.load_node("ns=2;s=Temperature")
        qapp.processEvents()
        root = browser.tree.topLevelItem(0)
        assert root is not None
        assert browser.is_monitored("ns=2;s=Temperature") is False
        assert not root.text(0).startswith("●")

        # Added but not started: pending marker ○.
        assert window.monitor_tab.add_node("ns=2;s=Temperature")
        qapp.processEvents()
        root = browser.tree.topLevelItem(0)
        assert root.text(0).startswith("○")
        assert root.font(0).bold() is True
        assert "not started" in root.toolTip(0)
        assert browser.is_monitored("ns=2;s=Temperature") is True

        # Starting the subscription switches the marker to ●.
        window.monitor_tab._on_start()
        deadline = time.monotonic() + 30
        while not window.monitor_tab.is_running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert window.monitor_tab.is_running
        qapp.processEvents()
        root = browser.tree.topLevelItem(0)
        assert root.text(0).startswith("●")
        assert "running" in root.toolTip(0)

        # Stopping leaves the node queued: back to ○.
        window.monitor_tab._on_stop()
        deadline = time.monotonic() + 30
        while window.monitor_tab.is_running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert window.monitor_tab.is_running is False
        qapp.processEvents()
        assert browser.tree.topLevelItem(0).text(0).startswith("○")

        # The marker is a session detail: another session does not share it.
        second = window.add_session()
        assert second.browser_tab.is_monitored("ns=2;s=Temperature") is False

        # Clearing the monitor list removes the mark again.
        window.monitor_tab._on_clear_items()
        qapp.processEvents()
        root = browser.tree.topLevelItem(0)
        assert not root.text(0).startswith(("●", "○"))
        assert root.font(0).bold() is False
        assert "monitor" not in root.toolTip(0).lower()
    finally:
        window.shutdown()
        for session_service in window.services:
            runner.run_until_complete(session_service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_monitor_tab_applies_filter_server_mode_and_intervals(qapp, tmp_path) -> None:
    """The tab drives DataChangeFilter, SetMonitoringMode and the interval changes."""
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.monitor_tab
        window.show_tab(tab)

        assert tab.trigger_combo.currentText() == "StatusValue"
        assert tab.deadband_combo.currentText() == "None"
        assert tab.deadband_spin.isEnabled() is False
        assert tab.apply_intervals_btn.isEnabled() is False
        assert tab.mode_combo.isEnabled() is False

        tab.node_edit.setText("ns=2;s=Temperature")
        tab.add_btn.click()
        tab.deadband_combo.setCurrentText("Absolute")
        assert tab.deadband_spin.isEnabled() is True
        tab.deadband_spin.setValue(0.01)
        tab.publishing_spin.setValue(200)
        tab.sampling_spin.setValue(100)
        tab._on_start()

        deadline = time.monotonic() + 30
        while tab.notifications_table.rowCount() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.notifications_table.rowCount() > 0, tab.status_label.text()

        # The filter is fixed at start; intervals and the server mode are live.
        assert tab.trigger_combo.isEnabled() is False
        assert tab.deadband_combo.isEnabled() is False
        assert tab.apply_intervals_btn.isEnabled() is True
        assert tab.mode_combo.isEnabled() is True
        assert tab.mode_combo.currentText() == "Reporting"

        # Disabled at the server stops delivery; the rows stay.
        tab.mode_combo.setCurrentText("Disabled")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            qapp.processEvents()
            if "mode is Disabled" in tab.status_label.text():
                break
            time.sleep(0.03)
        assert "mode is Disabled" in tab.status_label.text(), tab.status_label.text()
        rows = tab.notifications_table.rowCount()
        quiet_until = time.monotonic() + 1.6
        while time.monotonic() < quiet_until:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.notifications_table.rowCount() == rows

        # Reporting resumes delivery.
        tab.mode_combo.setCurrentText("Reporting")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            qapp.processEvents()
            if "mode is Reporting" in tab.status_label.text():
                break
            time.sleep(0.03)
        assert "mode is Reporting" in tab.status_label.text(), tab.status_label.text()
        deadline = time.monotonic() + 20
        while tab.notifications_table.rowCount() == rows and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.notifications_table.rowCount() > rows, "notifications did not resume"

        # Apply new intervals to the running subscription.
        tab.publishing_spin.setValue(300)
        tab.sampling_spin.setValue(250)
        tab.apply_intervals_btn.click()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            qapp.processEvents()
            if "Intervals applied" in tab.status_label.text() or "was refused" in tab.status_label.text():
                break
            time.sleep(0.03)
        status = tab.status_label.text()
        assert "Intervals applied" in status or "was refused" in status, status
        assert "publishing revised to" in status, status
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
