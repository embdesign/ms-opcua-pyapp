"""Headless GUI workflows: connect/download, tests tab, NodeSet2 export, guarded write."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server


def test_connect_browse_and_download_through_the_tabs(qapp, tmp_path) -> None:
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()

        # The browser loads the Objects folder automatically after connecting.
        deadline = time.monotonic() + 20
        root = None
        while time.monotonic() < deadline:
            qapp.processEvents()
            root = window.browser_tab.tree.topLevelItem(0)
            if root is not None and root.childCount() > 0:
                break
            time.sleep(0.03)
        assert root is not None and root.childCount() > 0
        names = [root.child(i).text(0) for i in range(root.childCount())]
        assert "Plant" in names

        # Download through the Export tab with a small budget.
        window.export_tab.max_nodes_spin.setValue(120)
        window.export_tab.batch_size_spin.setValue(20)
        window.export_tab._on_download()
        deadline = time.monotonic() + 45
        while window.export_tab.snapshot is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)

        snapshot = window.export_tab.snapshot
        assert snapshot is not None, window.export_tab.status_label.text()
        assert 0 < len(snapshot.nodes) <= 120
        assert "max_nodes" in snapshot.limits_hit
        assert window.export_tab.summary_view.toPlainText().strip() != ""
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_tests_tab_runs_suite_and_enables_reports(qapp, tmp_path) -> None:
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()

        window.show_tab(window.tests_tab)
        tab = window.tests_tab
        assert tab.run_btn.isEnabled()
        assert tab.export_json_btn.isEnabled() is False  # nothing to export yet

        tab.variable_edit.setText("ns=2;s=Temperature")
        tab.window_spin.setValue(2.0)
        tab.latency_spin.setValue(3)
        tab._on_run()

        deadline = time.monotonic() + 60
        while tab.run_result is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)

        run = tab.run_result
        assert run is not None, tab.status_label.text()
        assert run.summary()["failed"] == 0
        assert tab.results_table.rowCount() == len(run.checks) == 11
        assert tab.export_json_btn.isEnabled() and tab.export_html_btn.isEnabled()
        assert "passed" in tab.summary_label.text()

        tab.results_table.selectRow(0)
        qapp.processEvents()
        details = tab.details_view.toPlainText()
        assert "Endpoint discovery" in details
        assert "StatusCodes" in details or "Data:" in details
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_export_tab_writes_nodeset2_for_a_downloaded_snapshot(qapp, tmp_path, monkeypatch) -> None:
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    target = Path(tmp_path) / "browser-model.xml"
    try:
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()

        tab = window.export_tab
        window.show_tab(tab)
        # Without a snapshot there is nothing to export.
        assert tab.export_nodeset_btn.isEnabled() is False
        assert tab.dependencies_check.isChecked() is True
        assert tab.max_dependencies_spin.value() > 0

        tab.subtree_radio.setChecked(True)
        tab.subtree_edit.setText("i=85")
        tab.max_nodes_spin.setValue(400)
        tab.batch_size_spin.setValue(20)
        tab._on_download()
        deadline = time.monotonic() + 45
        while tab.snapshot is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        assert tab.snapshot is not None, tab.status_label.text()
        assert tab.export_nodeset_btn.isEnabled() is True

        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *args, **kwargs: (str(target), "NodeSet2 XML (*.xml)")),
        )
        tab._on_export_nodeset()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            qapp.processEvents()
            if target.exists() and "NodeSet2 XML" in tab.summary_view.toPlainText():
                break
            time.sleep(0.03)

        assert target.is_file(), tab.status_label.text()
        text = target.read_text(encoding="utf-8")
        assert "UANodeSet" in text
        assert "urn:opcua-explorer:demo" in text
        assert "NodeSet2 XML" in tab.summary_view.toPlainText()
        assert tab.export_nodeset_btn.isEnabled() is True
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


@pytest.mark.parametrize("include_values", [False, True])
def test_nodeset_config_reflects_the_widgets(qapp, tmp_path, include_values) -> None:
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        tab = window.export_tab
        tab.include_values_check.setChecked(include_values)
        tab.dependencies_check.setChecked(False)
        tab.max_dependencies_spin.setValue(17)
        tab.schema_edit.setText("  ")

        config = tab._nodeset_config()

        assert config.include_values is include_values
        assert config.include_dependencies is False
        assert config.max_dependency_nodes == 17
        assert config.schema_path is None
    finally:
        window.shutdown()
        runner.stop()


def test_guarded_write_asks_for_confirmation_and_refuses_to_run_when_declined(qapp, tmp_path, monkeypatch) -> None:
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        assert service.is_connected, window.connection_tab.error_label.text()

        tab = window.tests_tab
        window.show_tab(tab)
        tab.root_edit.setText("i=85")
        tab.window_spin.setValue(1.0)
        tab.latency_spin.setValue(2)
        tab.write_node_edit.setText("ns=2;s=Temperature")
        tab.write_value_edit.setText("33.5")
        tab._on_add_write()
        assert tab.write_list.count() == 1
        assert tab.call_list.count() == 0

        # Writes stay off until the operator ticks the box: no dialog, no write.
        tab._on_run()
        deadline = time.monotonic() + 60
        while tab.run_result is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        first = tab.run_result
        assert first is not None
        write_check = next(check for check in first.checks if check.id == "write_value_1")
        assert write_check.status == "skipped"
        assert "disabled" in write_check.detail.lower()

        # Now enable, decline the confirmation, and verify nothing runs.
        seen: dict[str, str] = {}

        def decline(parent, title, text, *args, **kwargs):
            seen["text"] = text
            return QMessageBox.No

        monkeypatch.setattr(QMessageBox, "question", staticmethod(decline))
        tab.allow_writes_check.setChecked(True)
        tab._run = None
        tab._on_run()
        qapp.processEvents()
        assert "not confirmed" in tab.status_label.text()
        assert tab.run_result is None
        assert "ns=2;s=Temperature" in seen["text"]
        assert "33.5" in seen["text"]
        assert "physical" in seen["text"].lower()

        # Accept this time: the write check runs and is reported.
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        tab._on_run()
        deadline = time.monotonic() + 60
        while tab.run_result is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)
        second = tab.run_result
        assert second is not None
        write_check = next(check for check in second.checks if check.id == "write_value_1")
        assert write_check.status == "passed", write_check.detail
        assert "33.5" in write_check.detail or write_check.data["read_back"] == "33.5"
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
