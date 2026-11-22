"""Headless ``opcua-monitor`` end to end against the demo server."""

from __future__ import annotations

import asyncio
import json

from opcua_explorer import cli_monitor


def _argv(tmp_path, demo_model, *extra: str) -> list[str]:
    return [
        "--endpoint",
        demo_model.endpoint,
        "--trust-store",
        str(tmp_path / "trust.json"),
        "--quiet",
        *extra,
    ]


async def test_cli_monitor_prints_the_initial_notification(demo_model, tmp_path, capsys) -> None:
    node = demo_model.node_ids["temperature"]
    code = await asyncio.to_thread(
        cli_monitor.main,
        _argv(tmp_path, demo_model, "--node", node, "--count", "1", "--seconds", "5", "--json"),
    )

    assert code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "no notification was printed"
    payload = json.loads(lines[0])
    assert payload["node_id"] == node
    assert payload["status"] == "Good"


async def test_cli_monitor_writes_the_same_rows_to_a_file(demo_model, tmp_path, capsys) -> None:
    """``--output`` writes the same notifications the command printed, as a document."""
    node = demo_model.node_ids["temperature"]
    target = tmp_path / "log.json"

    code = await asyncio.to_thread(
        cli_monitor.main,
        _argv(
            tmp_path,
            demo_model,
            "--node",
            node,
            "--count",
            "1",
            "--seconds",
            "10",
            "--json",
            "--output",
            str(target),
        ),
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    printed = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert printed, "no notification was printed"
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["format"] == "opcua-explorer-monitor-log"
    assert document["endpoint_url"] == demo_model.endpoint
    assert document["count"] == len(document["notifications"]) == len(printed)
    assert document["node_ids"] == [node]
    # One row per printed notification, in the same order.
    assert [row["sequence"] for row in document["notifications"]] == [row["sequence"] for row in printed]
