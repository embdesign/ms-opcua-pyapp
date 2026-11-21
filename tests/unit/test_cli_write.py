"""Unit tests for the headless guarded-write CLI (no network)."""

from __future__ import annotations

from types import SimpleNamespace

from opcua_explorer import cli_write
from opcua_explorer.models import STATUS_OUTCOME_UNKNOWN, STATUS_PASSED

ENDPOINT = "opc.tcp://127.0.0.1:4840"


def _args(*extra: str):
    return cli_write.build_parser().parse_args(
        ["--endpoint", ENDPOINT, "--node", "ns=2;s=Temperature", "--value", "42.5", *extra]
    )


def _result(status: str = STATUS_PASSED, *, restore: bool = False):
    write = SimpleNamespace(
        node_id="ns=2;s=Temperature",
        status=status,
        detail="write: Good; read back: 42.5",
        sent=True,
        write_status_code="Good",
        read_back_value="42.5",
        read_back_status_code="Good",
        previous_value="21.5",
    )
    restored = SimpleNamespace(
        node_id="ns=2;s=Temperature",
        status="passed",
        detail="restored 21.5",
        sent=True,
        written_value="21.5",
        read_back_value="21.5",
        read_back_status_code="Good",
    )
    return SimpleNamespace(
        write=write,
        restore=restored if restore else None,
        status=status,
        summary_text=lambda: "write: Good; read back: 42.5",
    )


def test_confirm_is_off_by_default() -> None:
    args = _args()
    assert args.confirm is False
    assert args.restore is False


def test_document_reports_write_and_restore() -> None:
    document = cli_write._write_document(_result(restore=True))
    assert document["format"] == "opcua-write"
    assert document["status"] == STATUS_PASSED
    assert document["write"]["read_back_value"] == "42.5"
    assert document["restore"]["written_value"] == "21.5"


def test_unknown_outcome_is_reported_as_such(capsys) -> None:
    document = cli_write._write_document(_result(STATUS_OUTCOME_UNKNOWN))
    cli_write._print_result(document)
    output = capsys.readouterr().out
    assert "outcome_unknown" in output
    assert "read back: 42.5" in output
