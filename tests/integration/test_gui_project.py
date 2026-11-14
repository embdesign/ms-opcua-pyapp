"""Headless GUI project-file test: save/load configuration without secrets."""

from __future__ import annotations

from opcua_explorer.config import AppSettings, TrustStore
from opcua_explorer.gui.async_runner import AsyncRunner
from opcua_explorer.gui.main_window import MainWindow
from opcua_explorer.models import WriteTarget
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.project import ExportState, MonitorState, TestsState


def test_project_file_roundtrips_configuration_without_secrets(qapp, tmp_path, monkeypatch) -> None:
    """Save project -> change everything -> load it back; nothing is connected or run."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = AsyncRunner()
    runner.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    window = MainWindow(runner, service, AppSettings())
    try:
        workspace = window.workspaces[0]
        connection = workspace.connection_tab
        connection.url_edit.setText("opc.tcp://192.168.20.4:4840")
        connection.connect_timeout_spin.setValue(6.5)
        connection.request_timeout_spin.setValue(4.5)
        connection.auto_reconnect_check.setChecked(False)
        workspace.monitor_tab.apply_monitor_state(
            MonitorState(
                node_ids=["ns=2;s=Temperature"],
                publishing_interval_ms=750.0,
                sampling_interval_ms=300.0,
                keep_rows=400,
            )
        )
        workspace.export_tab.apply_export_state(
            ExportState(
                scope="subtree",
                subtree_node_id="ns=2;s=Plant",
                max_nodes=321,
                max_depth=4,
                concurrency=2,
                batch_size=11,
                max_references_per_node=7,
                include_values=True,
                namespaces=[0, 2],
                include_dependencies=False,
                max_dependency_nodes=50,
                schema_path="/tmp/nodeset.xsd",
            )
        )
        workspace.tests_tab.apply_tests_state(
            TestsState(
                variable_node_id="ns=2;s=Speed",
                latency_samples=3,
                include_channel_recovery=True,
                write_targets=[WriteTarget(node_id="ns=2;s=Speed", value_text="42")],
            )
        )

        second = window.add_session()
        second.connection_tab.url_edit.setText("opc.tcp://127.0.0.1:4841")
        second.set_project_title("Pump")

        path = tmp_path / "plant.opcua-project.json"
        window.save_project(path)
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert '"password"' not in text
        assert "private_key_password" not in text
        assert "server_certificate_der" not in text
        assert "allow_writes" not in text, "guarded writes stay opt-in per run"

        # Change the live editors, then load the file back.
        connection.url_edit.setText("opc.tcp://changed:4840")
        connection.auto_reconnect_check.setChecked(True)
        window.load_project(path)

        assert len(window.workspaces) == 2
        first = window.workspaces[0]
        assert first.title == "Session 1"
        assert window.workspaces[1].title == "Pump"
        assert first.connection_tab.url_edit.text() == "opc.tcp://192.168.20.4:4840"
        assert first.connection_tab.connect_timeout_spin.value() == 6.5
        assert first.connection_tab.request_timeout_spin.value() == 4.5
        assert first.connection_tab.auto_reconnect_check.isChecked() is False
        # Monitor targets come back as pending rows: no subscription was created.
        assert first.monitor_tab.monitored_node_ids() == {"ns=2;s=Temperature"}
        assert first.monitor_tab.is_running is False
        assert first.monitor_tab.publishing_spin.value() == 750.0
        assert first.monitor_tab.sampling_spin.value() == 300.0
        assert first.monitor_tab.max_rows_spin.value() == 400
        assert first.export_tab.subtree_radio.isChecked()
        assert first.export_tab.subtree_edit.text() == "ns=2;s=Plant"
        assert first.export_tab.max_nodes_spin.value() == 321
        assert first.export_tab.max_depth_spin.value() == 4
        assert first.export_tab.concurrency_spin.value() == 2
        assert first.export_tab.batch_size_spin.value() == 11
        assert first.export_tab.include_values_check.isChecked()
        assert first.export_tab.dependencies_check.isChecked() is False
        assert first.export_tab.max_dependencies_spin.value() == 50
        assert first.export_tab.schema_edit.text() == "/tmp/nodeset.xsd"
        assert first.export_tab._pending_namespace_checks == [0, 2]
        assert first.tests_tab.variable_edit.text() == "ns=2;s=Speed"
        assert first.tests_tab.latency_spin.value() == 3
        assert first.tests_tab.recovery_check.isChecked() is True
        assert first.tests_tab.allow_writes_check.isChecked() is False
        assert len(first.tests_tab._write_targets) == 1
        # Nothing was connected, subscribed or run.
        assert window.services[0].is_connected is False
        assert first.browser_tab.load_btn.isEnabled() is False
    finally:
        window.shutdown()
        for session_service in window.services:
            runner.run_until_complete(session_service.shutdown, timeout=8.0)
        runner.stop()
