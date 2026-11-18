"""Operator-facing text that is safety-relevant or purely presentational.

These strings used to live inside widgets, where they could only be exercised by
a full headless GUI run. Keeping them here (no Qt import) makes them testable
and ensures the connection tab and the tests tab render the same facts the same
way.

The guarded-operation confirmation is part of the safety model: it must show the
endpoint, every target and the physical-equipment warning before anything is
sent. ``docs/architecture.md`` §11 describes the rules it implements.
"""

from __future__ import annotations

from .models import ConnectionInfo, DiscoveryResult, TestConfig

#: Shown before any write or method call, and repeated in the confirmation dialog.
EQUIPMENT_WARNING = (
    "A write or a method call can change a running process. If this server is connected to physical "
    "equipment, the change happens immediately and may move machinery. Only continue if you own the "
    "target, know what the value or call does, and are allowed to change it."
)

_NOT_CONNECTED_HINT = "Not connected. Discover an endpoint to see the server's advertisement."


def guarded_operation_confirmation(*, endpoint_url: str, config: TestConfig) -> str:
    """Confirmation body listing the exact guarded operations that will be sent."""
    lines = [
        f"Endpoint: {endpoint_url}",
        "",
        "The following operations will be sent once, in this order:",
        "",
    ]
    for target in config.write_targets:
        lines.append(f"WRITE  {target.node_id}  ←  {target.value_text}")
        lines.append(
            "       previous value: "
            + ("restored afterwards because you asked for it" if target.restore_previous_value else "not restored")
        )
    for call in config.method_calls:
        arguments = ", ".join(call.arguments) if call.arguments else "no arguments"
        lines.append(f"CALL   {call.method_node_id} on {call.object_node_id} with ({arguments})")
    lines.extend(["", EQUIPMENT_WARNING, "", "These operations are not part of a read-only check run."])
    return "\n".join(lines)


def write_value_confirmation(
    *,
    endpoint_url: str,
    node_id: str,
    value_preview: str,
    data_type_label: str,
    value_rank: int,
    restore_previous: bool,
    previous_value: str = "",
) -> str:
    """Confirmation body for one interactive variable write from the Write & call tab."""
    lines = [
        f"Endpoint: {endpoint_url}",
        "",
        "Exactly one write will be sent, then read back and compared:",
        f"  WRITE  {node_id}  \u2190  {value_preview}",
        f"  Type: {data_type_label}, ValueRank {value_rank}",
    ]
    if previous_value:
        lines.append(f"  Current value: {previous_value}")
    lines.extend(
        [
            "  Restore afterwards: " + ("yes, one additional verified write" if restore_previous else "no"),
            "",
            "A refused write, a differing read-back, or an unreadable previous value is reported as such. "
            "Nothing is retried and no value is restored unless you asked for it.",
            "",
            EQUIPMENT_WARNING,
        ]
    )
    return "\n".join(lines)


def method_call_confirmation(    *,
    endpoint_url: str,
    object_node_id: str,
    method_node_id: str,
    arguments: list[str],
    executable_status: str = "",
) -> str:
    """Confirmation body for one interactive method call from the Write & call tab."""
    rendered = ", ".join(arguments) if arguments else "no arguments"
    lines = [
        f"Endpoint: {endpoint_url}",
        "",
        "Exactly one method call will be sent:",
        f"  CALL  {method_node_id} on {object_node_id}",
        f"  Arguments ({len(arguments)}): {rendered}",
    ]
    if executable_status:
        lines.append(f"  Executable: {executable_status}")
    lines.extend(
        [
            "",
            "The arguments were parsed with the server's InputArguments. The call is never retried, and its "
            "output arguments are reported as the server returned them.",
            "",
            EQUIPMENT_WARNING,
        ]
    )
    return "\n".join(lines)


def attribute_write_confirmation(
    *,
    endpoint_url: str,
    node_id: str,
    attribute_name: str,
    text: str,
    previous_text: str = "",
) -> str:
    """Confirmation body for one DisplayName/Description write from the Write & call tab."""
    lines = [
        f"Endpoint: {endpoint_url}",
        "",
        "Exactly one attribute write will be sent, then read back and compared:",
        f"  WRITE  {attribute_name} of {node_id}",
        f"  new text: {text!r}",
    ]
    if previous_text:
        lines.append(f"  current text: {previous_text!r}")
    lines.extend(
        [
            "",
            "This changes the node's metadata (its name or description), not its process value. Servers "
            "usually allow only privileged users to write non-Value attributes, so a refusal is reported "
            "with its StatusCode. Nothing is retried.",
            "",
            EQUIPMENT_WARNING,
        ]
    )
    return "\n".join(lines)


def acknowledge_condition_confirmation(
    *, endpoint_url: str, condition_node_id: str, event_id: str, comment: str
) -> str:
    """Confirmation body for one condition acknowledgement (Acknowledge method)."""
    return "\n".join(
        [
            f"Endpoint: {endpoint_url}",
            "",
            "One method call will be sent exactly once:",
            f"  CALL Acknowledge on {condition_node_id}",
            f"  EventId: {event_id}",
            f"  Comment: {comment or '(empty)'}",
            "",
            "This acknowledges one condition event. It does not write a variable, does not confirm the "
            "condition and is never retried; the server decides whether the acknowledgement is accepted.",
            "",
            EQUIPMENT_WARNING,
        ]
    )


def connection_diagnostics_text(
    *,
    discovery: DiscoveryResult | None = None,
    info: ConnectionInfo | None = None,
) -> str:
    """Multi-line diagnostics shown on the Connection tab.

    With neither argument the text is the "not connected" hint. A discovery
    result is shown even before a session exists; when a session exists it is
    rendered instead (the tab calls this with one of them).
    """
    lines: list[str] = []
    if discovery is not None:
        app = discovery.server_application
        lines.append(f"Endpoint URL      : {discovery.endpoint_url}")
        lines.append(f"Server            : {app.get('application_name') or 'unknown'}")
        lines.append(f"Application URI   : {app.get('application_uri') or 'unknown'}")
        lines.append(f"Product URI       : {app.get('product_uri') or 'unknown'}")
        lines.append(f"Application type  : {app.get('application_type') or 'unknown'}")
        lines.append(f"Advertised        : {len(discovery.endpoints)} endpoint(s)")
        for endpoint in discovery.endpoints:
            lines.append(
                f"  - {endpoint.label():<38} level={endpoint.security_level:<3} "
                f"tokens={','.join(endpoint.token_types) or '-'}"
            )
        lines.append("")
    if info is not None:
        lines.append(f"Security policy   : {info.security_policy_label} ({info.security_policy_uri})")
        lines.append(f"Security mode     : {info.security_mode}")
        lines.append(f"Identity          : {info.auth_mode}")
        lines.append(f"Session timeout   : {info.session_timeout_ms} ms")
        lines.append(f"Client app URI    : {info.application_uri}")
        lines.append(f"Manufacturer      : {info.manufacturer_name or 'unknown'}")
        lines.append(f"Software version  : {info.software_version or 'unknown'} (build {info.build_number or '?'})")
        if info.server_certificate_sha256:
            lines.append(f"Server cert SHA256: {info.server_certificate_sha256}")
        lines.append("")
        lines.append(f"Namespaces ({len(info.namespaces)}):")
        for entry in info.namespaces:
            lines.append(f"  [{entry.index}] {entry.uri}")
    else:
        lines.append(_NOT_CONNECTED_HINT)
    return "\n".join(lines)
