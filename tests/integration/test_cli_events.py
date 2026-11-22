"""Headless ``opcua-events`` end to end against the demo server."""

from __future__ import annotations

import asyncio
import json

import pytest_asyncio
from asyncua import ua

from opcua_explorer import cli_events
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server


@pytest_asyncio.fixture
async def event_model():
    """Demo server that publishes one event every 300 ms on the Server object."""
    model = await start_demo_server(update_values=False, event_interval=0.3)
    try:
        yield model
    finally:
        await stop_demo_server(model)


def _argv(endpoint, tmp_path, *extra: str) -> list[str]:
    return ["--endpoint", endpoint, "--trust-store", str(tmp_path / "trust.json"), "--quiet", *extra]


async def test_cli_events_prints_events_as_json(event_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_events.main,
        _argv(event_model.endpoint, tmp_path, "--count", "1", "--seconds", "15", "--json"),
    )

    assert code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "no event was printed"
    payload = json.loads(lines[0])
    assert payload["event_type"] == "BaseEventType"
    assert payload["source_node"] == "i=2253"
    assert "Demo event" in payload["message"]
    # The identity an acknowledgement needs is part of the JSON output.
    assert payload["event_id"]


async def test_cli_events_refreshes_the_conditions_it_subscribed_to(event_model, tmp_path, capsys) -> None:
    # A short publishing interval keeps the refresh events well inside the client's
    # bounded observation window, also when the host is busy.
    code = await asyncio.to_thread(
        cli_events.main,
        _argv(
            event_model.endpoint,
            tmp_path,
            "--refresh",
            "--publishing-interval",
            "200",
            "--seconds",
            "3",
            "--json",
        ),
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    documents = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    refreshes = [entry["condition_refresh"] for entry in documents if "condition_refresh" in entry]
    assert refreshes, captured.out
    assert refreshes[0]["status"] == "passed"
    assert refreshes[0]["refresh_started"] is True
    # The refresh events themselves are ordinary events and are printed too.
    types = {entry["event_type"] for entry in documents if "event_type" in entry}
    assert {"RefreshStartEventType", "RefreshEndEventType"} <= types


async def test_cli_events_reports_an_empty_run(event_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_events.main,
        _argv(event_model.endpoint, tmp_path, "--seconds", "0.5", "--min-severity", "60000"),
    )

    assert code == 3
    assert capsys.readouterr().out.strip() == ""


async def test_cli_events_refuses_an_unconfirmed_acknowledgement(event_model, tmp_path, capsys) -> None:
    code = await asyncio.to_thread(
        cli_events.main,
        _argv(event_model.endpoint, tmp_path, "--acknowledge", "0102ff", "--condition", "ns=2;s=Condition"),
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--confirm" in captured.err
    assert captured.out.strip() == ""


async def test_cli_events_acknowledges_a_condition_once(event_model, tmp_path, capsys) -> None:
    calls: list[tuple[object, object]] = []
    objects = event_model.server.get_objects_node()
    condition = await objects.add_object(ua.NodeId("Condition", 2), ua.QualifiedName("Condition", 2))

    async def acknowledge(parent, event_id, comment):
        calls.append((event_id, comment))
        return []

    await condition.add_method(
        ua.NodeId("ConditionAcknowledge", 2),
        ua.QualifiedName("Acknowledge"),
        acknowledge,
        [ua.VariantType.ByteString, ua.VariantType.LocalizedText],
        [],
    )

    code = await asyncio.to_thread(
        cli_events.main,
        _argv(
            event_model.endpoint,
            tmp_path,
            "--acknowledge",
            "0102ff",
            "--condition",
            condition.nodeid.to_string(),
            "--comment",
            "checked by operator",
            "--confirm",
            "--seconds",
            "0.5",
        ),
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    # Exactly one call with the EventId and the comment the operator typed.
    assert len(calls) == 1
    event_id, comment = calls[0]
    assert event_id.Value == b"\x01\x02\xff"
    assert comment.Value.Text == "checked by operator"
    assert "not proof" in captured.err


async def test_cli_events_reaches_the_other_condition_methods(event_model, tmp_path, capsys) -> None:
    """``--condition-action`` sends the named method once, with the same EventId/Comment shape."""
    calls: list[tuple[object, object]] = []
    objects = event_model.server.get_objects_node()
    condition = await objects.add_object(
        ua.NodeId("CommentCondition", 2), ua.QualifiedName("CommentCondition", 2)
    )

    async def add_comment(parent, event_id, comment):
        calls.append((event_id, comment))
        return [ua.Variant(b"comment-id", ua.VariantType.ByteString)]

    await condition.add_method(
        ua.NodeId("CommentConditionAddComment", 2),
        ua.QualifiedName("AddComment"),
        add_comment,
        [ua.VariantType.ByteString, ua.VariantType.LocalizedText],
        [ua.VariantType.ByteString],
    )

    code = await asyncio.to_thread(
        cli_events.main,
        _argv(
            event_model.endpoint,
            tmp_path,
            "--condition-action",
            "comment",
            "--eventid",
            "0102ff",
            "--condition",
            condition.nodeid.to_string(),
            "--comment",
            "seen it",
            "--confirm",
            "--seconds",
            "0.5",
        ),
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert len(calls) == 1
    event_id, comment = calls[0]
    assert event_id.Value == b"\x01\x02\xff"
    assert comment.Value.Text == "seen it"
    assert "Add comment on" in captured.err
    assert "changes no condition state" in captured.err
