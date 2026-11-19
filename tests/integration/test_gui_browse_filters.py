"""Headless GUI test for the address-space browse filters."""

from __future__ import annotations

import time

from PySide6.QtWidgets import QTreeWidgetItem

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


def _child_rows(tab) -> list[QTreeWidgetItem]:
    root = tab.tree.topLevelItem(0)
    if root is None:
        return []
    return [root.child(index) for index in range(root.childCount())]


def _child_names(tab) -> set[str]:
    return {row.text(0).replace("● ", "").replace("○ ", "") for row in _child_rows(tab)}


def test_browse_filter_narrows_the_tree(qapp, tmp_path) -> None:
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

        # Default browse of Line1 shows objects and variables.
        tab.load_node("ns=2;s=Line1")
        assert _pump(qapp, lambda: {"Temperature", "Motor"} <= _child_names(tab)), _child_names(tab)
        assert tab.browse_options().is_default() is True

        # NodeClassMask = Variable keeps only variables.
        tab.browse_class_combo.setCurrentText("Variable")
        assert "Variable" in tab.browse_options().label()
        tab.browse_apply_btn.click()
        assert _pump(
            qapp,
            lambda: bool(_child_rows(tab))
            and all(row.text(1) == "Variable" for row in _child_rows(tab))
            and "Motor" not in _child_names(tab),
        ), _child_names(tab)
        assert "Temperature" in _child_names(tab)

        # Organizes only: Line1 organizes nothing, so the tree is empty.
        tab.browse_class_combo.setCurrentText("All node classes")
        tab.browse_type_combo.setCurrentText("Organizes")
        tab.browse_subtypes_check.setChecked(False)
        tab.browse_apply_btn.click()
        assert _pump(qapp, lambda: _child_rows(tab) == []), _child_rows(tab)
        assert "Browsing with Organizes" in tab.status_label.text()

        # Back to defaults shows the full set again.
        tab.browse_type_combo.setCurrentText("Hierarchical references")
        tab.browse_subtypes_check.setChecked(True)
        tab.browse_apply_btn.click()
        assert _pump(qapp, lambda: "Motor" in _child_names(tab)), _child_names(tab)
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
