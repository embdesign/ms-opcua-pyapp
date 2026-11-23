"""Unit tests for the headless alarm summary (no network)."""

from __future__ import annotations

import json

from opcua_explorer import cli_conditions
from opcua_explorer.models import CONDITION_TYPE_NODE_ID, ConditionState, ConditionStateSnapshot


def _snapshot() -> ConditionStateSnapshot:
    return ConditionStateSnapshot(
        conditions=[
            ConditionState(
                node_id="ns=2;s=PumpCondition",
                condition_name="PumpCondition",
                acked_state="Acknowledged",
                acked_id=True,
                confirmed_state="Unconfirmed",
                confirmed_id=False,
                enabled_state="Enabled",
                enabled_id=True,
                missing=["Retain"],
                last_severity=700,
            )
        ]
    )


def test_parser_defaults_to_condition_type_and_a_bounded_window() -> None:
    args = cli_conditions.build_parser().parse_args(["--endpoint", "opc.tcp://x:4840"])

    assert args.condition is None
    assert args.event_type == CONDITION_TYPE_NODE_ID
    assert args.seconds == 5.0
    assert args.json is False


def test_text_output_names_every_state_and_what_is_not_exposed(capsys) -> None:
    cli_conditions._print_text(_snapshot())
    text = capsys.readouterr().out

    assert "ns=2;s=PumpCondition" in text
    assert "Acknowledged" in text
    assert "Unconfirmed" in text
    assert "enabled=Enabled" in text
    assert "retain=not exposed" in text
    assert "not exposed: Retain" in text


def test_json_output_is_versioned(capsys) -> None:
    cli_conditions._print_json(_snapshot())
    payload = json.loads(capsys.readouterr().out)

    assert payload["format"] == "opcua-alarm-summary"
    assert payload["version"] == 1
    assert payload["snapshot"]["conditions"][0]["acked_id"] is True
    assert payload["snapshot"]["conditions"][0]["retain"] is None
