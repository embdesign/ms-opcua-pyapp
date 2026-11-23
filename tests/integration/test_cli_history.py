"""Headless ``opcua-history`` end to end against the demo historian."""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from opcua_explorer import cli_history
from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import HistoryConfig
from opcua_explorer.opcua.client_service import OpcUaService


@pytest_asyncio.fixture
async def historized_model(tmp_path):
    """Demo server with Temperature historized; three values are written first."""
    model = await start_demo_server(update_values=False, history=True)
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
    try:
        for value in (21.5, 22.0, 22.5):
            await model.temperature.write_value(value)
            await asyncio.sleep(0.05)
        assert len((await service.read_history(model.node_ids["temperature"], HistoryConfig())).values) == 3
        yield model
    finally:
        await service.shutdown()
        await stop_demo_server(model)


def _argv(tmp_path, model, *extra: str) -> list[str]:
    return [
        "--endpoint",
        model.endpoint,
        "--trust-store",
        str(tmp_path / "trust.json"),
        "--node",
        model.node_ids["temperature"],
        "--quiet",
        *extra,
    ]


async def test_cli_history_prints_the_raw_values(historized_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(cli_history.main, _argv(tmp_path, historized_model, "--json"))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    result = payload["result"]
    assert result["status"] == "Good"
    assert result["historizing"] is True
    assert [value["value"] for value in result["values"]] == [21.5, 22.0, 22.5]


async def test_cli_history_reports_an_unsupported_aggregate(historized_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_history.main,
        _argv(
            tmp_path,
            historized_model,
            "--aggregate",
            "average",
            "--from",
            "2000-01-01T00:00:00Z",
            "--to",
            "2100-01-01T00:00:00Z",
        ),
    )

    assert code == 3
    output = capsys.readouterr().out
    assert "BadNotImplemented" in output
    assert "aggregate history" in output


async def test_cli_history_csv_contains_a_header_and_all_rows(historized_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(cli_history.main, _argv(tmp_path, historized_model, "--csv"))

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("source_timestamp,")
    assert len(lines) == 4  # header + three values
    assert [float(line.rsplit(",", 1)[1]) for line in lines[1:]] == pytest.approx([21.5, 22.0, 22.5])


async def test_cli_history_update_reports_the_demo_refusal(historized_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_history.main,
        _argv(
            tmp_path,
            historized_model,
            "--update",
            "insert",
            "--entry",
            "2026-11-21T10:00:00Z=21.5",
            "--confirm",
            "--json",
        ),
    )

    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "opcua-history-update"
    assert payload["result"]["sent"] is True
    assert payload["result"]["status_code"] != "Good"
