"""Headless GUI history-tab tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.gui.tabs import history_tab as history_tab_module
from opcua_explorer.models import (
    ConditionState,
    ConditionStateSnapshot,
    EventNotification,
)
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

        # Temperature is numeric, so the same result is drawn as a trend: the chart
        # plots the returned values and states what it left out.
        series = tab.trend_chart.series
        assert series.is_empty is False
        assert len(series.points) == tab.values_table.rowCount()
        assert series.time_based is True
        assert "Trend of" in tab.summary_label.text()

        # A node the demo does not historize returns no values, not an error.
        tab.node_edit.setText("ns=2;s=Pressure")
        tab.read_btn.click()
        deadline = time.monotonic() + 30
        while "Historizing = false" not in tab.status_label.text() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert "Historizing = false" in tab.status_label.text(), tab.status_label.text()
        assert tab.values_table.rowCount() == 0
        assert tab.trend_chart.series.is_empty is True

        # Clearing drops both views.
        tab.clear_btn.click()
        assert tab.values_table.rowCount() == 0
        assert tab.trend_chart.series.is_empty is True
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_history_update_previews_confirms_and_reports_the_server_refusal(
    qapp, tmp_path, monkeypatch
) -> None:
    port = free_port()
    server = start_demo_server(port, "--history", "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    answers: list[str] = []

    def confirm(_parent, _title, text):
        answers.append(text)
        return True

    monkeypatch.setattr(history_tab_module, "ask_confirmation", confirm)
    try:
        connect_window(window, port, qapp)
        tab = window.history_tab
        window.show_tab(tab)
        tab.node_edit.setText("ns=2;s=Temperature")
        tab.update_options.set_visible(True)
        tab.update_action_combo.setCurrentIndex(tab.update_action_combo.findData("insert"))
        tab.update_entries_edit.setPlainText(f"{datetime.now(UTC).isoformat()}=21.5")

        tab.history_write_btn.click()
        deadline = time.monotonic() + 30
        while not answers and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert answers, tab.status_label.text()
        assert "HistoryUpdate" in answers[0]
        assert "server's history store" in answers[0]

        deadline = time.monotonic() + 30
        while "History insert" not in tab.status_label.text() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        status = tab.status_label.text()
        assert "History insert" in status, status
        # asyncua's demo server refuses HistoryUpdate for a non-admin user; the
        # refusal is the answer, not a client defect.
        assert "Bad" in status, status
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_alarm_summary_table_renders_states_and_missing_ones(qapp) -> None:
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore())
    window = MainWindow(runner, service, AppSettings())
    try:
        tab = window.events_tab
        tab._fill_condition_states(
            ConditionStateSnapshot(
                conditions=[
                    ConditionState(
                        node_id="ns=2;s=PumpCondition",
                        condition_name="PumpCondition",
                        acked_state="Acknowledged",
                        acked_id=True,
                        confirmed_state="Unconfirmed",
                        confirmed_id=False,
                        enabled_state="Enabled",
                        enabled_id=True,
                        missing=["Retain"],
                        last_severity=700,
                    )
                ]
            )
        )

        assert tab.state_table.rowCount() == 1
        assert tab.state_table.item(0, 0).text() == "ns=2;s=PumpCondition"
        assert tab.state_table.item(0, 2).text() == "Acknowledged"
        assert tab.state_table.item(0, 5).text() == "not exposed"
        assert "Retain" in tab.state_table.item(0, 7).text()

        # Only an event with a condition identity contributes a condition node.
        plain = EventNotification(sequence=1, message="plain")
        condition = EventNotification(
            sequence=2, message="condition", condition_node_id="ns=2;s=PumpCondition"
        )
        tab._append_event_row(plain, stored=False)
        tab._append_event_row(condition, stored=False)
        tab._append_event_row(condition, stored=True)
        assert tab._condition_node_ids() == ["ns=2;s=PumpCondition"]
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
