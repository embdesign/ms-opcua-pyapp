"""Unit tests for the headless monitor CLI (no network)."""

from __future__ import annotations

from datetime import UTC, datetime

from opcua_explorer import cli_monitor
from opcua_explorer.models import MonitorNotification

ENDPOINT = "opc.tcp://127.0.0.1:4840"


def _args(*extra: str):
    return cli_monitor.build_parser().parse_args(["--endpoint", ENDPOINT, "--node", "i=85", *extra])


def test_defaults_run_for_ten_seconds_without_a_count_limit() -> None:
    args = _args()
    assert args.seconds == 10.0
    assert args.count == 0
    assert args.node == ["i=85"]


def test_notification_line_names_status_value_and_timestamp() -> None:
    notification = MonitorNotification(
        sequence=1,
        node_id="ns=2;s=Temperature",
        value=42.5,
        status="Good",
        source_timestamp="2026-11-21T10:00:00.000Z",
    )
    line = cli_monitor._notification_line(notification)
    assert "ns=2;s=Temperature" in line
    assert "Good" in line
    assert "42.5" in line
    assert "2026-11-21T10:00:00.000Z" in line


def test_long_values_are_bounded() -> None:
    notification = MonitorNotification(
        sequence=1,
        node_id="i=1",
        value="x" * 1_000,
        received_at=datetime(2026, 11, 21, tzinfo=UTC),
    )
    line = cli_monitor._notification_line(notification)
    assert "…" in line
    assert line.count("x") <= cli_monitor._VALUE_CELL_CHARS
    assert len(line) < 400
