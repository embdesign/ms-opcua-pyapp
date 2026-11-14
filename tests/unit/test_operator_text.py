"""Unit tests for operator-facing safety and diagnostics text (no Qt)."""

from __future__ import annotations

from opcua_explorer.models import (
    ConnectionInfo,
    DiscoveryResult,
    EndpointInfo,
    MethodCallTarget,
    NamespaceEntry,
    TestConfig,
    WriteTarget,
)
from opcua_explorer.operator_text import (
    EQUIPMENT_WARNING,
    connection_diagnostics_text,
    guarded_operation_confirmation,
)


def test_guarded_confirmation_lists_exact_operations_and_warning() -> None:
    config = TestConfig(
        write_targets=[WriteTarget(node_id="ns=2;s=Temperature", value_text="21.5", restore_previous_value=True)],
        method_calls=[
            MethodCallTarget(object_node_id="ns=2;s=Motor", method_node_id="ns=2;s=Start", arguments=["1450"])
        ],
    )

    text = guarded_operation_confirmation(endpoint_url="opc.tcp://plant:4840", config=config)

    assert "Endpoint: opc.tcp://plant:4840" in text
    assert "WRITE  ns=2;s=Temperature  ←  21.5" in text
    assert "restored afterwards because you asked for it" in text
    assert "CALL   ns=2;s=Start on ns=2;s=Motor with (1450)" in text
    assert EQUIPMENT_WARNING in text


def test_guarded_confirmation_states_when_nothing_is_restored() -> None:
    config = TestConfig(write_targets=[WriteTarget(node_id="ns=2;s=Speed", value_text="900")])
    text = guarded_operation_confirmation(endpoint_url="opc.tcp://x", config=config)
    assert "previous value: not restored" in text


def test_diagnostics_show_discovery_then_not_connected_hint() -> None:
    discovery = DiscoveryResult(
        endpoint_url="opc.tcp://demo:4840",
        endpoints=[
            EndpointInfo(
                endpoint_url="opc.tcp://demo:4840",
                security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#None",
                security_mode="None",
                security_policy_label="None",
                token_types=["Anonymous"],
            )
        ],
        server_application={"application_name": "Demo", "application_uri": "urn:demo"},
    )

    text = connection_diagnostics_text(discovery=discovery)

    assert "Endpoint URL      : opc.tcp://demo:4840" in text
    assert "Advertised        : 1 endpoint(s)" in text
    assert "Not connected. Discover an endpoint" in text


def test_diagnostics_show_session_details_and_namespaces() -> None:
    info = ConnectionInfo(
        endpoint_url="opc.tcp://demo:4840",
        security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#None",
        security_policy_label="None",
        security_mode="None",
        auth_mode="anonymous",
        namespaces=[NamespaceEntry(0, "http://opcfoundation.org/UA/"), NamespaceEntry(2, "urn:demo")],
        session_timeout_ms=600_000,
        server_certificate_sha256="ab" * 32,
    )

    text = connection_diagnostics_text(info=info)

    assert "Security policy   : None" in text
    assert "Session timeout   : 600000 ms" in text
    assert "Namespaces (2):" in text
    assert "[2] urn:demo" in text
    assert "Not connected" not in text


def test_diagnostics_without_arguments_is_the_hint() -> None:
    assert connection_diagnostics_text() == "Not connected. Discover an endpoint to see the server's advertisement."
