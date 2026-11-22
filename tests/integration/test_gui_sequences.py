"""Headless GUI tests for the Sequences tab (load, dry run, run, project state)."""

from __future__ import annotations

import asyncio
import json
import time

from PySide6.QtWidgets import QMessageBox

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.models import STATUS_PASSED
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.project import SequenceState

from .gui_support import connect_window, free_port, start_demo_server, stop_demo_server


def _wait_until(qapp, predicate, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.03)
    return False


def _sequence_document(node_id: str) -> dict:
    return {
        "format": "opcua-explorer-sequence",
        "version": 1,
        "name": "Demo setpoint",
        "description": "Writes the demo temperature and reads it back.",
        "inputs": {"value": "42.5"},
        "steps": [
            {"kind": "write", "id": "set", "node_id": node_id, "value": "${value}", "verify": True},
            {"kind": "expect", "id": "check", "node_id": node_id, "expected": "${value}"},
        ],
    }


def test_sequences_tab_loads_dry_runs_and_runs(qapp, tmp_path) -> None:
    port = free_port()
    # --no-updates: the dry run must see the value the document expects, not a value
    # the demo updater moved while the test was waiting.
    server = start_demo_server(port, "--no-updates")
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        connect_window(window, port, qapp)
        tab = window.workspaces[0].sequences_tab
        document = tmp_path / "demo.opcua-sequence.json"
        document.write_text(json.dumps(_sequence_document("ns=2;s=Temperature")), encoding="utf-8")

        assert tab.load_sequence(document) is True
        assert tab.name_label.text() == "Demo setpoint"
        assert tab.steps_table.rowCount() == 2
        assert tab.inputs_table.rowCount() == 1
        assert tab.inputs_table.item(0, 1).text() == "42.5"
        assert tab.run_button.isEnabled(), tab.status_label.text()

        # Dry run: everything is resolved, nothing is written.
        tab._on_run(dry_run=True)
        assert _wait_until(qapp, lambda: tab._run is not None), tab.status_label.text()
        run = tab._run
        assert run is not None and run.dry_run is True
        assert run.status == STATUS_PASSED, run.error
        assert tab.results_table.rowCount() == len(run.results)
        value = runner.run_until_complete(lambda: service.read_value("ns=2;s=Temperature"), timeout=10.0)
        assert value.value == 21.5

        # Real run: the input value is substituted and read back.
        tab.inputs_table.item(0, 1).setText("33.5")
        tab._on_run(dry_run=False)
        assert _wait_until(qapp, lambda: tab._run is not None), tab.status_label.text()
        run = tab._run
        assert run is not None and run.dry_run is False
        assert run.status == STATUS_PASSED, run.error
        assert "Gate:" in tab.gate_label.text()
        value = runner.run_until_complete(lambda: service.read_value("ns=2;s=Temperature"), timeout=10.0)
        assert value.value == 33.5
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
        stop_demo_server(server)


def test_closing_a_session_cancels_a_running_sequence(qapp, tmp_path) -> None:
    """Closing the session tab must cancel the tab's run, not leave it on the loop."""
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        workspace = window.workspaces[0]
        tab = workspace.sequences_tab

        async def long_run() -> None:
            await asyncio.sleep(30.0)

        key = runner.new_key()
        tab._task_key = key
        tab._handle = runner.submit(long_run, key=key)
        assert _wait_until(qapp, lambda: runner.is_active(key)), "the run did not start"

        window.close_session(workspace)

        assert tab._handle is None
        assert tab._task_key is None
        assert _wait_until(qapp, lambda: not runner.is_active(key)), (
            "the sequence kept running after its session was closed"
        )
    finally:
        window.shutdown()
        runner.stop()


def test_sequences_tab_refuses_a_bad_document_and_keeps_state(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok)
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        tab = window.workspaces[0].sequences_tab
        assert tab.run_button.isEnabled() is False

        broken = tmp_path / "broken.opcua-sequence.json"
        broken.write_text(json.dumps({"format": "opcua-explorer-sequence", "version": 1, "steps": [{"kind": "explode"}]}), encoding="utf-8")
        assert tab.load_sequence(broken) is False
        assert "unsupported step kind" in tab.status_label.text()

        good = tmp_path / "good.opcua-sequence.json"
        good.write_text(json.dumps(_sequence_document("ns=2;s=Temperature")), encoding="utf-8")
        assert tab.load_sequence(good) is True
        tab.inputs_table.item(0, 1).setText("12.0")

        state = tab.sequence_state()
        assert state.path == str(good)
        assert state.inputs == {"value": "12.0"}

        # A project restore re-loads the document and keeps the saved input value.
        tab.apply_sequence_state(SequenceState(path=str(good), inputs={"value": "7.5"}))
        assert tab.inputs_table.item(0, 1).text() == "7.5"

        # A missing document is reported, not fatal.
        tab.apply_sequence_state(SequenceState(path=str(tmp_path / "missing.json"), inputs={}))
        assert "not available" in tab.status_label.text()
    finally:
        window.shutdown()
        runner.run_until_complete(service.shutdown, timeout=8.0)
        runner.stop()
