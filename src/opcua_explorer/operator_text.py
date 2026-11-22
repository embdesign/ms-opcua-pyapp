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

from .models import (
    CONDITION_ACTION_METHODS,
    ConnectionInfo,
    DiscoveryResult,
    TestConfig,
    condition_action_label,
)

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


def node_create_confirmation(
    *,
    endpoint_url: str,
    node_class: str,
    browse_name: str,
    parent_node_id: str,
    display_name: str = "",
    data_type: str = "",
    value_text: str = "",
) -> str:
    """Confirmation body for one AddNodes request from the Write & call tab."""
    lines = [
        f"Endpoint: {endpoint_url}",
        "",
        "Exactly one node will be created, with a NodeId the server assigns:",
        f"  CREATE  {node_class} {browse_name!r} below {parent_node_id}",
    ]
    if display_name:
        lines.append(f"  DisplayName: {display_name!r}")
    if node_class == "Variable" and data_type:
        lines.append(f"  DataType: {data_type}, initial value {value_text!r}")
    lines.extend(
        [
            "",
            "This changes the server's address space, not a process value. Servers usually allow node management "
            "for administrators only, so a refusal is reported with its StatusCode. One node is created — no "
            "children, no references and no batch — and it is read back afterwards. Nothing is retried.",
            "",
            EQUIPMENT_WARNING,
        ]
    )
    return "\n".join(lines)


def node_delete_confirmation(*, endpoint_url: str, node_id: str) -> str:
    """Confirmation body for one DeleteNodes request from the Write & call tab."""
    return "\n".join(
        [
            f"Endpoint: {endpoint_url}",
            "",
            "Exactly one node will be deleted:",
            f"  DELETE  {node_id}",
            "",
            "This removes the node from the server's address space. Deleting a node that still has children or "
            "that a running process depends on can break the server or the process; the client does not check "
            "for children first and does not offer an undo. References to the node are deleted with it, one "
            "request only, never retried.",
            "",
            EQUIPMENT_WARNING,
        ]
    )


#: What each condition method does to the condition, in the operator's words. One
#: definition, because the GUI dialog, the CLI note and the report line all say it.
_CONDITION_ACTION_CONSEQUENCES = {
    "acknowledge": (
        "This acknowledges one condition event. It does not write a variable, does not confirm or "
        "re-enable the condition and is never retried; the server decides whether the acknowledgement "
        "is accepted."
    ),
    "confirm": (
        "This confirms one condition event. Confirming is not an acknowledgement and does not clear the "
        "condition; a server refuses Confirm for an event that was not acknowledged first. The call is "
        "never retried."
    ),
    "enable": (
        "This re-enables a disabled condition, so the server may report its events for it again. It "
        "acknowledges and confirms nothing, and the call is never retried."
    ),
    "disable": (
        "This disables the condition: the server stops reporting its events, and the condition itself "
        "still exists. Re-enabling it is a separate Enable call. The call is never retried."
    ),
    "comment": (
        "This adds one comment to the condition. It changes no condition state - nothing is "
        "acknowledged, confirmed, enabled or disabled - and the call is never retried."
    ),
}

#: What a completed call proves, per action. Used by the Events tab report line and
#: by ``opcua-events`` so a success is never described as more than it is.
_CONDITION_ACTION_RESULTS = {
    "acknowledge": "no Confirm/Enable call was made and the acknowledgement is not proof the condition cleared",
    "confirm": "confirming is not proof the condition cleared",
    "enable": "the server decides whether the condition reports its events again",
    "disable": "the condition still exists and reports no events until it is enabled again",
    "comment": "a comment changes no condition state",
}


