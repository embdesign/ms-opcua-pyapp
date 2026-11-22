"""Headless ``opcua-status``, ``opcua-directory`` and ``opcua-nodes`` against the demo server.

The demo server's anonymous user is not an admin, so structural changes are refused
with ``BadUserAccessDenied``. That is the honest end-to-end result and it is asserted
as such: the request leaves, the server's StatusCode comes back, and the exit code says
so. The success path of node management is covered by ``test_node_management.py``
against the service.
"""

from __future__ import annotations

import asyncio
import json

from opcua_explorer import cli_directory, cli_nodes, cli_status


def _argv(endpoint, tmp_path, *extra: str) -> list[str]:
    return ["--endpoint", endpoint, "--trust-store", str(tmp_path / "trust.json"), "--quiet", *extra]


async def test_cli_status_reads_the_snapshot(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(cli_status.main, _argv(demo_model.endpoint, tmp_path, "--json"))

    captured = capsys.readouterr()
    assert code == 0, captured.err
    document = json.loads(captured.out)
    assert document["format"] == "opcua-status"
    assert document["status"]["state"] == "Running"
    # The capability limits the client depends on are part of the answer.
    assert document["status"]["limits"]


async def test_cli_status_limits_only_is_a_short_answer(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(cli_status.main, _argv(demo_model.endpoint, tmp_path, "--limits-only"))

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "Capability limits" in captured.out
    assert "State" not in captured.out.split("\n")[0]


async def test_cli_directory_asks_the_endpoint_without_a_session(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_directory.main, ["--discovery-url", demo_model.endpoint, "--quiet", "--json"]
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    document = json.loads(captured.out)
    assert document["format"] == "opcua-directory"
    servers = document["directory"]["servers"]
    assert servers and servers[0]["application_uri"] == "urn:opcua-explorer:demo:server"
    # A lookup only: it reports the advertised URL, it does not connect to it.
    assert servers[0]["discovery_urls"]


async def test_cli_nodes_refuses_to_act_without_confirm(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_nodes.main, _argv(demo_model.endpoint, tmp_path, "--delete", demo_model.node_ids["plant"])
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--confirm" in captured.err
    assert captured.out.strip() == ""


async def test_cli_nodes_reports_the_servers_refusal(demo_model, tmp_path, capsys) -> None:
    """One ``DeleteNodes`` leaves and the server's answer decides the exit code."""
    code = await asyncio.to_thread(
        cli_nodes.main,
        _argv(demo_model.endpoint, tmp_path, "--delete", demo_model.node_ids["plant"], "--confirm", "--json"),
    )

    captured = capsys.readouterr()
    assert code == 3, captured.out + captured.err
    document = json.loads(captured.out)
    assert document["action"] == "delete"
    assert document["delete"]["sent"] is True
    assert document["delete"]["status_code"] == "BadUserAccessDenied"
    assert document["status"] == "failed"


async def test_cli_nodes_reports_a_refused_create(demo_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_nodes.main,
        _argv(
            demo_model.endpoint,
            tmp_path,
            "--create",
            "Object",
            "--parent",
            demo_model.node_ids["line1"],
            "--name",
            "Line3",
            "--confirm",
        ),
    )

    captured = capsys.readouterr()
    assert code == 3, captured.out + captured.err
    assert "BadUserAccessDenied" in captured.out
