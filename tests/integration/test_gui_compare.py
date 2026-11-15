"""Headless GUI tests for the read-only Compare tab.

Two levels: the pure row/headline helpers (no widgets) and the tab itself fed
with a hand-built result, then one end-to-end run with two connected sessions.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from opcua_explorer.compare import (
    KIND_CHANGED,
    KIND_EXTRA_IN_CANDIDATE,
    KIND_MISSING_IN_CANDIDATE,
    ComparisonConfig,
    compare_address_spaces,
)
from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.exporters import export_json
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.gui.tabs.compare_tab import (
    FILE_SOURCE,
    FILTER_ALL,
    CompareTab,
    comparison_headline,
    comparison_warnings_text,
    difference_payload,
    difference_row,
    filtered_differences,
    split_node_id_list,
)
from opcua_explorer.models import AddressSpace, NamespaceEntry, NodeRecord, TraversalConfig
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server

NS0 = "http://opcfoundation.org/UA/"
PLANT = "urn:example:plant"


def _node(node_id: str, name: str, **fields: Any) -> NodeRecord:
    values: dict[str, Any] = {
        "node_class": "Variable",
        "data_type": "i=11",
        "value_rank": -1,
        "array_dimensions": [],
        "access_level_names": ["CurrentRead"],
        "user_access_level_names": ["CurrentRead"],
        "type_definition": "i=63",
        "display_name": name,
    }
    values.update(fields)
    return NodeRecord(
        node_id=node_id,
        browse_name=name,
        browse_name_namespace_index=1,
        namespace_uri=PLANT,
        **values,
    )


def _snapshot(endpoint: str, nodes: list[NodeRecord]) -> AddressSpace:
    return AddressSpace(
        endpoint_url=endpoint,
        nodes={record.node_id: record for record in nodes},
        namespaces=[NamespaceEntry(0, NS0), NamespaceEntry(1, PLANT)],
    )


def _result() -> Any:
    reference = _snapshot(
        "opc.tcp://old:4840",
        [_node("ns=1;s=Temperature", "Temperature"), _node("ns=1;s=OnlyOld", "OnlyOld")],
    )
    candidate = _snapshot(
        "opc.tcp://new:4840",
        [_node("ns=1;s=Temperature", "Temperature", node_class="Object", data_type=None), _node("ns=1;s=OnlyNew", "OnlyNew")],
    )
    return compare_address_spaces(reference, candidate, ComparisonConfig())


# -- pure helpers ----------------------------------------------------------


def test_rows_describe_kind_names_and_attributes() -> None:
    result = _result()
    rows = [difference_row(item) for item in result.differences]

    assert rows[0][0] == "missing on the candidate"
    assert rows[0][1] == "OnlyOld"
    assert rows[0][3] == "ns=1;s=OnlyOld"
    assert rows[2][0] == "changed"
    assert rows[2][5] == "NodeClass, DataType"


def test_payload_contains_every_attribute_difference() -> None:
    changed = _result().differences[-1]
    payload = difference_payload(changed)

    assert '"NodeClass"' in payload
    assert '"reference": "Variable"' in payload


def test_headline_reports_counts_and_warnings() -> None:
    result = _result()
    headline = comparison_headline(result)

    assert headline.startswith("3 difference(s) found")
    assert "matched 1" in headline
    assert "missing 1" in headline
    assert "extra 1" in headline
    assert comparison_warnings_text(result) == ""


def test_filter_selects_one_bucket() -> None:
    result = _result()

    assert filtered_differences(result, FILTER_ALL) == result.differences
    missing = filtered_differences(result, KIND_MISSING_IN_CANDIDATE)
    assert [item.kind for item in missing] == [KIND_MISSING_IN_CANDIDATE]
    assert filtered_differences(result, KIND_EXTRA_IN_CANDIDATE)[0].candidate_node_id == "ns=1;s=OnlyNew"
    assert filtered_differences(result, KIND_CHANGED)[0].key == f"{PLANT}|s=Temperature"


def test_node_id_lists_accept_commas_and_newlines() -> None:
    assert split_node_id_list(" i=2258, ns=2;s=Serial\nns=2;s=Counter ") == frozenset(
        {"i=2258", "ns=2;s=Serial", "ns=2;s=Counter"}
    )
    assert split_node_id_list("  ") == frozenset()


# -- widget ----------------------------------------------------------------


def test_tab_shows_rows_and_filters_them(qapp) -> None:
    tab = CompareTab(AsyncRunner())
    try:
        tab.set_sessions([(object(), "Session 1"), (object(), "Session 2")])
        assert tab.reference_combo.count() == 3  # two sessions + the saved-document item
        assert tab.candidate_combo.currentIndex() == 1

        tab.apply_result(_result())

        assert tab.table.rowCount() == 3
        assert "3 difference(s) found" in tab.headline_label.text()
        assert tab.export_html_button.isEnabled() is True
        assert '"kind": "missing_in_candidate"' in tab.details_view.toPlainText()

        tab.table.selectRow(2)  # the changed node carries attribute details
        assert "NodeClass" in tab.details_view.toPlainText()

        tab.filter_combo.setCurrentIndex(tab.filter_combo.findData(KIND_CHANGED))
        assert tab.table.rowCount() == 1
        assert tab.table.item(0, 0).text() == "changed"

        tab.search_edit.setText("OnlyNew")
        tab.filter_combo.setCurrentIndex(tab.filter_combo.findData(FILTER_ALL))
        assert tab.table.rowCount() == 1
        assert tab.table.item(0, 1).text() == "OnlyNew"
    finally:
        tab.deleteLater()


def test_tab_reports_an_equal_result_without_rows(qapp) -> None:
    tab = CompareTab(AsyncRunner())
    try:
        nodes = [_node("ns=1;s=Temperature", "Temperature")]
        result = compare_address_spaces(
            _snapshot("opc.tcp://old:4840", nodes), _snapshot("opc.tcp://new:4840", nodes)
        )

        tab.apply_result(result)

        assert tab.table.rowCount() == 0
        assert tab.headline_label.text().startswith("No difference found")
        assert tab.warnings_label.isVisible() is False
    finally:
        tab.deleteLater()


def test_session_selectors_follow_the_open_sessions(qapp) -> None:
    tab = CompareTab(AsyncRunner())
    try:
        first, second = object(), object()
        tab.set_sessions([(first, "Session 1")])
        assert tab.reference_combo.count() == 2  # one session + the saved-document item

        tab.set_sessions([(first, "Session 1"), (second, "Session 2")])
        tab.reference_combo.setCurrentIndex(0)
        tab.candidate_combo.setCurrentIndex(1)
        tab.set_sessions([(first, "Session 1"), (second, "Session 2")])
        assert tab.reference_combo.currentData() is first
        assert tab.candidate_combo.currentData() is second

        tab._on_swap()
        assert tab.reference_combo.currentData() is second
        assert tab.candidate_combo.currentData() is first

        tab.set_sessions([])
        assert tab.compare_button.isEnabled() is False
    finally:
        tab.deleteLater()


# -- saved documents -------------------------------------------------------


def test_a_saved_document_can_be_chosen_for_one_side(qapp, tmp_path, monkeypatch) -> None:
    from opcua_explorer.gui.tabs import compare_tab as module

    document = tmp_path / "old.json"
    export_json(_snapshot("opc.tcp://saved:4840", [_node("ns=1;s=Temperature", "Temperature")]), document)
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(document), ""))

    tab = CompareTab(AsyncRunner())
    try:
        tab.set_sessions([(object(), "Session 1"), (object(), "Session 2")])
        tab.reference_combo.setCurrentIndex(tab.reference_combo.findData(FILE_SOURCE))

        workspace, path = tab._source("reference")
        assert workspace is None
        assert path == str(document)
        assert tab.reference_combo.currentText() == "Saved JSON: old.json"
        assert tab._source("candidate")[1] is None
    finally:
        tab.deleteLater()


def test_two_saved_documents_compare_without_a_connection(qapp, tmp_path) -> None:
    nodes = [_node("ns=1;s=Temperature", "Temperature")]
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    export_json(_snapshot("opc.tcp://old:4840", nodes), old)
    export_json(_snapshot("opc.tcp://new:4840", nodes), new)

    tab = CompareTab(AsyncRunner())
    try:
        result = asyncio.run(
            tab._compare_sources(
                (None, str(old)),
                (None, str(new)),
                ["i=85"],
                TraversalConfig(),
                ComparisonConfig(),
                None,
            )
        )
        tab.apply_result(result)

        assert result.is_equal, result.summary_text()
        assert tab.table.rowCount() == 0
        assert tab.headline_label.text().startswith("No difference found")
    finally:
        tab.deleteLater()


# -- end to end ------------------------------------------------------------


def test_compare_tab_runs_against_two_connected_sessions(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    port = free_port()
    server = start_demo_server(port)
    runner = AsyncRunner()
    runner.start()
    window = MainWindow(runner, OpcUaService(trust_store=TrustStore(tmp_path / "trust.json")), AppSettings())
    try:
        url = f"opc.tcp://127.0.0.1:{port}"
        connect_window(window, port, qapp)

        second = window.add_session()
        second.connection_tab.url_edit.setText(url)
        second.connection_tab._on_connect()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            qapp.processEvents()
            if second.service.is_connected:
                break
            time.sleep(0.03)
        assert second.service.is_connected, second.connection_tab.error_label.text()

        tab = window.compare_tab
        assert tab.reference_combo.count() == 3  # two sessions + the saved-document item
        tab.reference_combo.setCurrentIndex(0)
        tab.candidate_combo.setCurrentIndex(1)
        tab.max_nodes_spin.setValue(0)  # unlimited: a truncated side cannot be compared
        tab._on_compare()

        deadline = time.monotonic() + 120
        while tab._result is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.03)

        assert tab._result is not None, tab.error_label.text()
        assert tab._result.matched > 5
        assert tab._result.is_equal, tab._result.summary_text()
        assert tab.table.rowCount() == 0
        assert tab.headline_label.text().startswith("No difference found")
        assert tab.warnings_label.isVisible() is False
    finally:
        window.shutdown()
        for session_service in window.services:
            runner.run_until_complete(session_service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)
