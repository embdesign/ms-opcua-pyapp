"""Headless GUI test for the address-space browse filters."""

from __future__ import annotations

import time

from PySide6.QtWidgets import QTreeWidgetItem

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.gui.tabs.browser_tab import ITEM_LOADED, BrowserTab
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


def _root_loaded(tab) -> bool:
    """True when the root's browse came back and its children are in the tree.

    ``load_node`` clears the tree, so an empty tree is also what a browse *in flight*
    looks like - waiting for "no rows" is never a valid assertion. The tab marks the
    root item as loaded when the result was applied.
    """
    root = tab.tree.topLevelItem(0)
    return root is not None and bool(root.data(0, ITEM_LOADED)) and not tab._loading


def _browse(tab, qapp) -> None:
    """Apply the current filter and wait until that result is in the tree."""
    assert _pump(qapp, lambda: not tab._loading, 5.0)
    tab.browse_apply_btn.click()
    assert _pump(qapp, lambda: _root_loaded(tab), 20.0), tab.status_label.text()


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
        _browse(tab, qapp)
        assert all(row.text(1) == "Variable" for row in _child_rows(tab)), [
            row.text(1) for row in _child_rows(tab)
        ]
        assert "Temperature" in _child_names(tab)
        assert "Motor" not in _child_names(tab)

        # Organizes only: the demo Line1 organizes exactly one node (its shared child),
        # so no hierarchical child may appear.
        tab.browse_class_combo.setCurrentText("All node classes")
        tab.browse_type_combo.setCurrentText("Organizes")
        tab.browse_subtypes_check.setChecked(False)
        _browse(tab, qapp)
        assert _child_names(tab) == {"SharedConfig"}, _child_names(tab)

        # Back to defaults shows the full set again.
        tab.browse_type_combo.setCurrentText("Hierarchical references")
        tab.browse_subtypes_check.setChecked(True)
        _browse(tab, qapp)
        assert "Motor" in _child_names(tab), _child_names(tab)
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_a_stale_browse_result_leaves_the_current_marker_alone(qapp, tmp_path) -> None:
    """A result for a replaced tree must not make the running browse look finished."""
    runner = AsyncRunner()
    tab = BrowserTab(runner, OpcUaService(trust_store=TrustStore(tmp_path / "trust.json")))
    try:
        root = tab._make_item("ns=2;s=Line1", "ns=2;s=Line1", "")
        tab.tree.addTopLevelItem(root)
        tab._tree_generation = 5
        tab._loading.add("ns=2;s=Line1")

        # The previous generation's result arrives late: dropped, marker untouched.
        tab._on_children(root, "ns=2;s=Line1", [], generation=4)
        assert "ns=2;s=Line1" in tab._loading
        assert root.data(0, ITEM_LOADED) is False

        # The current generation's result applies, clears the marker and names the
        # filter it was produced with.
        tab._on_children(root, "ns=2;s=Line1", [], generation=5, label="Hierarchical references, Forward")
        assert "ns=2;s=Line1" not in tab._loading
        assert root.data(0, ITEM_LOADED) is True
        assert "with Hierarchical references, Forward" in tab.status_label.text()

        # A result for an item the tree no longer holds is dropped, not raised.
        orphan = tab._make_item("ns=2;s=Gone", "ns=2;s=Gone", "")
        tab.tree.addTopLevelItem(orphan)
        tab.tree.takeTopLevelItem(1)
        tab._on_children(orphan, "ns=2;s=Gone", [], generation=tab._tree_generation)
        tab._on_children_error(orphan, "ns=2;s=Gone", "boom", generation=tab._tree_generation)
    finally:
        tab.shutdown()


def _visible_child_names(tab) -> set[str]:
    return {row.text(0).replace("● ", "").replace("○ ", "") for row in _child_rows(tab) if not row.isHidden()}


def test_the_row_filter_hides_loaded_rows_and_keeps_their_ancestors(qapp, tmp_path) -> None:
    """The filter is local: it hides existing rows, browses nothing, and says so."""
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

        tab.load_node("ns=2;s=Line1")
        assert _pump(qapp, lambda: {"Temperature", "Motor"} <= _child_names(tab)), _child_names(tab)
        loaded = _child_names(tab)

        tab.row_filter_edit.setText("temper")
        assert _visible_child_names(tab) == {"Temperature"}, _visible_child_names(tab)
        # The root is an ancestor of a match, so it must stay visible.
        assert not tab.tree.topLevelItem(0).isHidden()
        assert "Unloaded branches are not searched" in tab.status_label.text()
        # Filtering is only a view: nothing was reloaded and no browse started.
        assert _child_names(tab) == loaded
        assert not tab._loading

        tab.row_filter_edit.setText("nothing-matches-this")
        assert _visible_child_names(tab) == set()
        assert not tab.tree.topLevelItem(0).isHidden()
        assert "Showing 1 of" in tab.status_label.text()

        tab.row_filter_edit.clear()
        assert _visible_child_names(tab) == loaded
        assert "every loaded row is shown again" in tab.status_label.text()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
