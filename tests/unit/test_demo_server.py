"""Unit tests for the demo-server command surface (no server started)."""

from __future__ import annotations

from opcua_explorer.demo.demo_server import build_parser


def test_parser_defaults_keep_values_changing():
    args = build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.no_updates is False
    assert args.no_username is False


def test_no_updates_is_available_for_deterministic_sequences():
    args = build_parser().parse_args(["--no-updates", "--port", "48999"])

    assert args.no_updates is True
    assert args.port == 48999
