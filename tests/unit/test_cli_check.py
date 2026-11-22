"""Unit tests for the ``opcua-check`` argument rules and output (no server).

The headless twin of the Tests tab must refuse a guarded run that was not asked for
and confirmed *before* it connects, so most of these tests are about that gate.
"""

from __future__ import annotations

import argparse
import json

import pytest

from opcua_explorer import cli_check
from opcua_explorer.cli import CliError
from opcua_explorer.models import TestCheck, TestRun


def _args(**overrides):
    base = {
        "root": "i=85",
        "variable": None,
        "publishing_interval": 500.0,
        "sampling_interval": 250.0,
        "notification_wait": 3.0,
        "latency_samples": 5,
        "include_recovery": False,
        "include_writes": False,
        "write": None,
        "call": None,
        "restore": False,
        "confirm": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_write_target_parsing_requires_node_and_value() -> None:
    target = cli_check._write_target("ns=2;s=Temperature=21.5")
    assert target.node_id == "ns=2;s=Temperature"
    assert target.value_text == "21.5"
    # The split is at the *last* '=', because a NodeId contains '=' itself.
    assert cli_check._write_target("ns=2;s=Recipe=x=y").node_id == "ns=2;s=Recipe=x"
    for text in ("ns=2;s=Temperature", "=21.5", "ns=2;s=X=", ""):
        with pytest.raises(CliError, match="NODEID=VALUE"):
            cli_check._write_target(text)


def test_method_call_parsing_takes_object_method_and_arguments() -> None:
    call = cli_check._method_call("ns=2;s=Motor|ns=2;s=Start|1450, true")
    assert call.object_node_id == "ns=2;s=Motor"
    assert call.method_node_id == "ns=2;s=Start"
    assert call.arguments == ["1450", "true"]
    assert cli_check._method_call("ns=2;s=Motor|ns=2;s=Stop").arguments == []
    with pytest.raises(CliError, match="OBJECT\\|METHOD"):
        cli_check._method_call("ns=2;s=Motor")


def test_guarded_targets_need_the_switch_and_the_confirmation() -> None:
    with pytest.raises(CliError, match="only apply with --include-writes"):
        cli_check._config_from_args(_args(write=["ns=2;s=T=1"]))
    with pytest.raises(CliError, match="needs at least one"):
        cli_check._config_from_args(_args(include_writes=True, confirm=True))
    with pytest.raises(CliError, match="--confirm"):
        cli_check._config_from_args(_args(include_writes=True, write=["ns=2;s=T=1"]))
    # --confirm alone never pretends that something guarded was requested.
    with pytest.raises(CliError, match="only applies with --include-writes"):
        cli_check._config_from_args(_args(confirm=True))
    with pytest.raises(CliError, match="only applies to --write"):
        cli_check._config_from_args(
            _args(include_writes=True, confirm=True, call=["ns=2;s=M|ns=2;s=S"], restore=True)
        )


def test_the_confirmed_configuration_carries_the_targets() -> None:
    config = cli_check._config_from_args(
        _args(
            include_writes=True,
            confirm=True,
            write=["ns=2;s=Temperature=21.5"],
            call=["ns=2;s=Motor|ns=2;s=Start|1450"],
            restore=True,
            include_recovery=True,
            variable="ns=2;s=Temperature",
        )
    )

    assert config.allow_writes is True
    assert config.include_channel_recovery is True
    assert config.variable_node_id == "ns=2;s=Temperature"
    assert [(target.node_id, target.value_text) for target in config.write_targets] == [
        ("ns=2;s=Temperature", "21.5")
    ]
    # --restore asks for the explicit, separately verified restore write.
    assert config.write_targets[0].restore_previous_value is True
    assert config.method_calls[0].arguments == ["1450"]


def test_a_read_only_configuration_has_no_targets() -> None:
    config = cli_check._config_from_args(_args())

    assert config.allow_writes is False
    assert config.write_targets == []
    assert config.method_calls == []
    assert config.root_node_id == "i=85"


def test_json_output_is_versioned_and_names_every_check(capsys) -> None:
    run = TestRun(endpoint_url="opc.tcp://x:4840")
    run.checks = [
        TestCheck(id="endpoint_discovery", name="Endpoint discovery", category="env", status="passed"),
        TestCheck(id="connection_recovery", name="Recovery", category="env", status="skipped"),
    ]

    cli_check._print_run(run, as_json=True)

    document = json.loads(capsys.readouterr().out)
    # The Tests tab's canonical document, not a second format for the CLI.
    assert document["format"] == "opcua-functional-test-report"
    assert "disclaimer" in document
    assert [entry["id"] for entry in document["results"]] == [
        "endpoint_discovery",
        "connection_recovery",
    ]


def test_text_output_marks_every_verdict(capsys) -> None:
    run = TestRun(endpoint_url="opc.tcp://x:4840")
    run.checks = [
        TestCheck(id="a.one", name="First", category="parser", status="passed"),
        TestCheck(id="b.two", name="Second", category="parser", status="failed", detail="boom"),
    ]

    cli_check._print_run(run, as_json=False)

    out = capsys.readouterr().out
    assert "not official OPC UA certification" in out
    assert "ok    a.one" in out
    assert "FAIL  b.two" in out
    assert "boom" in out


def test_report_path_must_be_json_or_html(tmp_path) -> None:
    run = TestRun(endpoint_url="opc.tcp://x:4840")
    with pytest.raises(CliError, match="needs a .json or .html path"):
        cli_check._write_report(run, str(tmp_path / "report.txt"))


def test_parser_defaults_are_read_only() -> None:
    args = cli_check.build_parser().parse_args(["--endpoint", "opc.tcp://x:4840"])

    assert args.root == "i=85"
    assert args.include_writes is False
    assert args.confirm is False
    assert args.include_recovery is False
    assert args.report is None
