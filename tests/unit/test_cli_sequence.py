"""Unit tests for the ``opcua-run-sequence`` command surface (no server)."""

from __future__ import annotations

import pytest

from opcua_explorer import cli_sequence
from opcua_explorer.cli import CliError


def test_parser_requires_a_document_and_an_endpoint():
    parser = cli_sequence.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["seq.json"])

    args = parser.parse_args(["seq.json", "--endpoint", "opc.tcp://127.0.0.1:4840"])
    assert args.sequence == "seq.json"
    assert args.dry_run is False
    assert args.input == []
    assert args.mode == "None"


def test_inputs_are_parsed_as_name_value_pairs():
    assert cli_sequence.parse_inputs(["voltage=24.0", "settle=0.5"]) == {"voltage": "24.0", "settle": "0.5"}
    assert cli_sequence.parse_inputs(["text=a=b"]) == {"text": "a=b"}
    assert cli_sequence.parse_inputs([]) == {}


@pytest.mark.parametrize("value", ["voltage", "=24.0", ""])
def test_a_malformed_input_is_a_usage_error(value):
    with pytest.raises(CliError, match="NAME=VALUE"):
        cli_sequence.parse_inputs([value])


def test_a_missing_or_invalid_document_is_a_usage_error(tmp_path, capsys):
    missing = tmp_path / "missing.opcua-sequence.json"
    code = cli_sequence.main([str(missing), "--endpoint", "opc.tcp://127.0.0.1:4840", "--quiet"])

    assert code == 2
    assert "Could not read" in capsys.readouterr().err


def test_an_invalid_document_reports_the_reason(tmp_path, capsys):
    document = tmp_path / "bad.opcua-sequence.json"
    document.write_text('{"format": "opcua-explorer-sequence", "version": 1, "steps": [{"kind": "explode"}]}', encoding="utf-8")

    code = cli_sequence.main([str(document), "--endpoint", "opc.tcp://127.0.0.1:4840", "--quiet"])

    assert code == 2
    assert "unsupported step kind" in capsys.readouterr().err
