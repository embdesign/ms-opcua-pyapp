"""Headless GUI test for the FindServers directory dialog and its wiring."""

from __future__ import annotations

import time

from PySide6.QtWidgets import QDialog

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.gui.tabs import connection_tab as connection_tab_module
from opcua_explorer.gui.tabs.server_directory_dialog import ServerDirectoryDialog
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import free_port, start_demo_server, stop_demo_server, wait_for_port


def _pump(qapp, condition, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.03)
    return condition()


def test_directory_dialog_lists_servers_and_fills_the_endpoint(qapp, tmp_path, monkeypatch) -> None:
    port = free_port()
    server = start_demo_server(port, "--no-updates")
    endpoint = f"opc.tcp://127.0.0.1:{port}"
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    dialog = None
    try:
        window.show()
        tab = window.connection_tab
        assert tab.find_servers_btn.isEnabled() is True  # works without a session

        wait_for_port(port)
        dialog = ServerDirectoryDialog(tab, runner, service, discovery_url=endpoint)
        dialog.show()
        assert dialog.use_btn.isEnabled() is False
        dialog.find_btn.click()

        assert _pump(qapp, lambda: dialog.table.rowCount() > 0), dialog.status_label.text()
        assert dialog.table.item(0, 1).text() == "urn:opcua-explorer:demo:server"
        assert dialog.table.item(0, 0).text() == "OPC UA Explorer Demo Server"
        assert dialog.table.item(0, 3).text() == endpoint
        assert "server(s) reported" in dialog.summary_label.text()
        assert dialog.status_label.text().startswith("Read 1 application(s)")

        dialog.table.selectRow(0)
        qapp.processEvents()
        assert dialog.use_btn.isEnabled() is True
        dialog.use_btn.click()
        assert dialog.selected_url == endpoint
        assert dialog.result() == int(QDialog.DialogCode.Accepted)

        dialog.shutdown()
        dialog.deleteLater()
        dialog = None

        # The Connection tab takes the selected URL into the endpoint field.
        class _StubDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, *args, **kwargs) -> None:
                self.selected_url = endpoint

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

            def shutdown(self) -> None:
                return None

        monkeypatch.setattr(connection_tab_module, "ServerDirectoryDialog", _StubDialog)
        tab.url_edit.setText("")
        tab.find_servers_btn.click()
        qapp.processEvents()
        assert tab.url_edit.text() == endpoint
        assert tab.status_chip.text() == "Endpoint taken from the server directory"
    finally:
        if dialog is not None:
            dialog.shutdown()
            dialog.deleteLater()
        window.shutdown()
        runner.stop()
        stop_demo_server(server)
