"""End-to-end behaviour of the client service against the real demo server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

from opcua_explorer import __version__
from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.errors import ConnectionFailedError, NotConnectedError
from opcua_explorer.exporters import export_csv, export_json
from opcua_explorer.models import TraversalConfig
from opcua_explorer.opcua.client_service import OpcUaService


async def test_discovery_lists_advertised_endpoints(demo_model) -> None:
    service = OpcUaService()
    try:
        discovery = await service.discover(demo_model.endpoint)
    finally:
        await service.disconnect()

    assert discovery.endpoint_url == demo_model.endpoint
    assert discovery.endpoints, "the demo server must advertise at least one endpoint"
    endpoint = discovery.endpoints[0]
    assert endpoint.security_policy_label == "None"
    assert endpoint.security_mode == "None"
    assert "Anonymous" in endpoint.token_types
    assert discovery.server_application["application_name"] == "OPC UA Explorer Demo Server"
    assert discovery.server_application["application_uri"] == "urn:opcua-explorer:demo:server"


async def test_connect_reports_diagnostics(service, demo_model) -> None:
    info = service.connection
    assert info is not None
    assert info.endpoint_url == demo_model.endpoint
    assert info.security_policy_label == "None"
    assert info.auth_mode == "anonymous"
    assert info.manufacturer_name == "opcua-explorer"
    assert info.software_version == __version__
    uris = [entry.uri for entry in info.namespaces]
    assert "urn:opcua-explorer:demo" in uris
    assert "urn:opcua-explorer:demo:extra" in uris
    assert await service.connection_state() == "connected"


async def test_browse_children_and_node_details(service, demo_model) -> None:
    children = await service.browse_children("i=85")
    by_name = {child.display_name: child for child in children}
    assert "Plant" in by_name
    plant = by_name["Plant"]
    assert plant.node_class == "Object"
    assert plant.node_id == demo_model.node_ids["plant"]
    assert plant.browse_name_namespace_index == 2  # qualified browse name from the demo model

    details = await service.read_node_details(demo_model.node_ids["temperature"])
    record = details.record
    assert record.node_class == "Variable"
    assert record.browse_name == "Temperature"
    assert record.namespace_uri == "urn:opcua-explorer:demo"
    assert record.data_type == "i=11"
    assert record.data_type_name == "Double"
    assert record.value_rank == -1
    assert "CurrentRead" in record.access_level_names
    assert "CurrentWrite" in record.access_level_names  # set_writable in the demo model
    assert record.type_definition_name == "BaseDataVariableType"

    attribute_names = [attribute.attribute for attribute in details.attributes]
    assert attribute_names[0] == "NodeId"
    assert "NamespaceUri" in attribute_names
    assert "DataTypeName" in attribute_names
    assert "TypeDefinition" in attribute_names

    directions = {(reference.direction, reference.reference_type_name) for reference in details.references}
    assert ("Forward", "HasTypeDefinition") in directions
    assert ("Inverse", "HasComponent") in directions


async def test_read_value_returns_status_and_timestamps(service, demo_model) -> None:
    snapshot = await service.read_value(demo_model.node_ids["temperature"])
    assert snapshot.status == "Good"
    assert snapshot.variant_type == "Double"
    assert snapshot.value == pytest.approx(21.5)
    assert snapshot.source_timestamp is not None
    assert snapshot.server_timestamp is not None
    assert snapshot.encoding == "json"


async def test_download_reports_limits_and_respects_node_budget(service) -> None:
    config = TraversalConfig(max_nodes=60, concurrency=2, batch_size=8)
    snapshot = await service.download(["i=85"], config)

    assert 0 < len(snapshot.nodes) <= 60
    assert "max_nodes" in snapshot.limits_hit
    assert snapshot.summary_text().count("Limits hit") == 1
    assert snapshot.namespaces, "the namespace URI table must always travel with the snapshot"
    assert snapshot.values_included is False
    assert all(record.value is None for record in snapshot.nodes.values())
    assert snapshot.limitations  # never presented as a complete information model


async def test_download_depth_limit_stops_descent(service, demo_model) -> None:
    snapshot = await service.download(["i=85"], TraversalConfig(max_depth=1, max_nodes=None, batch_size=20))

    # Children of the root are included, but their children are not.
    assert demo_model.node_ids["plant"] in snapshot.nodes
    assert demo_model.node_ids["line1"] not in snapshot.nodes
    assert "max_depth" in snapshot.limits_hit


async def test_download_preserves_graph_structure(service, demo_model) -> None:
    snapshot = await service.download(["i=85"], TraversalConfig(max_nodes=600, concurrency=4, batch_size=25))

    edge_keys = [(edge.source, edge.target, edge.reference_type) for edge in snapshot.references]
    assert len(edge_keys) == len(set(edge_keys)), "references must be de-duplicated"

    # The cycle Motor -> Plant is recorded as a forward edge.
    assert any(
        edge.source == demo_model.node_ids["motor"] and edge.target == demo_model.node_ids["plant"]
        for edge in snapshot.references
    )
    # SharedConfig is reachable from more than one parent.
    shared_sources = {
        edge.source for edge in snapshot.references if edge.target == demo_model.node_ids["shared_config"]
    }
    assert len(shared_sources) >= 2

    # Names are resolved for reference types, so the export is readable.
    organizes = [edge for edge in snapshot.references if edge.reference_type == "i=35"]
    assert organizes and organizes[0].reference_type_name == "Organizes"


async def test_download_with_values_encodes_supported_types(service, demo_model) -> None:
    snapshot = await service.download(
        ["i=85"],
        TraversalConfig(max_nodes=600, concurrency=4, batch_size=25, include_values=True),
    )

    nodes = snapshot.nodes
    assert snapshot.values_included is True

    blob = nodes[demo_model.node_ids["blob"]].value
    assert blob is not None
    assert blob.value["$type"] == "ByteString"
    assert blob.value["base64"] == "AAEC/w=="

    operating_range = nodes[demo_model.node_ids["operating_range"]].value
    assert operating_range is not None
    assert operating_range.variant_type == "ExtensionObject"
    assert operating_range.value["Low"] == 0.0
    assert operating_range.value["High"] == 100.0

    samples = nodes[demo_model.node_ids["samples"]].value
    assert samples.value == [1.0, 2.5, 3.75]

    timestamp = nodes[demo_model.node_ids["timestamp"]].value
    assert timestamp.value["$type"] == "DateTime"
    assert timestamp.value["iso"].endswith("Z")

    assert snapshot.unsupported_encodings == []


async def test_namespace_filter_excludes_other_namespaces(service, demo_model) -> None:
    demo_index = demo_model.namespace_index
    extra_index = demo_model.extra_namespace_index
    snapshot = await service.download(
        ["i=85"],
        TraversalConfig(max_nodes=600, namespace_filter=frozenset({demo_index})),
    )

    assert demo_model.node_ids["plant"] in snapshot.nodes
    assert demo_model.node_ids["extra_value"] not in snapshot.nodes
    # Namespace 0 stays reachable so the standard structure is intact.
    assert "i=85" in snapshot.nodes
    indexes = {int(node_id.split("=")[1].split(";")[0]) for node_id in snapshot.nodes if node_id.startswith("ns=")}
    assert extra_index not in indexes


async def test_export_files_from_download(service, tmp_path) -> None:
    snapshot = await service.download(["i=85"], TraversalConfig(max_nodes=150, batch_size=20))

    json_path = tmp_path / "space.json"
    csv_path = tmp_path / "space.csv"
    json_result = export_json(snapshot, json_path)
    csv_result = export_csv(snapshot, csv_path)

    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["source"]["endpoint_url"] == snapshot.endpoint_url
    assert len(document["nodes"]) == json_result.nodes == len(snapshot.nodes)
    assert len(document["references"]) == json_result.references
    assert document["namespaces"]
    assert all(node["value"] is None for node in document["nodes"].values())

    header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("node_id,node_class,browse_name")
    assert csv_result.rows == len(snapshot.nodes)
    assert csv_path.stat().st_size > 0


async def test_username_authentication(demo_model, tmp_path) -> None:
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    try:
        info = await service.connect(
            ConnectionSettings(
                endpoint_url=demo_model.endpoint,
                auth_mode="username",
                username="demo",
                password="demo-pass",
            )
        )
        assert info.auth_mode == "username"
        assert service.is_connected
        await service.disconnect()

        with pytest.raises(ConnectionFailedError):
            await service.connect(
                ConnectionSettings(
                    endpoint_url=demo_model.endpoint,
                    auth_mode="username",
                    username="demo",
                    password="definitely-wrong",
                )
            )
        assert not service.is_connected
    finally:
        await service.shutdown()


async def test_operations_require_a_session(demo_model) -> None:
    service = OpcUaService()
    with pytest.raises(NotConnectedError):
        await service.browse_children("i=85")
    with pytest.raises(NotConnectedError):
        await service.read_node_details("i=85")
    with pytest.raises(NotConnectedError):
        await service.download(["i=85"], TraversalConfig(max_nodes=5))


async def test_cancel_download_returns_partial_snapshot(service) -> None:
    task = asyncio.create_task(
        service.download(["i=84"], TraversalConfig(max_nodes=5000, concurrency=2, batch_size=5))
    )
    await asyncio.sleep(0.15)
    task.cancel()
    snapshot = await task

    assert snapshot.cancelled is True
    assert snapshot.finished_at is not None
    assert any("cancelled" in note.lower() for note in snapshot.limitations)

    # The session must survive a cancelled browse (continuation points released).
    children = await service.browse_children("i=85")
    assert children, "the session must still be usable after cancelling a download"


async def test_automatic_reconnect_keeps_endpoint_security_and_identity(tmp_path) -> None:
    """A dropped link is retried with the same endpoint, policy and credentials."""
    model = await start_demo_server(update_values=False)
    port = int(model.endpoint.rsplit(":", 1)[1])
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))

    async def wait_for_state(wanted: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        state = await service.connection_state()
        while state != wanted and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            state = await service.connection_state()
        return state

    try:
        info = await service.connect(
            ConnectionSettings(
                endpoint_url=model.endpoint,
                auth_mode="username",
                username="demo",
                password="demo-pass",
                auto_reconnect=True,
                request_timeout=5.0,
            )
        )
        assert info.auth_mode == "username"
        assert await wait_for_state("connected", 5.0) == "connected"

        # Take the server away: the supervisor must retry without releasing the session.
        await stop_demo_server(model)
        assert await wait_for_state("reconnecting", 20.0) == "reconnecting"
        assert service.is_connected is True

        # Bring it back on the same endpoint; recovery happens without a new connect().
        for attempt in range(20):
            try:
                model = await start_demo_server(port=port, update_values=False)
                break
            except OSError:
                if attempt == 19:
                    raise
                await asyncio.sleep(0.25)
        assert await wait_for_state("connected", 40.0) == "connected"

        # Nothing about the connection configuration changed during the recovery.
        restored = await service.connection_snapshot()
        assert restored is not None
        assert restored.endpoint_url == info.endpoint_url
        assert restored.security_policy_uri == info.security_policy_uri
        assert restored.security_mode == info.security_mode
        assert restored.auth_mode == "username"

        # The re-created session is usable, including the namespace table.
        assert await service.browse_children("i=85")
        namespaces = await service.refresh_namespaces()
        assert any(entry.uri == "urn:opcua-explorer:demo" for entry in namespaces)
        value = await service.read_value(model.node_ids["temperature"])
        assert value.status == "Good"
    finally:
        await service.shutdown()
        with contextlib.suppress(Exception):
            await stop_demo_server(model)
