"""Guarded write and method-call checks (phase 4).

These are the only checks that can change a server. They are opt-in through
``TestConfig.allow_writes``, operate on exactly one configured target each and
never discover, scan or repeat an operation. Validation and read-back rules are
described in ``docs/architecture.md`` §11.

The functions take the client and config explicitly so they have no dependency
on :class:`~opcua_explorer.opcua.test_runner.FunctionalTestRunner` internals.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from asyncua import ua

from ..models import MethodCallTarget, TestCheck, TestConfig, WriteTarget
from .check_support import CheckSkipped, mark_failed, mark_passed, preview_value
from .ua_names import node_class_name, status_name
from .value_parsing import ValueParseError, describe_data_type, parse_value_text


def require_writes_allowed(config: TestConfig, check: TestCheck) -> None:
    if not config.allow_writes:
        raise CheckSkipped(
            "Writes and method calls are disabled. Enable 'allow guarded write / method-call tests' and "
            "confirm the target before running."
        )
    check.data["confirmed_by_operator"] = True


async def _data_type_and_rank(client: Any, node_id: str) -> tuple[ua.NodeId, int]:
    node = client.get_node(node_id)
    try:
        data_type = await node.read_data_type()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise CheckSkipped(f"DataType of {node_id} could not be read: {exc}") from exc
    try:
        rank_result = await node.read_attribute(ua.AttributeIds.ValueRank)
        value_rank = int(rank_result.Value.Value)
    except asyncio.CancelledError:
        raise
    except Exception:
        value_rank = 0
    return data_type, value_rank


async def _read_arguments(client: Any, method_id: str) -> list[Any]:
    """Read ``InputArguments`` of a method as ``ua.Argument``-like objects.

    asyncua 2.1.0 returns ``uaprotocol_auto.Argument`` for a decoded value while
    ``ua.Argument`` is the hand-written class, so this checks the fields instead
    of the class identity.
    """
    method = client.get_node(method_id)
    try:
        argument_property = await method.get_child(["InputArguments"])
    except asyncio.CancelledError:
        raise
    except Exception:
        return []
    try:
        value = await argument_property.read_value()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise CheckSkipped(f"InputArguments of {method_id} could not be read: {exc}") from exc
    if not value:
        return []
    return [
        argument
        for argument in value
        if all(hasattr(argument, field) for field in ("Name", "DataType", "ValueRank"))
    ]


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
    require_writes_allowed(config, check)

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

    data_type, value_rank = await _data_type_and_rank(client, node_id)
    check.data["data_type"] = data_type.to_string()
    check.data["data_type_name"] = describe_data_type(data_type)
    check.data["value_rank"] = value_rank

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

    try:
        parsed = parse_value_text(target.value_text, data_type, value_rank)
    except ValueParseError as exc:
        raise CheckSkipped(f"The value was not written: {exc}") from exc
    check.data["parsed_value"] = preview_value(parsed)

    previous: Any = None
    previous_known = False
    try:
        previous_value = await node.read_data_value()
        previous = previous_value.Value.Value
        previous_known = previous_value.StatusCode.is_good()
        check.add_status("Read previous value", status_name(previous_value.StatusCode), node_id=node_id)
        check.data["previous_value"] = preview_value(previous) if previous_known else "unreadable"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - servers without read rights on Value
        check.data["previous_value"] = f"unreadable: {type(exc).__name__}"

    variant_type = ua.datatype_to_varianttype(data_type)
    write_started = time.perf_counter()
    try:
        await node.write_value(ua.DataValue(ua.Variant(parsed, variant_type)))
    except asyncio.CancelledError:
        raise
    except ua.UaStatusCodeError as exc:
        status = status_name(exc.code)
        check.add_status("Write", status, node_id=node_id)
        mark_failed(check, f"Writing to {node_id} returned {status}.")
        return
    except Exception as exc:
        mark_failed(check, f"Writing to {node_id} failed: {type(exc).__name__}: {exc}")
        return
    check.add_status("Write", "Good", node_id=node_id)
    check.data["write_ms"] = round((time.perf_counter() - write_started) * 1000.0, 2)

    read_back = await node.read_data_value()
    read_back_status = status_name(read_back.StatusCode)
    check.add_status("Read back", read_back_status, node_id=node_id)
    check.data["read_back"] = preview_value(read_back.Value.Value) if read_back.StatusCode.is_good() else "unreadable"
    matches = read_back.StatusCode.is_good() and preview_value(read_back.Value.Value) == preview_value(parsed)

    restored: str | None = None
    restore_failed = False
    if target.restore_previous_value:
        if not previous_known:
            restored = "not restored: the previous value could not be read"
        else:
            try:
                await node.write_value(ua.DataValue(ua.Variant(previous, variant_type)))
                verify = await node.read_data_value()
                if verify.StatusCode.is_good() and preview_value(verify.Value.Value) == preview_value(previous):
                    restored = f"restored to {preview_value(previous)}"
                    check.add_status("Restore", "Good", node_id=node_id)
                else:
                    restored = "restore could not be verified"
                    restore_failed = True
                    check.add_status("Restore", status_name(verify.StatusCode), node_id=node_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                restored = f"restore failed: {type(exc).__name__}: {exc}"
                restore_failed = True
    elif previous_known:
        restored = "not restored (restore was not requested)"
    check.data["restore"] = restored

    if read_back_status != "Good":
        mark_failed(check, f"The value was written but reading it back returned {read_back_status}.")
        return
    if not matches:
        mark_failed(
            check,
            f"Read-back {check.data['read_back']} differs from the written value {check.data['parsed_value']}.",
        )
        return
    if restore_failed:
        mark_failed(check, f"The value was written and verified, but {restored}.")
        return
    detail = f"Wrote {check.data['parsed_value']} to {node_id} ({describe_data_type(data_type)}) and read it back."
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
    require_writes_allowed(config, check)

    method = client.get_node(call.method_node_id)
    arguments = await _read_arguments(client, call.method_node_id)
    signature = [
        {
            "name": argument.Name,
            "data_type": argument.DataType.to_string(),
            "data_type_name": describe_data_type(argument.DataType),
            "value_rank": int(argument.ValueRank),
        }
        for argument in arguments
    ]
    check.data["signature"] = signature

    if len(call.arguments) != len(arguments):
        raise CheckSkipped(
            f"The method expects {len(arguments)} argument(s) "
            f"({', '.join(item['name'] or item['data_type_name'] for item in signature) or 'none'}) "
            f"but {len(call.arguments)} were configured; the method was not called."
        )

    parsed_arguments: list[Any] = []
    for index, (text, argument) in enumerate(zip(call.arguments, arguments, strict=True), start=1):
        try:
            parsed_arguments.append(parse_value_text(text, argument.DataType, int(argument.ValueRank)))
        except ValueParseError as exc:
            raise CheckSkipped(
                f"Argument {index} ({argument.Name or argument.DataType.to_string()}): {exc}. "
                "The method was not called."
            ) from exc
    check.data["parsed_arguments"] = [preview_value(value) for value in parsed_arguments]

    try:
        executable = await method.read_attribute(ua.AttributeIds.Executable)
        check.add_status("Read Executable", status_name(executable.StatusCode), node_id=call.method_node_id)
        if executable.StatusCode.is_good() and executable.Value.Value is False:
            raise CheckSkipped(f"{call.method_node_id} reports Executable = false; the method was not called.")
    except asyncio.CancelledError:
        raise
    except CheckSkipped:
        raise
    except Exception:  # pragma: no cover - not every server exposes Executable
        check.add_status("Read Executable", "unavailable", node_id=call.method_node_id)

    object_node = client.get_node(call.object_node_id)
    started = time.perf_counter()
    try:
        result = await object_node.call_method(method, *parsed_arguments)
    except asyncio.CancelledError:
        raise
    except ua.UaStatusCodeError as exc:
        status = status_name(exc.code)
        check.add_status("Call", status, node_id=call.method_node_id)
        mark_failed(check, f"Calling {call.method_node_id} on {call.object_node_id} returned {status}.")
        return
    except Exception as exc:
        mark_failed(check, f"Calling {call.method_node_id} failed: {type(exc).__name__}: {exc}")
        return
    check.add_status("Call", "Good", node_id=call.method_node_id)
    check.data["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    check.data["output"] = preview_value(result) if result is not None else "no output arguments"

    argument_text = ", ".join(check.data["parsed_arguments"]) if parsed_arguments else "no arguments"
    mark_passed(check, f"Called {call.method_node_id} on {call.object_node_id} with {argument_text}.")
