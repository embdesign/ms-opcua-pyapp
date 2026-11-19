"""Headless GUI test for the Write & call tab (confirmations are stubbed)."""

from __future__ import annotations

import time

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.gui.tabs import write_tab as write_tab_module
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


def _run(qapp, tab, button, condition, *, seconds: float = 30.0) -> None:
    button.click()
    assert _pump(qapp, condition, seconds), tab.status_label.text() + " | " + tab.write_status_label.text()


def test_write_and_call_tab_writes_reads_back_and_calls(qapp, tmp_path, monkeypatch) -> None:
    port = free_port()
    server = start_demo_server(port, "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    asked: list[str] = []
    answer = {"yes": True}

    def fake_confirm(parent, title, text):  # noqa: ANN001 - test double
        asked.append(title)
        return answer["yes"]

    monkeypatch.setattr(write_tab_module, "ask_confirmation", fake_confirm)
    window.show()
    try:
        connect_window(window, port, qapp)
        tab = window.write_tab
        window.show_tab(tab)

        # Read the target: type, ValueRank and current value are shown.
        tab.node_edit.setText("ns=2;s=Temperature")
        _run(qapp, tab, tab.inspect_btn, lambda: "Double" in tab.info_label.text())
        assert "ValueRank: -1" in tab.info_label.text()
        original = tab._current_value_text

        # A declined confirmation sends nothing.
        answer["yes"] = False
        tab.value_edit.setText("42.5")
        _run(qapp, tab, tab.write_btn, lambda: "cancelled" in tab.write_status_label.text())
        assert asked == ["Confirm write"]
        assert runner.run_until_complete(lambda: service.read_value("ns=2;s=Temperature")).value != 42.5

        # An accepted confirmation writes, reads back and reports the previous value.
        answer["yes"] = True
        tab.value_edit.setText("42.5")
        _run(qapp, tab, tab.write_btn, lambda: "read it back" in tab.write_status_label.text())
        assert "write: Good" in tab.result_view.text()
        assert "read back: 42.5" in tab.result_view.text()
        assert runner.run_until_complete(lambda: service.read_value("ns=2;s=Temperature")).value == 42.5

        # Restore is one additional verified write.
        tab.value_edit.setText("43.0")
        tab.restore_check.setChecked(True)
        _run(qapp, tab, tab.write_btn, lambda: "Restored" in tab.write_status_label.text())
        assert runner.run_until_complete(lambda: service.read_value("ns=2;s=Temperature")).value == 42.5
        tab.restore_check.setChecked(False)

        # Method call: signature rows, then exactly one call with the entered argument.
        tab.object_edit.setText("ns=2;s=Motor")
        tab.method_edit.setText("ns=2;s=Start")
        _run(qapp, tab, tab.signature_btn, lambda: tab.args_table.rowCount() == 1)
        assert tab.args_table.item(0, 1).text() == "Double"
        tab.args_table.item(0, 2).setText("900")
        _run(qapp, tab, tab.call_btn, lambda: "Called" in tab.call_status_label.text())
        assert "output: \"Motor started at 900\"" in tab.result_view.text()
        assert asked[-1] == "Confirm method call"

        # A refused value never reaches the confirmation dialog.
        count = len(asked)
        tab.value_edit.setText("not-a-number")
        _run(qapp, tab, tab.write_btn, lambda: "Nothing was written" in tab.write_status_label.text())
        assert len(asked) == count
        assert original is not None
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_attribute_write_group_reports_the_server_refusal(qapp, tmp_path, monkeypatch) -> None:
    port = free_port()
    server = start_demo_server(port, "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    asked: list[str] = []
    answer = {"yes": True}

    def fake_confirm(parent, title, text):  # noqa: ANN001 - test double
        asked.append(title)
        return answer["yes"]

    monkeypatch.setattr(write_tab_module, "ask_confirmation", fake_confirm)
    window.show()
    try:
        connect_window(window, port, qapp)
        tab = window.write_tab
        window.show_tab(tab)
        tab.node_edit.setText("ns=2;s=Temperature")
        _run(qapp, tab, tab.inspect_btn, lambda: "Double" in tab.info_label.text())

        # The current text can be copied into the field.
        tab.attribute_combo.setCurrentText("Description")
        tab.load_attribute_btn.click()
        assert tab.attribute_edit.text() == "Temperature"

        # Declining the confirmation sends nothing.
        answer["yes"] = False
        tab.attribute_edit.setText("Boiler outlet")
        _run(qapp, tab, tab.attribute_write_btn, lambda: "cancelled" in tab.attribute_status_label.text())
        assert asked == ["Confirm attribute write"]

        # The demo server allows non-Value writes for administrators only.
        answer["yes"] = True
        _run(qapp, tab, tab.attribute_write_btn, lambda: "BadUserAccessDenied" in tab.attribute_status_label.text())
        assert "write: BadUserAccessDenied" in tab.result_view.text()
        assert "sent: yes" in tab.result_view.text()
        assert asked[-1] == "Confirm attribute write"

        # Empty text never reaches the confirmation dialog.
        count = len(asked)
        tab.attribute_edit.setText("   ")
        tab.attribute_write_btn.click()
        assert _pump(qapp, lambda: "Enter the new attribute text" in tab.status_label.text())
        assert len(asked) == count
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_node_management_group_reports_the_server_refusal(qapp, tmp_path, monkeypatch) -> None:
    port = free_port()
    server = start_demo_server(port, "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    asked: list[str] = []
    answer = {"yes": True}

    def fake_confirm(parent, title, text):  # noqa: ANN001 - test double
        asked.append(title)
        return answer["yes"]

    monkeypatch.setattr(write_tab_module, "ask_confirmation", fake_confirm)
    window.show()
    try:
        connect_window(window, port, qapp)
        tab = window.write_tab
        window.show_tab(tab)

        # A missing parent never reaches the confirmation dialog.
        tab.parent_edit.setText("")
        tab.browse_name_edit.setText("NotAllowed")
        tab.create_node_btn.click()
        assert _pump(qapp, lambda: "Enter the parent NodeId" in tab.status_label.text())
        assert asked == []

        # Declining the confirmation sends nothing.
        answer["yes"] = False
        tab.parent_edit.setText("ns=2;s=Plant")
        _run(qapp, tab, tab.create_node_btn, lambda: "cancelled" in tab.node_status_label.text())
        assert asked == ["Confirm node creation"]

        # The demo server allows node management for administrators only.
        answer["yes"] = True
        _run(qapp, tab, tab.create_node_btn, lambda: "BadUserAccessDenied" in tab.node_status_label.text())
        assert "create: BadUserAccessDenied" in tab.result_view.text()
        assert "sent: yes" in tab.result_view.text()

        # Deleting a node is refused by the same server.
        tab.node_edit.setText("ns=2;s=Temperature")
        _run(
            qapp,
            tab,
            tab.delete_node_btn,
            lambda: "delete: BadUserAccessDenied" in tab.result_view.text(),
        )
        assert "verified gone: None" in tab.result_view.text()
        assert asked[-1] == "Confirm node deletion"
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
