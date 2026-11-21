"""Headless tests for the space-saving layout rules (notices and option rows).

An engineering tool is judged on its working area, so these tests pin down the two
rules that keep it: a page description is one line until the operator asks for
more, and a hidden option row always names the non-default settings it carries.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.gui.selection_marker import SelectionMarker
from opcua_explorer.gui.tab_notice import OptionsRow, TabNotice, notices
from opcua_explorer.gui.tabs.events_tab import EventsTab
from opcua_explorer.gui.tabs.history_tab import HistoryTab
from opcua_explorer.opcua.client_service import OpcUaService

_SUMMARY = "One HistoryRead of one variable. Read-only."
_DETAIL = (
    "HistoryRead for one variable: an optional time range, an optional aggregate and a value cap. "
    "Read-only: no HistoryUpdate, no event history and no retry. The server decides whether a node "
    "has history and whether it computes aggregates."
)


def test_tab_notice_is_one_line_until_the_operator_asks_for_more(qapp) -> None:
    notice = TabNotice(_SUMMARY, _DETAIL)

    assert notice.text() == _SUMMARY
    assert notice.verbose is False
    # The full text stays reachable without changing the claim.
    assert notice.toolTip() == notice.detail
    assert notice.detail.startswith("HistoryRead for one variable")

    notice.set_verbose(True)
    assert notice.text() == notice.detail
    assert notice.toolTip() == ""

    notice.set_verbose(False)
    assert notice.text() == _SUMMARY


def test_options_row_names_the_settings_it_is_hiding(qapp) -> None:
    row = OptionsRow("Options")
    row.add_widget(QLabel("Aggregate:"))

    assert row.widget.isHidden() is True
    assert row.toggle_text() == "Options ▸"
    assert row.toggle.isChecked() is False

    row.set_active("average, max 5000")
    assert row.toggle_text() == "Options (average, max 5000) ▸"
    assert "average" in row.toggle.toolTip()

    row.set_visible(True)
    assert row.widget.isHidden() is False
    # Expanded: the fields speak for themselves, so the marker goes away.
    assert row.toggle_text() == "Options ▾"

    row.set_visible(False)
    assert row.widget.isHidden() is True
    assert "average" in row.toggle_text()


def test_history_tab_hides_the_aggregate_and_the_value_cap_by_default(qapp, tmp_path) -> None:
    runner = AsyncRunner()
    tab = HistoryTab(runner, OpcUaService(trust_store=TrustStore(tmp_path / "trust.json")))
    try:
        # Defaults: nothing to announce, and the row stays out of the way.
        assert tab.options.widget.isHidden() is True
        assert tab.options.toggle_text() == "Options ▸"

        index = tab.aggregate_combo.findData("average")
        tab.aggregate_combo.setCurrentIndex(index)
        tab.interval_spin.setValue(1000.0)
        tab.max_spin.setValue(5000)
        qapp.processEvents()

        # A collapsed row names every non-default setting it carries.
        text = tab.options.toggle_text()
        assert "average / 1000 ms" in text
        assert "max 5000" in text
        assert text.endswith("▸")
        assert tab.options.widget.isHidden() is True

        tab.options.toggle.click()
        assert tab.options.widget.isHidden() is False
        assert tab.options.toggle_text() == "Options ▾"
    finally:
        tab.shutdown()


def test_events_tab_names_the_server_side_filter_while_it_is_hidden(qapp, tmp_path) -> None:
    runner = AsyncRunner()
    tab = EventsTab(runner, OpcUaService(trust_store=TrustStore(tmp_path / "trust.json")))
    try:
        assert tab.options.toggle_text() == "Filter ▸"

        tab.min_severity_spin.setValue(10)
        tab.fields_edit.setText("ConditionClassId, 2:Temperature")
        qapp.processEvents()

        text = tab.options.toggle_text()
        assert "2 extra field(s)" in text
        assert "severity ≥ 10" in text
        assert tab.options.widget.isHidden() is True

        # Clearing the filter clears the marker: nothing is being hidden.
        tab.min_severity_spin.setValue(0)
        tab.fields_edit.clear()
        qapp.processEvents()
        assert tab.options.toggle_text() == "Filter ▸"
    finally:
        tab.shutdown()


def test_view_menu_switch_expands_every_page_description(qapp, tmp_path, monkeypatch) -> None:
    saved: list[bool] = []
    monkeypatch.setattr(AppSettings, "save", lambda self, path=None: saved.append(True))

    settings = AppSettings()
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, settings)
    try:
        # Collapsed by default, one notice per page.
        workspace = window.workspaces[0]
        collapsed = [
            notice
            for index in range(workspace.tabs.count())
            for notice in notices(workspace.tabs.widget(index))
        ]
        assert len(collapsed) >= 9, [notice.text() for notice in collapsed]
        assert all(notice.verbose is False for notice in collapsed)
        # Each collapsed line is a single, short statement.
        assert all(notice.text() == notice.summary for notice in collapsed)
        assert all(len(notice.summary) <= 130 for notice in collapsed), [
            notice.summary for notice in collapsed if len(notice.summary) > 130
        ]

        window.show_descriptions_action.setChecked(True)
        qapp.processEvents()

        assert settings.show_tab_descriptions is True
        assert saved, "the switch is persisted in the settings"
        expanded = [
            notice
            for index in range(workspace.tabs.count())
            for notice in notices(workspace.tabs.widget(index))
        ]
        assert all(notice.verbose is True for notice in expanded)
        assert all(notice.text() == notice.detail for notice in expanded)
        assert all(len(notice.detail) >= len(notice.summary) for notice in expanded)

        # A session opened later inherits the switch (and the shared tabs too).
        opened = window.add_session()
        assert all(
            notice.verbose
            for index in range(opened.tabs.count())
            for notice in notices(opened.tabs.widget(index))
        )
        for fixed in (window.unified_monitor_tab, window.compare_tab, window.logs_tab):
            assert all(notice.verbose for notice in notices(fixed)), fixed

        window.show_descriptions_action.setChecked(False)
        qapp.processEvents()
        assert settings.show_tab_descriptions is False
        assert all(notice.verbose is False for notice in notices(opened.tabs.widget(0)))
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        assert Path(tmp_path).exists()


def test_selection_marker_speaks_only_when_the_target_differs(qapp, tmp_path) -> None:
    """A page keeps its own target; the marker only reports a divergence."""
    runner = AsyncRunner()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    selection = {"node": "ns=2;s=Plant"}
    tab = HistoryTab(runner, service, selection_provider=lambda: selection["node"])
    try:
        marker = tab.selection_marker
        # Nothing typed yet: no news, no space used.
        assert marker.differs is False
        assert marker.isHidden() is True

        tab.node_edit.setText("ns=2;s=Plant")
        qapp.processEvents()
        assert marker.differs is False
        assert marker.isHidden() is True

        tab.node_edit.setText("ns=2;s=Temperature")
        qapp.processEvents()
        assert marker.differs is True
        assert marker.isHidden() is False
        assert "ns=2;s=Plant" in marker.text()
        assert "ns=2;s=Temperature" in marker.toolTip()

        # The browser moving on re-checks every marker in the session.
        tab.node_edit.setText("ns=2;s=Plant")
        selection["node"] = "ns=2;s=Line1"
        marker.refresh()
        assert marker.differs is True

        # Taking the selection resolves it without any dialog.
        tab._on_use_selection()
        qapp.processEvents()
        assert marker.differs is False
        assert marker.isHidden() is True

        # Without a selection, or with an empty field, there is nothing to compare.
        selection["node"] = ""
        marker.refresh()
        assert marker.isHidden() is True
    finally:
        tab.shutdown()


def test_every_writing_page_marks_its_target(qapp, tmp_path) -> None:
    """Every page that acts on a node carries the same marker, none is left out."""
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        workspace = window.workspaces[0]
        for tab in (
            workspace.history_tab,
            workspace.write_tab,
            workspace.events_tab,
            workspace.export_tab,
            workspace.tests_tab,
        ):
            markers = tab.findChildren(SelectionMarker)
            assert markers, f"{type(tab).__name__} has no selection marker"

        # The browser publishes its selection; the workspace re-checks the markers.
        workspace.browser_tab._current_node_id = "ns=2;s=Plant"
        workspace.browser_tab.selection_changed.emit("ns=2;s=Plant")
        workspace.write_tab.node_edit.setText("ns=2;s=Motor")
        qapp.processEvents()
        assert workspace.write_tab.selection_marker.differs is True
        assert workspace.write_tab.selection_marker.isHidden() is False

        # Selecting the page's own target in the browser clears the marker again.
        workspace.browser_tab._current_node_id = "ns=2;s=Motor"
        workspace.browser_tab.selection_changed.emit("ns=2;s=Motor")
        qapp.processEvents()
        assert workspace.write_tab.selection_marker.differs is False
        assert workspace.write_tab.selection_marker.isHidden() is True
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
