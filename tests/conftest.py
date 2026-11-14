"""Shared fixtures."""

from __future__ import annotations

import pytest

from opcua_explorer.models import (
    AddressSpace,
    ConnectionInfo,
    Edge,
    NamespaceEntry,
    NodeRecord,
    ValueSnapshot,
)


@pytest.fixture
def sample_snapshot() -> AddressSpace:
    """A small hand-built snapshot used by exporter and model tests."""
    connection = ConnectionInfo(
        endpoint_url="opc.tcp://demo:4840",
        security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#None",
        security_policy_label="None",
        security_mode="None",
        auth_mode="anonymous",
        namespaces=[NamespaceEntry(0, "http://opcfoundation.org/UA/"), NamespaceEntry(2, "urn:demo")],
        server_application_name="Demo Server",
    )

    plant = NodeRecord(
        node_id="ns=2;s=Plant",
        node_class="Object",
        browse_name="Plant",
        browse_name_namespace_index=2,
        display_name="Plant",
        namespace_uri="urn:demo",
        type_definition="i=61",
        type_definition_name="FolderType",
    )
    temperature = NodeRecord(
        node_id="ns=2;s=Temperature",
        node_class="Variable",
        browse_name="Temperature",
        browse_name_namespace_index=2,
        display_name="Temperature",
        namespace_uri="urn:demo",
        description='Line temperature, in "°C", sampled',
        data_type="i=11",
        data_type_name="Double",
        value_rank=-1,
        array_dimensions=[],
        access_level=3,
        access_level_names=["CurrentRead", "CurrentWrite"],
        user_access_level=3,
        user_access_level_names=["CurrentRead", "CurrentWrite"],
        type_definition="i=63",
        type_definition_name="BaseDataVariableType",
        value=ValueSnapshot(value=21.5, variant_type="Double", status="Good"),
    )
    denied = NodeRecord(node_id="ns=2;s=Denied", node_class="Variable", display_name="Denied")
    denied.unavailable["Value"] = "BadUserAccessDenied"

    snapshot = AddressSpace(
        endpoint_url="opc.tcp://demo:4840",
        roots=["i=85"],
        nodes={record.node_id: record for record in (plant, temperature, denied)},
        references=[
            Edge("i=85", "ns=2;s=Plant", "i=35", "Organizes"),
            Edge("ns=2;s=Plant", "ns=2;s=Temperature", "i=47", "HasComponent"),
            Edge("ns=2;s=Plant", "ns=2;s=Missing", "i=47", "HasComponent"),
        ],
        namespaces=connection.namespaces,
        connection=connection,
        values_included=False,
        inaccessible={"ns=2;s=Locked": "BadUserAccessDenied"},
        limitations=["Test limitation note."],
    )
    snapshot.finished_at = snapshot.started_at
    return snapshot
