"""Guarded write and method-call checks.

These are the only checks that can change a server. They are opt-in through
``TestConfig.allow_writes``, operate on exactly one configured target each and
never discover, scan or repeat an operation. Validation and read-back rules are
described in ``docs/architecture.md`` §11.

The functions take the client and config explicitly so they have no dependency
on :class:`~opcua_explorer.opcua.test_runner.FunctionalTestRunner` internals.
"""

from __future__ import annotations

import asyncio
from typing import Any

from asyncua import ua

from ..models import (
    AUTHORITY_OPERATOR_DIALOG,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    MethodCallTarget,
    TestCheck,
    TestConfig,
    WriteAuthority,
    WriteTarget,
)
from . import write_service
from .check_support import CheckSkipped, mark_failed, mark_passed, preview_value
from .ua_names import node_class_name, status_name
from .value_parsing import describe_data_type


def require_writes_allowed(config: TestConfig, check: TestCheck) -> WriteAuthority:
    """Refuse unless writes are enabled; return the authority the operator dialog grants."""
    if not config.allow_writes:
        raise CheckSkipped(
            "Writes and method calls are disabled. Enable 'allow guarded write / method-call tests' and "
            "confirm the target before running."
        )
    check.data["confirmed_by_operator"] = True
    authorization = WriteAuthority(AUTHORITY_OPERATOR_DIALOG, check.id)
    check.data["authorization"] = authorization.describe()
    return authorization


async def _restore_previous_value(
    prepared: Any,
    outcome: Any,
    check: TestCheck,
    target: WriteTarget,
    authorization: WriteAuthority,
) -> tuple[str | None, bool]:
    """Restore exactly what was read before the write, only when the operator asked for it."""
    restored: str | None = None
    restore_failed = False
    if target.restore_previous_value:
        if not outcome.previous_known:
            restored = "not restored: the previous value could not be read"
        else:
            try:
                restore = await write_service.restore_value(
                    prepared, outcome.previous_raw, authorization=authorization
                )
            except asyncio.CancelledError:
                raise
            if restore.status == STATUS_PASSED:
                restored = f"restored to {restore.written_value}"
                check.add_status("Restore", "Good", node_id=prepared.node_id)
            elif restore.status == STATUS_OUTCOME_UNKNOWN:
                restored = f"restore effect unknown: {restore.detail}"
                restore_failed = True
                check.add_status("Restore", "unknown", node_id=prepared.node_id)
            else:
                restored = f"restore failed: {restore.detail}"
                restore_failed = True
                check.add_status("Restore", restore.read_back_status_code or "unknown", node_id=prepared.node_id)
    elif outcome.previous_known:
        restored = "not restored (restore was not requested)"
    check.data["restore"] = restored
    return restored, restore_failed


async def check_write_target(
    client: Any,
    config: TestConfig,
    check: TestCheck,
    *,
    target: WriteTarget,
) -> None:
    """Write one value to one node, read it back and optionally restore the old value."""
    node_id = target.node_id
    check.data.update(
        {
            "node_id": node_id,
            "requested_value": target.value_text,
            "restore_previous_value": target.restore_previous_value,
        }
    )
    authorization = require_writes_allowed(config, check)

    node = client.get_node(node_id)
    try:
        node_class = await node.read_node_class()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise CheckSkipped(f"{node_id} could not be read: {exc}") from exc
    check.data["node_class"] = node_class_name(node_class)
    if node_class != ua.NodeClass.Variable:
        raise CheckSkipped(f"{node_id} is a {node_class_name(node_class)}, not a Variable; nothing was written.")

    # The one write path of the application: server type -> parse -> prepared write.
    try:
        prepared = await write_service.prepare_write(client, node_id, target.value_text)
    except write_service.PrepareError as exc:
        raise CheckSkipped(str(exc)) from exc
    check.data["data_type"] = prepared.data_type.to_string()
    check.data["data_type_name"] = describe_data_type(prepared.data_type)
    check.data["value_rank"] = prepared.value_rank
    if prepared.value_type is not None and prepared.value_type.kind != "builtin":
        check.data["value_type"] = prepared.value_type.label()

    access_status = "unknown"
    try:
        access_result = await node.read_attribute(ua.AttributeIds.UserAccessLevel)
        access_status = status_name(access_result.StatusCode)
        if access_result.StatusCode.is_good():
            check.data["user_access_level"] = int(access_result.Value.Value)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - optional metadata
        pass
    check.add_status("Read UserAccessLevel", access_status, node_id=node_id)
    check.data["parsed_value"] = preview_value(prepared.parsed)

    outcome = await write_service.send_prepared_write(prepared, authorization=authorization)
    check.add_status("Read previous value", outcome.previous_status_code or "unreadable", node_id=node_id)
    check.data["previous_value"] = outcome.previous_value if outcome.previous_known else "unreadable"
    check.data["write_ms"] = outcome.duration_ms
    if outcome.write_status_code:
        check.add_status("Write", outcome.write_status_code, node_id=node_id)
    if outcome.read_back_value:
        check.add_status("Read back", outcome.read_back_status_code or "unreadable", node_id=node_id)
        check.data["read_back"] = outcome.read_back_value

    # Nothing was written (or the write was rejected): restoring would be an unnecessary write.
    if not outcome.sent or (outcome.write_status_code and outcome.write_status_code != "Good"):
        mark_failed(check, outcome.detail)
        return

    restored, restore_failed = await _restore_previous_value(prepared, outcome, check, target, authorization)

    if outcome.status != STATUS_PASSED:
        mark_failed(check, outcome.detail)
        return
    if restore_failed:
        mark_failed(check, f"The value was written and verified, but {restored}.")
        return
    detail = f"Wrote {check.data['parsed_value']} to {node_id} ({describe_data_type(prepared.data_type)}) and read it back."
    if restored:
        detail += f" {restored.capitalize()}."
    mark_passed(check, detail)


async def check_method_call(
    client: Any,
    config: TestConfig,
    check: TestCheck,
    *,
    call: MethodCallTarget,
) -> None:
    """Call one method on one object node with validated arguments."""
    check.data.update(
        {
            "object_node_id": call.object_node_id,
            "method_node_id": call.method_node_id,
            "argument_texts": list(call.arguments),
        }
    )
    authorization = require_writes_allowed(config, check)

    # The one method-call path of the application: InputArguments -> parse -> Executable -> one call.
    try:
        prepared = await write_service.prepare_call(
            client, call.object_node_id, call.method_node_id, list(call.arguments)
        )
    except write_service.PrepareError as exc:
        raise CheckSkipped(str(exc)) from exc
    check.data["signature"] = prepared.signature
    check.data["parsed_arguments"] = list(prepared.previews)
    check.add_status("Read Executable", prepared.executable_status, node_id=call.method_node_id)

    outcome = await write_service.send_prepared_call(prepared, authorization=authorization)
    check.add_status("Call", outcome.status_code or "Good", node_id=call.method_node_id)
    check.data["duration_ms"] = outcome.duration_ms
    check.data["output"] = outcome.output or "no output arguments"
    if outcome.status != STATUS_PASSED:
        mark_failed(check, outcome.detail)
        return

    argument_text = ", ".join(prepared.previews) if prepared.previews else "no arguments"
    mark_passed(check, f"Called {call.method_node_id} on {call.object_node_id} with {argument_text}.")
