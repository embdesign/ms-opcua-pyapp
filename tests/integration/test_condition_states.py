"""Condition-state reads against a small in-process A&C-shaped server."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator

import pytest_asyncio
from asyncua import Server, ua

from opcua_explorer import cli_conditions
from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.opcua.client_service import OpcUaService

NAMESPACE = "urn:opcua-explorer:test:conditions"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest_asyncio.fixture
async def condition_server(tmp_path) -> AsyncIterator[tuple[OpcUaService, str]]:
    """A server with one condition object exposing three states and no Retain."""
    server = Server()
    await server.init()
    port = _free_port()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    index = await server.register_namespace(NAMESPACE)

    def node_id(name: str) -> ua.NodeId:
        return ua.NodeId(name, index)

    objects = server.get_objects_node()
    condition = await objects.add_object(node_id("PumpCondition"), ua.QualifiedName("PumpCondition", index))
    await condition.add_property(node_id("ConditionName"), ua.QualifiedName("ConditionName"), "PumpCondition")
    await condition.add_variable(
        node_id("LastSeverity"), ua.QualifiedName("LastSeverity"), 700, varianttype=ua.VariantType.UInt16
    )

    async def state(name: str, text: str, value: bool) -> None:
        variable = await condition.add_variable(
            node_id(name), ua.QualifiedName(name), ua.LocalizedText(text),
            varianttype=ua.VariantType.LocalizedText,
        )
        await variable.add_property(
            node_id(f"{name}.Id"), ua.QualifiedName("Id"), value, varianttype=ua.VariantType.Boolean
        )

    await state("AckedState", "Acknowledged", True)
    await state("ConfirmedState", "Unconfirmed", False)
    await state("EnabledState", "Enabled", True)

    await server.start()
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(ConnectionSettings(endpoint_url=server.endpoint.geturl()))
    try:
        yield service, condition.nodeid.to_string()
    finally:
        await service.shutdown()
        await server.stop()


async def test_state_variables_are_read_from_the_condition_object(condition_server) -> None:
    service, node_id = condition_server

    snapshot = await service.read_condition_states([node_id])

    assert snapshot.conditions
    state = snapshot.conditions[0]
    assert state.node_id == node_id
    assert state.condition_name == "PumpCondition"
    assert state.acked_text() == "Acknowledged"
    assert state.acked_id is True
    assert state.confirmed_text() == "Unconfirmed"
    assert state.confirmed_id is False
    assert state.enabled_text() == "Enabled"
    assert state.enabled_id is True
    assert state.last_severity == 700
    # This condition exposes no Retain variable; that is reported, never guessed.
    assert state.retain_text() == "not exposed"
    assert state.missing == ["Retain"]


async def test_an_unknown_condition_is_reported_without_failing_the_read(condition_server) -> None:
    service, node_id = condition_server

    snapshot = await service.read_condition_states([node_id, "ns=4;s=NoSuchCondition"])

    assert len(snapshot.conditions) == 2
    assert snapshot.conditions[0].acked_id is True
    assert snapshot.conditions[1].status != "Good"
    assert "Browsing the condition node failed" in snapshot.conditions[1].detail


async def test_cli_conditions_prints_the_alarm_summary(condition_server, tmp_path, capsys) -> None:
    _service, node_id = condition_server

    code = await asyncio.to_thread(
        cli_conditions.main,
        [
            "--endpoint",
            _service.endpoint_url,
            "--trust-store",
            str(tmp_path / "trust.json"),
            "--condition",
            node_id,
            "--json",
            "--quiet",
        ],
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "opcua-alarm-summary"
    condition = payload["snapshot"]["conditions"][0]
    assert condition["acked_id"] is True
    assert condition["retain"] is None
