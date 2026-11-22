"""Headless ``opcua-check`` end to end against the demo server."""

from __future__ import annotations

import asyncio
import json

from opcua_explorer import cli_check


def _argv(endpoint, tmp_path, *extra: str) -> list[str]:
    return ["--endpoint", endpoint, "--trust-store", str(tmp_path / "trust.json"), "--quiet", *extra]


async def test_cli_check_runs_the_read_only_suite(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_check.main,
        _argv(demo_model.endpoint, tmp_path, "--variable", demo_model.node_ids["temperature"]),
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "Functional and interoperability checks" in captured.out
    assert "0 failed" in captured.out
    # The read-only suite never writes and skips the disruptive check.
    assert "connection_recovery" in captured.out
    assert "skip" in captured.out


async def test_cli_check_writes_the_tests_tab_report(demo_model, tmp_path, capsys) -> None:
    report = tmp_path / "checks.json"
    code = await asyncio.to_thread(
        cli_check.main,
        _argv(demo_model.endpoint, tmp_path, "--variable", demo_model.node_ids["temperature"], "--report", str(report)),
    )

    assert code == 0, capsys.readouterr().err
    document = json.loads(report.read_text(encoding="utf-8"))
    # The same canonical document the Tests tab writes, with the disclaimer.
    assert document["format"] == "opcua-functional-test-report"
    assert document["disclaimer"]
    assert document["summary"]["failed"] == 0


async def test_cli_check_json_output_is_the_run_document(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_check.main,
        _argv(demo_model.endpoint, tmp_path, "--variable", demo_model.node_ids["temperature"], "--json"),
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    document = json.loads(captured.out)
    ids = [entry["id"] for entry in document["results"]]
    assert "variable_read" in ids
    assert all(entry["status"] != "failed" for entry in document["results"]), document["results"]


async def test_cli_check_refuses_a_guarded_run_without_confirm(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_check.main,
        _argv(
            demo_model.endpoint,
            tmp_path,
            "--include-writes",
            "--write",
            f"{demo_model.node_ids['temperature']}=33.5",
        ),
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--confirm" in captured.err
    # Refused before connecting: nothing was read and nothing was printed.
    assert captured.out.strip() == ""


async def test_cli_check_guarded_write_runs_once_and_restores(demo_model, tmp_path, capsys) -> None:
    node = demo_model.node_ids["temperature"]
    code = await asyncio.to_thread(
        cli_check.main,
        _argv(
            demo_model.endpoint,
            tmp_path,
            "--variable",
            node,
            "--include-writes",
            "--confirm",
            "--write",
            f"{node}=33.5",
            "--restore",
        ),
    )

    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    # The equipment warning the GUI confirmation shows is printed before the run.
    assert "EQUIPMENT" in captured.err.upper() or "equipment" in captured.err
    assert "Guarded write" in captured.out
    # --restore is one further verified write and says so in the verdict line.
    assert "Restored to 21.5" in captured.out

    # --restore put the original value back; the check itself passed.
    from opcua_explorer.config import ConnectionSettings, TrustStore
    from opcua_explorer.opcua.client_service import OpcUaService

    service = OpcUaService(trust_store=TrustStore(tmp_path / "verify-trust.json"))
    try:
        await service.connect(ConnectionSettings(endpoint_url=demo_model.endpoint))
        snapshot = await service.read_value(node)
    finally:
        await service.shutdown()
    assert snapshot.value == 21.5
