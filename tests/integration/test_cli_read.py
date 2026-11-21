"""Headless ``opcua-read`` end to end against the demo server."""

from __future__ import annotations

import asyncio
import json

import pytest

from opcua_explorer import cli_read


def _argv(tmp_path, demo_model, *extra: str) -> list[str]:
    return [
        "--endpoint",
        demo_model.endpoint,
        "--trust-store",
        str(tmp_path / "trust.json"),
        "--quiet",
        *extra,
    ]


async def test_cli_read_prints_a_good_value(demo_model, tmp_path, capsys) -> None:
    node = demo_model.node_ids["temperature"]
    code = await asyncio.to_thread(cli_read.main, _argv(tmp_path, demo_model, "--node", node))

    assert code == 0
    output = capsys.readouterr().out
    assert node in output
    assert "Value:" in output
    assert "Good" in output


async def test_cli_read_json_reports_the_value(demo_model, tmp_path, capsys) -> None:
    node = demo_model.node_ids["temperature"]
    code = await asyncio.to_thread(cli_read.main, _argv(tmp_path, demo_model, "--node", node, "--json"))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "opcua-read"
    assert payload["reads"][0]["node_id"] == node
    assert payload["reads"][0]["status"] == "Good"
    assert payload["reads"][0]["value"] == pytest.approx(21.5)


async def test_cli_read_can_read_a_non_value_attribute(demo_model, tmp_path, capsys) -> None:
    node = demo_model.node_ids["temperature"]
    code = await asyncio.to_thread(
        cli_read.main, _argv(tmp_path, demo_model, "--node", node, "--attribute", "DisplayName")
    )

    assert code == 0
    assert "Temperature" in capsys.readouterr().out