def condition_action_confirmation(
    *,
    action: str,
    endpoint_url: str,
    condition_node_id: str,
    event_id: str,
    comment: str,
) -> str:
    """Confirmation body for one condition method call (Acknowledge/Confirm/Enable/Disable/AddComment)."""
    method = CONDITION_ACTION_METHODS.get(str(action), str(action))
    consequence = _CONDITION_ACTION_CONSEQUENCES.get(
        str(action), "This calls one condition method once and is never retried."
    )
    return "\n".join(
        [
            f"Endpoint: {endpoint_url}",
            "",
            "One method call will be sent exactly once:",
            f"  CALL {method} on {condition_node_id}",
            f"  EventId: {event_id}",
            f"  Comment: {comment or '(empty)'}",
            "",
            consequence,
            "",
            EQUIPMENT_WARNING,
        ]
    )


def condition_action_result_text(
    *, action: str, condition_node_id: str, event_id: str
) -> str:
    """One report line describing what a completed condition method call did and did not do."""
    label = condition_action_label(action)
    result = _CONDITION_ACTION_RESULTS.get(str(action), "the server decides the effect")
    return (
        f"{label} on {condition_node_id} was sent once (EventId {event_id}); {result}."
    )


def condition_refresh_text(*, subscription_id: int | None, object_node_id: str) -> str:
    """What a ``ConditionRefresh`` call does, for the Events tab and its log line.

    No dialog uses this text: the call stores nothing and interrupts nothing, so
    the operator only needs to know what it asks the server to do.
    """
    return (
        f"ConditionRefresh on subscription {subscription_id} (method call on {object_node_id}): the server "
        "is asked to publish the Conditions it already retains for this subscription again. Nothing is "
        "written, acknowledged, confirmed or reset, no condition is cleared and the call is sent once "
        "without a retry. The server's RefreshStart/RefreshEnd events are the only evidence it arrived."
    )


def client_certificate_confirmation(
    *,
    mode: str,
    common_name: str,
    application_uri: str,
    validity_days: int,
    key_size: int,
    files: list[str],
) -> str:
    """Confirmation body for creating one local client certificate or signing request."""
    is_request = mode == "csr"
    lines = [
        "These files will be written on this computer:",
        *[f"  {path}" for path in files],
        "",
        f"Subject: CN={common_name}",
        f"Application URI (subjectAltName): {application_uri}",
        f"Key: RSA {key_size} bit, written unencrypted as PEM with file mode 600",
    ]
    if is_request:
        lines.append("Validity: decided by the CA that issues from this request, not by this client.")
    else:
        lines.append(f"Validity: {validity_days} day(s) from today (self-signed, no CA).")
    lines.extend(
        [
            "",
            "Nothing is sent to a server, nothing is installed and no certificate is trusted: this only "
            "creates local files. The private key is unencrypted, so treat it like a password.",
        ]
    )
    if is_request:
        lines.append(
            "Send the .csr file to the CA; this client cannot issue a certificate and does not "
            "pretend the request is one."
        )
    else:
        lines.append(
            "A self-signed certificate is only accepted by servers whose administrators trust it, so hand "
            "the certificate (or its SHA-256) to the server's operator."
        )
    return "\n".join(lines)


def client_certificate_warning(
    *,
    endpoint_url: str,
    subject: str,
    not_after: str | None,
    problems: list[str],
) -> str:
    """Confirmation body shown when the selected client certificate has local problems.

    Nothing is changed by connecting: the operator is only told what was found on
    this computer before the server refuses the certificate.
    """
    lines = [f"Endpoint: {endpoint_url}", "", f"Client certificate: {subject or '(unparsed)'}"]
    if not_after:
        lines.append(f"Valid until: {not_after}")
    lines.append("")
    lines.extend(f"- {problem}" for problem in problems)
    lines.extend(
        [
            "",
            "These are local findings; the server has not been contacted yet. Continue anyway? The "
            "certificate is sent unchanged, nothing is trusted or replaced, and the server's own answer "
            "(usually a Bad StatusCode) is reported if it refuses it.",
        ]
    )
    return "\n".join(lines)


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
