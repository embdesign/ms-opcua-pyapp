"""Unit tests for the FindServers directory mapping (no network)."""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_explorer.config import TrustStore
from opcua_explorer.errors import ConnectionFailedError
from opcua_explorer.gui.tabs.server_directory_dialog import split_uris
from opcua_explorer.models import DiscoveredServer, ServerDiscoveryResult
from opcua_explorer.opcua.client_service import OpcUaService


def _description(
    uri: str,
    name: str | None = "Demo",
    *,
    app_type: int = ua.ApplicationType.Server.value,
    urls: list[str] | None = None,
) -> ua.ApplicationDescription:
    description = ua.ApplicationDescription()
    description.ApplicationUri = uri
    description.ProductUri = "urn:product"
    description.ApplicationName = ua.LocalizedText(Text=name) if name is not None else ua.LocalizedText()
    description.ApplicationType = ua.ApplicationType(app_type)
    description.DiscoveryUrls = urls if urls is not None else ["opc.tcp://h:4840"]
    description.GatewayServerUri = None
    description.DiscoveryProfileUri = None
    return description


def test_split_uris_accepts_the_usual_separators() -> None:
    assert split_uris("urn:a, urn:b;urn:c\nurn:d\t urn:e") == ["urn:a", "urn:b", "urn:c", "urn:d", "urn:e"]
    assert split_uris("   ") == []


def test_discovered_server_label_falls_back_to_the_uri() -> None:
    assert DiscoveredServer(application_uri="urn:x", application_name="Name").label() == "Name"
    assert DiscoveredServer(application_uri="urn:x").label() == "urn:x"


def test_result_summary_distinguishes_empty_from_filtered() -> None:
    plain = ServerDiscoveryResult(discovery_url="opc.tcp://h:4840")
    assert "empty server list" in plain.summary_text()

    filtered = ServerDiscoveryResult(discovery_url="opc.tcp://h:4840", server_uris=["urn:none"])
    assert "knows no application matching urn:none" in filtered.summary_text()

    found = ServerDiscoveryResult(
        discovery_url="opc.tcp://h:4840",
        servers=[DiscoveredServer(application_uri="urn:x")],
    )
    assert found.summary_text() == "1 server(s) reported by opc.tcp://h:4840."


def test_result_document_is_json_safe() -> None:
    result = ServerDiscoveryResult(
        discovery_url="opc.tcp://h:4840",
        server_uris=["urn:x"],
        servers=[
            DiscoveredServer(
                application_uri="urn:x",
                application_name="X",
                application_type="Server",
                discovery_urls=["opc.tcp://h:4840"],
            )
        ],
        duration_ms=12.3456,
    )

    document = result.to_dict()

    assert document["discovery_url"] == "opc.tcp://h:4840"
    assert document["server_uris"] == ["urn:x"]
    assert document["duration_ms"] == 12.346
    assert document["servers"][0]["discovery_urls"] == ["opc.tcp://h:4840"]
    assert document["received_at"].endswith("Z")


async def test_discover_servers_maps_and_sorts_the_directory_answer(tmp_path, monkeypatch) -> None:
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    seen: dict[str, object] = {}

    async def fake_fetch(client, uris):
        seen["uris"] = list(uris)
        seen["url"] = client.server_url.geturl()
        return [
            _description("urn:b", "Zulu", app_type=ua.ApplicationType.Client.value, urls=[]),
            _description("urn:a", "alpha", urls=["opc.tcp://h:4840", ""]),
        ]

    monkeypatch.setattr(service, "_fetch_find_servers", fake_fetch)

    result = await service.discover_servers("127.0.0.1:4840", server_uris=[" urn:pre ", ""])

    assert seen["uris"] == ["urn:pre"]  # blank filters are dropped
    assert seen["url"] == "opc.tcp://127.0.0.1:4840"  # scheme is added
    assert result.discovery_url == "opc.tcp://127.0.0.1:4840"
    assert result.server_uris == ["urn:pre"]
    assert [server.application_name for server in result.servers] == ["alpha", "Zulu"]
    first, second = result.servers
    assert first.application_uri == "urn:a"
    assert first.application_type == "Server"
    assert first.discovery_urls == ["opc.tcp://h:4840"]  # empty URLs are dropped
    assert second.application_type == "Client"
    assert second.discovery_urls == []
    assert result.duration_ms >= 0.0


async def test_a_refusing_endpoint_becomes_a_connection_error(tmp_path, monkeypatch) -> None:
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))

    async def failing(client, uris):
        raise ua.UaError("BadServiceUnsupported")

    monkeypatch.setattr(service, "_fetch_find_servers", failing)

    with pytest.raises(ConnectionFailedError) as excinfo:
        await service.discover_servers("opc.tcp://h:4840")
    assert "server directory" in str(excinfo.value)
    assert "connect to it directly" in str(excinfo.value)


async def test_a_blank_url_is_refused_before_any_request(tmp_path) -> None:
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))

    with pytest.raises(ConnectionFailedError):
        await service.discover_servers("   ")
