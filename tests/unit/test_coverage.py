"""The Export tab's snapshot coverage text (download vs. export completeness)."""

from __future__ import annotations

from opcua_explorer.models import AddressSpace, NodeRecord, TraversalConfig


def _snapshot(**config) -> AddressSpace:
    snapshot = AddressSpace(
        endpoint_url="opc.tcp://demo:4840",
        nodes={"ns=2;s=Temperature": NodeRecord(node_id="ns=2;s=Temperature", node_class="Variable")},
        config=TraversalConfig(**config),
    )
    snapshot.finished_at = snapshot.started_at
    return snapshot


def test_coverage_reports_disabled_options_as_not_included():
    text = _snapshot(include_values=False, include_description=False).coverage_text()

    assert "Included:       base attributes and references" in text
    not_included = text.split("NOT included:", 1)[1].splitlines()[0]
    assert "variable values (Value, SourceTimestamp, ServerTimestamp)" in not_included
    assert "node descriptions" in not_included
    assert "Details panel only:" in text
    assert "device attributes" in not_included
    assert "EventNotifier" in text and "RolePermissions" in text


def test_coverage_lists_values_and_descriptions_when_enabled():
    text = _snapshot(include_values=True, include_description=True).coverage_text()

    assert "variable values (Value, SourceTimestamp, ServerTimestamp)" in text
    assert "node descriptions" in text
    # Device attributes stay opt-in, so they are still listed as not included.
    assert "NOT included:" in text
    assert "device attributes" in text.split("NOT included:", 1)[1].splitlines()[0]

    with_device = _snapshot(include_values=True, include_description=True, include_device_attributes=True).coverage_text()
    assert "NOT included:" not in with_device
    assert "device attributes" in with_device


def test_coverage_reports_a_partial_snapshot():
    snapshot = _snapshot()
    snapshot.limits_hit = ["max_nodes"]
    snapshot.inaccessible = {"ns=2;s=Locked": "BadUserAccessDenied"}
    denied = NodeRecord(node_id="ns=2;s=Denied")
    denied.unavailable["Value"] = "BadUserAccessDenied"
    snapshot.nodes["ns=2;s=Denied"] = denied
    snapshot.unsupported_encodings = [{"node_id": "ns=2;s=Temperature", "kind": "binary"}]
    snapshot.cancelled = True

    text = snapshot.coverage_text()

    assert "Limits hit:     max_nodes (partial snapshot)" in text
    assert "Inaccessible:   1 node(s) could not be browsed" in text
    assert "Refused attrs:  1 attribute read(s) returned a bad StatusCode" in text
    assert "Unsupported:    1 value encoding(s) flagged as approximate" in text
    assert "State:          CANCELLED (partial snapshot)" in text
