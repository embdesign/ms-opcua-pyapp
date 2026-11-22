"""Headless ``opcua-write`` end to end against the demo server."""

from __future__ import annotations

import asyncio
import json

import pytest

from opcua_explorer import cli_write


def _argv(tmp_path, demo_model, *extra: str) -> list[str]:
    return [
        "--endpoint",
        demo_model.endpoint,
        "--trust-store",
        str(tmp_path / "trust.json"),
        "--node",
        demo_model.node_ids["temperature"],
        "--quiet",
        *extra,
    ]


async def test_without_confirm_nothing_is_sent(service, demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_write.main, _argv(tmp_path, demo_model, "--value", "42.5")
    )

    assert code == 2
    assert "Nothing was sent" in capsys.readouterr().err
    assert (await service.read_value(demo_model.node_ids["temperature"])).value == pytest.approx(21.5)


async def test_confirm_sends_one_write_and_reads_it_back(service, demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_write.main, _argv(tmp_path, demo_model, "--value", "42.5", "--confirm", "--json")
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["write"]["read_back_value"] == "42.5"
    assert (await service.read_value(demo_model.node_ids["temperature"])).value == pytest.approx(42.5)


async def test_restore_writes_the_previous_value_back(service, demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_write.main,
        _argv(tmp_path, demo_model, "--value", "43.0", "--confirm", "--restore", "--json"),
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["restore"]["status"] == "passed"
    assert (await service.read_value(demo_model.node_ids["temperature"])).value == pytest.approx(21.5)


async def test_a_refused_value_is_previewed_and_not_sent(service, demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_write.main, _argv(tmp_path, demo_model, "--value", "not-a-number", "--confirm")
    )

    assert code == 2
    assert "Refused" in capsys.readouterr().err
    assert (await service.read_value(demo_model.node_ids["temperature"])).value == pytest.approx(21.5)
