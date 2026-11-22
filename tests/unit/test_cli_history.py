"""Unit tests for the headless history CLI (no network)."""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from opcua_explorer import cli_history
from opcua_explorer.cli import CliError
from opcua_explorer.models import HistoryResult, ValueSnapshot

ENDPOINT = "opc.tcp://127.0.0.1:4840"


def _result() -> HistoryResult:
    return HistoryResult(
        node_id="ns=2;s=Temperature",
        status="Good",
        historizing=True,
        values=[
            ValueSnapshot(value=21.5, status="Good", source_timestamp="2026-11-21T10:00:00.000Z"),
            ValueSnapshot(value=22.0, status="Good", source_timestamp="2026-11-21T10:00:01.000Z"),
        ],
    )


def test_parser_defaults_are_bounded_and_raw() -> None:
    args = cli_history.build_parser().parse_args(["--endpoint", ENDPOINT, "--node", "i=85"])
    assert args.max_values == 1_000
    assert args.aggregate is None
    assert args.processing_interval == 0.0
    assert args.json is False and args.csv is False


def test_parse_time_accepts_z_and_naive_values() -> None:
    assert cli_history._parse_time("2026-11-21T10:00:00Z", "--from").tzinfo == UTC
    assert cli_history._parse_time("2026-11-21T10:00:00", "--from").tzinfo == UTC
    assert cli_history._parse_time(None, "--from") is None
    with pytest.raises(CliError):
        cli_history._parse_time("yesterday", "--from")


def test_json_output_is_versioned(capsys) -> None:
    cli_history._print_result(_result(), as_json=True, as_csv=False)
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "opcua-history"
    assert payload["version"] == 1
    assert payload["result"]["node_id"] == "ns=2;s=Temperature"
    assert len(payload["result"]["values"]) == 2


def test_csv_output_has_a_header_and_rows(capsys) -> None:
    cli_history._print_result(_result(), as_json=False, as_csv=True)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "source_timestamp,server_timestamp,status,variant_type,value"
    assert lines[1].startswith("2026-11-21T10:00:00.000Z,")
    assert lines[1].endswith(",21.5")


def test_text_output_starts_with_the_summary(capsys) -> None:
    cli_history._print_result(_result(), as_json=False, as_csv=False)
    lines = capsys.readouterr().out.splitlines()
    assert "2 value(s)" in lines[0]
    assert "21.5" in lines[1]
