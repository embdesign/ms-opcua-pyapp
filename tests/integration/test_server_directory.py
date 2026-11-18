"""FindServers integration tests against the demo server."""

from __future__ import annotations

import pytest
import pytest_asyncio

from opcua_explorer.config import TrustStore
from opcua_explorer.errors import ConnectionFailedError
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import free_port


@pytest_asyncio.fixture
async def directory_service(tmp_path):
    """A service with no session: FindServers needs no session at all."""
    instance = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    try:
        yield instance
    finally:
        await instance.shutdown()


async def test_the_demo_server_lists_itself(demo_model, directory_service) -> None:
    service = directory_service

    result = await service.discover_servers(demo_model.endpoint)

    assert service.is_connected is False  # no session was created
    assert result.discovery_url == demo_model.endpoint
    applications = {server.application_uri: server for server in result.servers}
    our_uri = "urn:opcua-explorer:demo:server"
    assert our_uri in applications
    server = applications[our_uri]
    assert server.application_name == "OPC UA Explorer Demo Server"
    assert server.application_type == "ClientAndServer"
    assert server.discovery_urls == [demo_model.endpoint]
    assert "server(s) reported" in result.summary_text()


async def test_the_uri_filter_narrows_and_can_match_nothing(demo_model, directory_service) -> None:
    service = directory_service

    matching = await service.discover_servers(demo_model.endpoint, server_uris=["urn:opcua-explorer"])
    assert [server.application_uri for server in matching.servers] == ["urn:opcua-explorer:demo:server"]

    empty = await service.discover_servers(demo_model.endpoint, server_uris=["urn:does-not-exist"])
    assert empty.servers == []
    assert "knows no application matching" in empty.summary_text()


async def test_a_closed_port_is_reported_as_a_connection_error(directory_service) -> None:
    port = free_port()  # nothing listens there

    with pytest.raises(ConnectionFailedError):
        await directory_service.discover_servers(
            f"opc.tcp://127.0.0.1:{port}", request_timeout=2.0, connect_timeout=5.0
        )
