"""Unit tests for the ``opcua-events`` argument rules and line formatting (no server).

The headless twin of the Events tab must refuse an incomplete or unconfirmed
acknowledgement *before* it connects, so these tests are about that gate and the
text the command prints.
"""

from __future__ import annotations

import pytest

from opcua_explorer import cli_events
from opcua_explorer.cli import CliError
from opcua_explorer.models import EventNotification
from opcua_explorer.opcua.event_service import SERVER_OBJECT_NODE_ID


def _args(**overrides):
    base = {
        "acknowledge": None,
        "condition": None,
        "comment": "",
        "confirm": False,
    }
    base.update(overrides)
    return type("Args", (), base)()


def test_acknowledge_needs_a_condition_and_a_confirmation() -> None:
    with pytest.raises(CliError, match="needs --condition"):
        cli_events._check_condition_arguments(_args(acknowledge="0102ff", confirm=True))
    with pytest.raises(CliError, match="--confirm"):
        cli_events._check_condition_arguments(_args(acknowledge="0102ff", condition="ns=2;s=Cond"))
    # The complete triple is accepted and sends exactly one call later.
    cli_events._check_condition_arguments(
        _args(acknowledge="0102ff", condition="ns=2;s=Cond", comment="checked", confirm=True)
    )


def test_condition_options_do_not_apply_without_an_acknowledgement() -> None:
    with pytest.raises(CliError, match="only apply together"):
        cli_events._check_condition_arguments(_args(condition="ns=2;s=Cond"))
    with pytest.raises(CliError, match="only apply together"):
        cli_events._check_condition_arguments(_args(comment="checked"))
    # A bare --confirm never pretends to have confirmed anything.
    with pytest.raises(CliError, match="--confirm only applies"):
        cli_events._check_condition_arguments(_args(confirm=True))
    cli_events._check_condition_arguments(_args())


def test_parser_defaults_target_the_server_object_and_run_for_ten_seconds() -> None:
    args = cli_events.build_parser().parse_args(["--endpoint", "opc.tcp://x:4840"])

    # The source is chosen in _run so that --help shows the real default.
    assert args.source is None
    assert SERVER_OBJECT_NODE_ID == "i=2253"
    assert args.event_type == ""
    assert args.seconds == 10.0
    assert args.count == 0
    assert args.refresh is False
    assert args.select is None


def test_event_line_names_time_severity_type_source_and_message() -> None:
    notification = EventNotification(
        sequence=1,
        event_type="RefreshStartEventType",
        event_type_id="i=2787",
        source_node="i=2253",
        source_name="Server",
        severity=1,
        message="Pump stopped",
        time="2026-11-21T10:00:00.000Z",
    )

    line = cli_events._event_line(notification)

    assert "2026-11-21T10:00:00.000Z" in line
    assert "RefreshStartEventType" in line
    assert "Server" in line
    assert "Pump stopped" in line


def test_event_line_falls_back_to_the_received_time_and_source_node() -> None:
    notification = EventNotification(sequence=1, source_node="ns=2;s=Condition")

    line = cli_events._event_line(notification)

    assert "ns=2;s=Condition" in line
    assert notification.received_at.isoformat()[:10] in line
