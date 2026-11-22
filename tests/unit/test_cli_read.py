"""Unit tests for the headless read CLI (no network)."""

from __future__ import annotations

import json

from opcua_explorer import cli_read
from opcua_explorer.models import AttributeResult, NodeDetails, NodeRecord, ValueSnapshot

ENDPOINT = "opc.tcp://127.0.0.1:4840"


class _FakeService:
    async def read_value(self, node_id: str) -> ValueSnapshot:
        return ValueSnapshot(value=42.5, variant_type="Double")

    async def read_node_details(self, node_id: str) -> NodeDetails:
        return NodeDetails(
            record=NodeRecord(node_id=node_id, node_class="Variable"),
            attributes=[
                AttributeResult(attribute="NodeClass", value="Variable"),
                AttributeResult(attribute="DisplayName", value="Temperature"),
            ],
        )


def _args(*extra: str):
    return cli_read.build_parser().parse_args(["--endpoint", ENDPOINT, "--node", "i=85", *extra])


def test_defaults_read_the_value() -> None:
    args = _args()
    assert args.attribute == "Value"
    assert args.node == ["i=85"]
    assert args.json is False


async def test_read_value_returns_the_snapshot() -> None:
    read = await cli_read._read_one(_FakeService(), "i=85", "Value")
    assert read["node_id"] == "i=85"
    assert read["status"] == "Good"
    assert read["value"] == 42.5


async def test_read_attribute_uses_the_node_details() -> None:
    read = await cli_read._read_one(_FakeService(), "i=85", "DisplayName")
    assert read["value"] == "Temperature"
    assert read["status"] == "Good"


async def test_missing_attribute_is_reported_as_bad() -> None:
    read = await cli_read._read_one(_FakeService(), "i=85", "Description")
    assert read["status"] == "BadAttributeIdInvalid"


def test_json_output_is_versioned(capsys) -> None:
    cli_read._print_reads(
        [{"node_id": "i=85", "attribute": "Value", "status": "Good", "value": 1, "detail": {}}],
        as_json=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "opcua-read"
    assert payload["version"] == 1
    assert payload["reads"][0]["value"] == 1
