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
    acknowledge_condition_confirmation,
    condition_refresh_text,
    connection_diagnostics_text,
    guarded_operation_confirmation,
    method_call_confirmation,
    write_value_confirmation,
)


def test_acknowledge_confirmation_names_the_target_event_and_limits() -> None:
    text = acknowledge_condition_confirmation(
        endpoint_url="opc.tcp://plant:4840",
        condition_node_id="ns=2;s=PumpCondition",
        event_id="0102ff",
        comment="checked on site",
    )

    assert "Endpoint: opc.tcp://plant:4840" in text
    assert "CALL Acknowledge on ns=2;s=PumpCondition" in text
    assert "EventId: 0102ff" in text
    assert "Comment: checked on site" in text
    assert "does not write a variable" in text
    assert "never retried" in text
    assert EQUIPMENT_WARNING in text


def test_acknowledge_confirmation_renders_an_empty_comment() -> None:
    text = acknowledge_condition_confirmation(
        endpoint_url="opc.tcp://x", condition_node_id="i=1", event_id="00", comment=""
    )
    assert "Comment: (empty)" in text


def test_condition_refresh_text_states_what_it_does_and_does_not_do() -> None:
    text = condition_refresh_text(subscription_id=78, object_node_id="i=2782")

    assert "ConditionRefresh on subscription 78" in text
    assert "i=2782" in text
    assert "already retains" in text
    assert "Nothing is written, acknowledged, confirmed or reset" in text
    assert "sent once without a retry" in text
    assert "RefreshStart/RefreshEnd" in text


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


def test_write_confirmation_states_target_type_and_restore() -> None:
    text = write_value_confirmation(
        endpoint_url="opc.tcp://demo:4840",
        node_id="ns=2;s=Temperature",
        value_preview="42.5",
        data_type_label="Double",
        value_rank=-1,
        restore_previous=True,
        previous_value="21.5",
    )

    assert "Endpoint: opc.tcp://demo:4840" in text
    assert "WRITE  ns=2;s=Temperature  \u2190  42.5" in text
    assert "Type: Double, ValueRank -1" in text
    assert "Current value: 21.5" in text
    assert "Restore afterwards: yes, one additional verified write" in text
    assert EQUIPMENT_WARNING in text


def test_write_confirmation_without_restore_or_previous_value() -> None:
    text = write_value_confirmation(
        endpoint_url="opc.tcp://demo:4840",
        node_id="i=1",
        value_preview="1",
        data_type_label="Int32",
        value_rank=-1,
        restore_previous=False,
    )

    assert "Restore afterwards: no" in text
    assert "Current value:" not in text


def test_method_call_confirmation_renders_the_argument_list() -> None:
    text = method_call_confirmation(
        endpoint_url="opc.tcp://demo:4840",
        object_node_id="ns=2;s=Motor",
        method_node_id="ns=2;s=Start",
        arguments=["900", "true"],
    )

    assert "CALL  ns=2;s=Start on ns=2;s=Motor" in text
    assert "Arguments (2): 900, true" in text
    assert "never retried" in text
    assert EQUIPMENT_WARNING in text


def test_method_call_confirmation_without_arguments() -> None:
    text = method_call_confirmation(
        endpoint_url="opc.tcp://demo:4840",
        object_node_id="i=1",
        method_node_id="i=2",
        arguments=[],
        executable_status="Good",
    )

    assert "Arguments (0): no arguments" in text
    assert "Executable: Good" in text
