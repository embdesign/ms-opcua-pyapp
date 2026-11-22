"""Unit tests for ``opcua-nodes`` and ``opcua-status`` argument/output handling (no server).

Structural changes are the most dangerous headless action, so the argument gate is
tested before anything can connect: exactly one action, only the flags that belong to
it, and never without ``--confirm``.
"""

from __future__ import annotations

import argparse
import json

import pytest

from opcua_explorer import cli_nodes, cli_status
from opcua_explorer.cli import EXIT_USAGE, CliError
from opcua_explorer.errors import InvalidNodeIdError
from opcua_explorer.models import ServerStatusSnapshot


def _node_args(**overrides):
    base = {
        "create": None,
        "parent": None,
        "name": None,
        "display": None,
        "data_type": None,
        "value": None,
        "namespace": -1,
        "delete": None,
        "node": None,
        "attribute": None,
        "text": None,
        "confirm": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_exactly_one_action_is_required() -> None:
    assert cli_nodes._validate(_node_args(create="Object", parent="i=85", name="Line3")) == "create"
    assert cli_nodes._validate(_node_args(delete="ns=2;s=Line3")) == "delete"
    assert cli_nodes._validate(_node_args(node="ns=2;s=Line3", attribute="Description", text="x")) == "attribute"
    for args in (_node_args(), _node_args(delete="i=85", node="i=85", attribute="Description", text="x")):
        with pytest.raises(CliError, match="exactly one action"):
            cli_nodes._validate(args)


def test_create_needs_a_parent_and_a_name_and_rejects_foreign_flags() -> None:
    with pytest.raises(CliError, match="--parent and --name"):
        cli_nodes._validate(_node_args(create="Object", parent="i=85"))
    with pytest.raises(CliError, match="only apply with --attribute"):
        cli_nodes._validate(_node_args(create="Object", parent="i=85", name="X", text="y"))
    with pytest.raises(CliError, match="exactly one action"):
        cli_nodes._validate(_node_args(create="Object", parent="i=85", name="X", delete="i=85"))


def test_delete_takes_only_a_node_id() -> None:
    with pytest.raises(CliError, match="only the NodeId"):
        cli_nodes._validate(_node_args(delete="i=85", name="X"))
    with pytest.raises(CliError, match="only apply with --attribute"):
        cli_nodes._validate(_node_args(delete="i=85", node="i=85"))


def test_attribute_needs_a_node_and_a_text() -> None:
    with pytest.raises(CliError, match="needs --node and --text"):
        cli_nodes._validate(_node_args(attribute="Description"))
    with pytest.raises(CliError, match="only apply with --create"):
        cli_nodes._validate(_node_args(attribute="Description", node="i=85", text="x", parent="i=85"))


def test_an_unparsable_node_id_is_refused_before_connecting() -> None:
    with pytest.raises(InvalidNodeIdError):
        cli_nodes._validate(_node_args(delete="not a node id"))


def test_nothing_is_sent_without_confirm(capsys) -> None:
    # No server, no trust store needed: the gate runs before the connection.
    code = cli_nodes.main(
        ["--endpoint", "opc.tcp://127.0.0.1:1", "--delete", "ns=2;s=Line3", "--quiet"]
    )

    captured = capsys.readouterr()
    assert code == EXIT_USAGE
    assert "--confirm" in captured.err
    assert captured.out.strip() == ""


def test_status_text_lists_state_uptime_counters_and_limits() -> None:
    snapshot = ServerStatusSnapshot(
        state="Running",
        current_time="2026-11-22T09:00:00.000Z",
        start_time="2026-11-22T08:00:00.000Z",
        uptime_seconds=3600.0,
        diagnostics_enabled=True,
        counters={"SessionCount": 2},
        limits={"MaxNodesPerRead": 10000},
        not_provided=["SecondsTillShutdown"],
        unavailable={"ShutdownReason": "BadNotReadable"},
    )

    text = cli_status._text(snapshot, limits_only=False)

    assert "Running" in text
    assert "Uptime" in text
    assert "SessionCount" in text and "2" in text
    assert "MaxNodesPerRead" in text
    assert "Not provided by the server" in text and "SecondsTillShutdown" in text
    assert "BadNotReadable" in text


def test_status_limits_only_prints_just_the_limits() -> None:
    snapshot = ServerStatusSnapshot(state="Running", counters={"SessionCount": 2}, limits={"MaxSessions": 10})

    text = cli_status._text(snapshot, limits_only=True)

    assert "Capability limits" in text
    assert "MaxSessions" in text
    assert "SessionCount" not in text


def test_status_json_is_versioned(capsys) -> None:
    snapshot = ServerStatusSnapshot(state="Running", limits={"MaxSessions": 10})
    print(json.dumps({"format": cli_status.FORMAT, "version": cli_status.FORMAT_VERSION, "status": snapshot.to_dict()}))

    document = json.loads(capsys.readouterr().out)
    assert document["format"] == "opcua-status"
    assert document["status"]["state"] == "Running"
