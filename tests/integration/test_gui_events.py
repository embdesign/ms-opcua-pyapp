"""Headless GUI events tab tests: event subscription, log, details and stop."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from PySide6.QtWidgets import QInputDialog, QMessageBox

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.models import STATUS_PASSED, EventNotification
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.opcua.write_service import CallOutcome

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server


def test_events_tab_streams_events_and_stops(qapp, tmp_path) -> None:
    port = free_port()
    server = start_demo_server(port, "--event-interval", "0.3", "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()

        tab = window.events_tab
        window.show_tab(tab)
        # The Server object is offered as the default source, but nothing is
        # subscribed until the operator adds it and presses Start.
        assert tab.source_edit.text() == "i=2253"
        assert tab.start_btn.isEnabled() is False

        # The address-space panel states whether a node is an event source.
        browser = window.browser_tab
        browser.load_node("i=2253")
        deadline = time.monotonic() + 30
        attribute_names: list[str] = []
        while time.monotonic() < deadline:
            qapp.processEvents()
            attribute_names = [
                browser.attributes_table.item(row, 0).text()
                for row in range(browser.attributes_table.rowCount())
            ]
            if "EventNotifier" in attribute_names:
                break
            time.sleep(0.03)
        assert "EventNotifier" in attribute_names, browser.status_label.text()
        event_row = attribute_names.index("EventNotifier")
        assert "SubscribeToEvents" in browser.attributes_table.item(event_row, 1).text()

        # The tab can take its source from the address-space selection.
        tab.source_edit.clear()
        tab.use_selection_btn.click()
        assert tab.source_edit.text() == "i=2253"

        tab.add_source_btn.click()
        assert tab.sources_table.rowCount() == 1
        assert tab.sources_table.item(0, 1).text() == "pending"
        assert tab.start_btn.isEnabled() is True

        tab.publishing_spin.setValue(200)
        tab._on_start()

        deadline = time.monotonic() + 30
        while tab.events_table.rowCount() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.events_table.rowCount() > 0, tab.status_label.text()
        assert tab._running is True
        assert tab.stop_btn.isEnabled() is True
        assert tab.publishing_spin.isEnabled() is False
        assert tab.event_type_edit.isEnabled() is False
        assert tab.sources_table.item(0, 1).text() == "Good"

        row_count = tab.events_table.rowCount()
        assert tab.events_table.item(0, 3).text() == "BaseEventType"
        assert "Demo event" in tab.events_table.item(0, 5).text()

        # Selecting an event shows every selected field, not only the columns.
        tab.events_table.selectRow(0)
        qapp.processEvents()
        details = tab.details_view.toPlainText()
        assert '"message"' in details
        assert "Demo event" in details
        assert '"EventId"' in details
        assert "received_at_utc" in details

        # More events may have arrived; the log stays bounded and keeps counting.
        deadline = time.monotonic() + 20
        while tab.events_table.rowCount() == row_count and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.events_table.rowCount() >= row_count
        assert tab.events_table.rowCount() <= tab.max_rows_spin.value()
        assert "event(s)" in tab.counters_label.text()

        tab._on_stop()
        deadline = time.monotonic() + 20
        while tab._running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab._running is False
        assert service.is_monitoring_events is False
        assert tab.sources_table.item(0, 1).text() == "stopped"
        assert tab.start_btn.isEnabled() is True

        # The session survives stopping the subscription.
        assert "stopped" in tab.status_label.text().lower()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_events_tab_condition_actions_need_a_confirmation(qapp, tmp_path, monkeypatch) -> None:
    """Each condition method is one explicitly confirmed, single call from the selected event."""
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.events_tab
        window.show_tab(tab)

        # A condition event would normally arrive by a ConditionType subscription;
        # the notification is injected so the UI flow is exercised in isolation.
        notification = EventNotification(
            sequence=1,
            event_type="AcknowledgeableConditionType",
            event_type_id="i=2881",
            source_node="ns=2;s=Condition",
            source_name="Condition",
            severity=300,
            message="Pump stopped",
            event_id="0102ff",
            condition_node_id="ns=2;s=Condition",
        )
        reports: list[str] = []
        tab.report_ready.connect(reports.append)

        tab._on_event(notification)
        tab.events_table.selectRow(0)
        qapp.processEvents()
        assert all(button.isEnabled() for button in tab.condition_buttons.values())
        assert tab.condition_buttons["acknowledge"].isEnabled() is True

        sent: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def fake_condition_action(*args: Any, **kwargs: Any) -> CallOutcome:
            sent.append((args, kwargs))
            return CallOutcome(
                object_node_id=args[0],
                method_node_id="ns=2;s=ConditionAcknowledge",
                status=STATUS_PASSED,
                detail="called once",
                sent=True,
            )

        monkeypatch.setattr(service, "condition_action", fake_condition_action)

        # Cancelling the comment dialog sends nothing and asks no confirmation.
        monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("", False))
        tab._on_condition_action("acknowledge")
        qapp.processEvents()
        assert sent == []
        assert "cancelled" in tab.status_label.text().lower()

        # Declining the confirmation also sends nothing.
        monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("checked", True))
        monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.No)
        tab._on_condition_action("acknowledge")
        qapp.processEvents()
        assert sent == []
        assert "cancelled" in tab.status_label.text().lower()

        # Confirming submits exactly one Acknowledge call for the selected event.
        monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
        tab._on_condition_action("acknowledge")
        deadline = time.monotonic() + 20
        while not sent and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert len(sent) == 1
        args, kwargs = sent[0]
        assert args[0] == "ns=2;s=Condition"
        assert args[1] == "0102ff"
        assert args[2] == "checked"
        assert kwargs["action"] == "acknowledge"
        assert kwargs["reference"] == "events-tab acknowledge ns=2;s=Condition"

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            qapp.processEvents()
            if "sent once" in tab.status_label.text().lower():
                break
            time.sleep(0.03)
        assert "sent once" in tab.status_label.text().lower(), tab.status_label.text()
        assert tab.condition_buttons["acknowledge"].isEnabled() is True
        # The guarded action is recorded for the shared log, not only in the status line.
        assert len(reports) == 1, reports
        assert "ns=2;s=Condition" in reports[0]
        assert "0102ff" in reports[0]
        assert "not proof" in reports[0]

        # A different action goes through the same single path with its own method.
        sent.clear()
        tab._on_condition_action("confirm")
        deadline = time.monotonic() + 20
        while not sent and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert len(sent) == 1
        assert sent[0][1]["action"] == "confirm"
        assert sent[0][1]["reference"] == "events-tab confirm ns=2;s=Condition"
        # Wait for that call to be reported before the next action overwrites the line.
        deadline = time.monotonic() + 20
        while "confirm on condition" not in tab.status_label.text().lower() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert "confirm on condition" in tab.status_label.text().lower(), tab.status_label.text()

        # AddComment without text is refused locally: an empty comment is pointless.
        sent.clear()
        monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("", True))
        tab._on_condition_action("comment")
        qapp.processEvents()
        assert sent == []
        assert "cannot be empty" in tab.status_label.text().lower()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_closing_a_session_cancels_event_work(qapp, tmp_path) -> None:
    """Event work still in flight must not outlive its session tab."""
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        workspace = window.workspaces[0]
        tab = workspace.events_tab

        async def slow_start() -> None:
            await asyncio.sleep(30.0)

        tab._submit(slow_start, on_done=lambda _result: None, on_error=lambda _message: None)
        assert len(tab._tasks) == 1
        key = next(iter(tab._tasks))

        deadline = time.monotonic() + 5.0
        while not runner.is_active(key) and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert runner.is_active(key), "the event task did not start"

        window.close_session(workspace)

        assert tab._tasks == {}
        deadline = time.monotonic() + 5.0
        while runner.is_active(key) and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert not runner.is_active(key), "the event task kept running after its session closed"
    finally:
        window.shutdown()
        for session_service in window.services:
            runner.run_until_complete(session_service.shutdown, timeout=3.0)
        runner.stop()


def test_events_tab_refreshes_conditions_without_a_dialog(qapp, tmp_path, monkeypatch) -> None:
    """ConditionRefresh is one unconfirmed call; the refresh events are the evidence."""
    port = free_port()
    server = start_demo_server(port, "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.events_tab
        window.show_tab(tab)

        # Nothing to refresh before a subscription exists.
        assert tab.refresh_conditions_btn.isEnabled() is False

        tab.add_source_btn.click()
        tab._on_start()
        deadline = time.monotonic() + 30
        while not tab._running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab._running is True, tab.status_label.text()
        assert tab.refresh_conditions_btn.isEnabled() is True

        # A call that stores nothing must not open any modal dialog.
        def fail_dialog(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("ConditionRefresh must not ask for a confirmation")

        monkeypatch.setattr(QMessageBox, "question", fail_dialog)
        reports: list[str] = []
        tab.report_ready.connect(reports.append)

        tab.refresh_conditions_btn.click()
        deadline = time.monotonic() + 30
        while "ConditionRefresh on subscription" not in tab.status_label.text() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)

        status = tab.status_label.text()
        assert "ConditionRefresh on subscription" in status, status
        assert "RefreshStart and RefreshEnd were published" in status, status
        assert reports and "ConditionRefresh" in reports[0], reports

        # The refresh events are visible in the log as the server's own evidence.
        deadline = time.monotonic() + 20
        seen: list[str] = []
        while time.monotonic() < deadline:
            qapp.processEvents()
            seen = [
                tab.events_table.item(row, 3).text()
                for row in range(tab.events_table.rowCount())
                if tab.events_table.item(row, 3) is not None
            ]
            if "RefreshEndEventType" in seen:
                break
            time.sleep(0.03)
        assert "RefreshStartEventType" in seen and "RefreshEndEventType" in seen, seen

        tab._on_stop()
        deadline = time.monotonic() + 20
        while tab._running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.refresh_conditions_btn.isEnabled() is False
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_events_tab_exports_the_rows_it_shows(qapp, tmp_path, monkeypatch) -> None:
    """The event export is a versioned document of the bounded table, not a second read."""
    import json

    from PySide6.QtWidgets import QFileDialog

    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        tab = window.events_tab
        window.show_tab(tab)
        assert tab.export_log_btn.isEnabled() is False

        tab._on_event(
            EventNotification(
                sequence=1,
                event_type="BaseEventType",
                source_node="i=2253",
                severity=300,
                message="pump stopped",
                received_at=datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
            )
        )
        qapp.processEvents()
        assert tab.export_log_btn.isEnabled() is True

        target = tmp_path / "events.json"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "JSON (*.json)"))
        tab.export_log_btn.click()
        deadline = time.monotonic() + 20
        while "Exported" not in tab.status_label.text() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)

        assert target.exists(), tab.status_label.text()
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["format"] == "opcua-explorer-event-log"
        assert document["format_version"] == 1
        assert document["count"] == 1
        assert document["events"][0]["message"] == "pump stopped"
        assert "Exported 1 event(s)" in tab.status_label.text()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
