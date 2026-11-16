"""GUI smoke tests: asyncio bridge, window shell and session wiring."""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QEventLoop, QTimer

from opcua_explorer.config import NONE_POLICY_URI, AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.opcua.security import all_policies

BASIC256SHA256 = "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"


def test_async_runner_delivers_result(qapp) -> None:
    runner = AsyncRunner()
    runner.start()
    results: list[object] = []

    async def work() -> int:
        await asyncio.sleep(0.01)
        return 42

    loop = QEventLoop()
    runner.submit(work, on_done=lambda value: (results.append(value), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    runner.stop()

    assert results == [42]


def test_async_runner_reports_cancellation(qapp) -> None:
    runner = AsyncRunner()
    runner.start()
    events: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(10)

    loop = QEventLoop()
    handle = runner.submit(
        slow,
        key="slow-task",
        on_done=lambda _value: (events.append("done"), loop.quit()),
        on_error=lambda message: (events.append(f"error:{message}"), loop.quit()),
    )
    runner.cancelled.connect(lambda key: (events.append("cancelled"), loop.quit()))
    QTimer.singleShot(100, handle.cancel)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    runner.stop()

    assert events[:1] == ["cancelled"]


def test_main_window_structure_and_security_controls(qapp, tmp_path) -> None:
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        # One session tab (with its own six tabs), the merged monitor tab, the compare tab, the logs tab.
        assert window.tabs.count() == 4
        assert window.tabs.tabText(0).startswith("Session 1")
        assert window.tabs.tabText(1) == "All monitors"
        assert window.tabs.tabText(2) == "Compare"
        assert window.tabs.tabText(3) == "Logs & reports"
        # "All sessions" + the one open session.
        assert window.unified_monitor_tab.filter_combo.count() == 2
        workspace = window.workspaces[0]
        assert [workspace.tabs.tabText(i) for i in range(workspace.tabs.count())] == [
            "Connection",
            "Address space",
            "Export",
            "Live monitor",
            "Tests",
            "Sequences",
        ]

        tab = window.connection_tab
        assert tab.policy_combo.currentData() == NONE_POLICY_URI
        assert tab.mode_combo.isEnabled() is False  # policy None forces mode None
        assert tab.mode_combo.currentData() == "None"
        assert tab.security_warning.isVisible() is True or tab.security_warning.text() != ""

        secure_index = tab.policy_combo.findData(BASIC256SHA256)
        assert secure_index >= 0
        tab.policy_combo.setCurrentIndex(secure_index)
        assert tab.mode_combo.isEnabled() is True
        assert tab.mode_combo.currentData() == "SignAndEncrypt"
        assert tab.cert_edit.isEnabled() is True

        # Every known policy must map to a URI the service understands.
        assert {info.uri for info in all_policies()} >= {tab.policy_combo.itemData(i) for i in range(tab.policy_combo.count())}

        # The Tests tab must state that the checks are read-only and not a certification.
        from PySide6.QtWidgets import QLabel

        test_texts = [label.text().lower() for label in window.tests_tab.findChildren(QLabel)]
        assert any("read-only" in text for text in test_texts)
        assert any("not official opc ua certification" in text for text in test_texts)
        assert window.tests_tab.run_btn.isEnabled() is False  # not connected

        # The monitor tab starts empty and cannot subscribe without a session.
        assert window.monitor_tab.start_btn.isEnabled() is False
        assert window.monitor_tab.node_edit.isEnabled() is False

        # Merged duplicate references are explained in the tree tooltip.
        from opcua_explorer.models import ChildEntry

        def child(**overrides):
            values = {
                "node_id": "ns=3;100100",
                "browse_name": "Output2",
                "browse_name_namespace_index": 3,
                "display_name": "Output2",
                "node_class": "Object",
                "reference_type": "i=47",
                "reference_type_name": "HasComponent",
            }
            values.update(overrides)
            return ChildEntry(**values)

        assert window.browser_tab._child_tooltip(child()) == "Output2\nReference: HasComponent"
        merged = child(reference_type_name="Organizes", additional_reference_types=["HasComponent"])
        tooltip = window.browser_tab._child_tooltip(merged)
        assert "Reference: Organizes, HasComponent" in tooltip
        assert "merged" in tooltip
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=3.0)
        runner.stop()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_browser_selection_is_local_to_each_session(qapp, tmp_path) -> None:
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        first = window.workspaces[0]
        second = window.add_session()
        first.browser_tab._current_node_id = "ns=2;s=First"
        second.browser_tab._current_node_id = "ns=2;s=Second"

        second.tests_tab._on_use_selection()
        assert second.tests_tab.variable_edit.text() == "ns=2;s=Second"

        second.export_tab._on_use_selection()
        assert second.export_tab.subtree_edit.text() == "ns=2;s=Second"

        first.tests_tab._on_use_selection()
        assert first.tests_tab.variable_edit.text() == "ns=2;s=First"
    finally:
        window.shutdown()
        for session_service in window.services:
            runner.run_until_complete(session_service.shutdown, timeout=3.0)
        runner.stop()
        window.close()
        window.deleteLater()
        qapp.processEvents()
