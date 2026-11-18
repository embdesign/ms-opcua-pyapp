"""One place that performs a single variable write, a single method call or a single restore.

The guarded test checks and the universal sequence runner share these primitives so that a
write is always: server DataType -> parse -> **one** write -> read-back compare, a call is
always: ``InputArguments`` -> parse -> ``Executable`` -> **one** call, and a restore is one more
write of the value that was read before, verified the same way.

Every send states its authority (``models.WriteAuthority``): a caller cannot send without saying
whether an operator dialog or an operator-written sequence document asked for it.

Nothing here discovers targets, scans, batches, retries or restores on its own. A cancelled
write or call reports whether it had already been sent, so a caller can never present an unknown
effect as a plain failure.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any

from asyncua import ua

from ..models import (
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    WriteAuthority,
)
from . import type_support
from .check_support import preview_value
from .method_support import MetadataUnavailable, read_executable, read_input_arguments, read_node_data_type
from .ua_names import node_class_name, status_name
from .value_parsing import ValueParseError, describe_data_type


class PrepareError(ValueError):
    """The operation cannot be performed safely; nothing was sent."""


class WriteCancelled(asyncio.CancelledError):
    """A cancellation while a write was in flight; ``sent`` tells whether the request left."""

    def __init__(self, *, sent: bool) -> None:
        super().__init__("write cancelled")
        self.sent = sent


class CallCancelled(asyncio.CancelledError):
    """A cancellation while a method call was in flight."""

    def __init__(self, *, sent: bool) -> None:
        super().__init__("call cancelled")
        self.sent = sent


@dataclass(slots=True)
class PreparedWrite:
    """A validated write: the node, its server type and the parsed value."""

    node: Any
    node_id: str
    data_type: ua.NodeId
    value_rank: int
    variant_type: ua.VariantType
    parsed: Any
    value_type: type_support.ValueType | None = None


@dataclass(slots=True)
class WriteOutcome:
    """Result of one write, including the read-back that proves it."""

    node_id: str
    status: str = STATUS_FAILED
    detail: str = ""
    previous_value: str = ""
    previous_status_code: str = ""
    previous_raw: Any = None
    previous_known: bool = False
    new_value: str = ""
    read_back_value: str = ""
    read_back_status_code: str = ""
    write_status_code: str = ""
    sent: bool = False
    duration_ms: float = 0.0
    authorization: WriteAuthority | None = None


@dataclass(slots=True)
class RestoreOutcome:
    """Result of one restore write: the value that was written back and its read-back."""

    node_id: str
    status: str = STATUS_FAILED
    detail: str = ""
    written_value: str = ""
    read_back_value: str = ""
    read_back_status_code: str = ""
    sent: bool = False
    duration_ms: float = 0.0
    authorization: WriteAuthority | None = None


@dataclass(slots=True)
class WriteResult:
    """One operator-requested write: the write itself plus an optional explicit restore.

    The restore is a separate, equally single and verified write (``restore_value``);
    it never happens unless the caller asked for it.
    """

    write: WriteOutcome
    restore: RestoreOutcome | None = None

    @property
    def status(self) -> str:
        if self.restore is None or self.write.status != STATUS_PASSED:
            return self.write.status
        return self.restore.status

    def summary_text(self) -> str:
        parts = [self.write.detail or self.write.status]
        if self.restore is not None:
            parts.append(self.restore.detail or self.restore.status)
        return " ".join(part for part in parts if part)


#: The only non-Value attributes this module writes (text metadata, not process data).
TEXT_ATTRIBUTES: dict[str, ua.AttributeIds] = {
    "DisplayName": ua.AttributeIds.DisplayName,
    "Description": ua.AttributeIds.Description,
}

#: Case-insensitive lookup, keeping the canonical OPC UA attribute name.
_TEXT_ATTRIBUTES_BY_LOWER = {name.lower(): (name, attribute_id) for name, attribute_id in TEXT_ATTRIBUTES.items()}


@dataclass(slots=True)
class PreparedAttributeWrite:
    """A validated DisplayName/Description write for one node."""

    node: Any
    node_id: str
    attribute_name: str
    attribute_id: ua.AttributeIds
    text: str
    locale: str | None = None
    previous_text: str = ""


@dataclass(slots=True)
class AttributeWriteOutcome:
    """Result of one attribute write, including the read-back that proves it."""

    node_id: str
    attribute_name: str
    status: str = STATUS_FAILED
    detail: str = ""
    text: str = ""
    previous_text: str = ""
    write_status_code: str = ""
    read_back_text: str = ""
    read_back_status_code: str = ""
    sent: bool = False
    duration_ms: float = 0.0
    authorization: WriteAuthority | None = None


def _localized_text(data_value: ua.DataValue) -> str:
    """Plain text of a LocalizedText DataValue (empty when it carries none)."""
    variant = data_value.Value
    raw = variant.Value if isinstance(variant, ua.Variant) else variant
    if isinstance(raw, ua.LocalizedText):
        return raw.Text or ""
    return "" if raw is None else str(raw)


async def prepare_attribute_write(
    client: Any, node_id: str, attribute_name: str, text: str
) -> PreparedAttributeWrite:
    """Validate one DisplayName/Description write without sending it."""
    name = (attribute_name or "").strip()
    entry = _TEXT_ATTRIBUTES_BY_LOWER.get(name.lower())
    if entry is None:
        allowed = ", ".join(TEXT_ATTRIBUTES)
        raise PrepareError(f"{attribute_name!r} is not a writable text attribute here ({allowed}).")
    canonical_name, attribute_id = entry
    if not (text or "").strip():
        raise PrepareError("The attribute text is empty; nothing was written.")
    node = client.get_node(node_id)
    try:
        await node.read_node_class()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise PrepareError(f"{node_id} could not be read: {type(exc).__name__}: {exc}") from exc
    previous_text = "unreadable"
    try:
        previous = await node.read_attribute(attribute_id, raise_on_bad_status=False)
        if previous.StatusCode is None or previous.StatusCode.is_good():
            previous_text = _localized_text(previous)
    except asyncio.CancelledError:
        raise
    except Exception:
        previous_text = "unreadable"
    return PreparedAttributeWrite(
        node=node,
        node_id=node_id,
        attribute_name=canonical_name,
        attribute_id=attribute_id,
        text=text.strip(),
        previous_text=previous_text,
    )


async def send_prepared_attribute_write(
    prepared: PreparedAttributeWrite, *, authorization: WriteAuthority
) -> AttributeWriteOutcome:
    """Send exactly one attribute write, then read the attribute back."""
    outcome = AttributeWriteOutcome(
        node_id=prepared.node_id,
        attribute_name=prepared.attribute_name,
        text=prepared.text,
        previous_text=prepared.previous_text,
        authorization=authorization,
    )
    started = time.perf_counter()
    localized = ua.LocalizedText(Text=prepared.text, Locale=prepared.locale)
    data_value = ua.DataValue(ua.Variant(localized, ua.VariantType.LocalizedText))
    try:
        await prepared.node.write_attribute(prepared.attribute_id, data_value)
    except asyncio.CancelledError:
        raise WriteCancelled(sent=True) from None
    except ua.UaStatusCodeError as exc:
        outcome.sent = True
        outcome.write_status_code = status_name(exc.code)
        outcome.detail = (
            f"Writing {prepared.attribute_name} of {prepared.node_id} returned "
            f"{outcome.write_status_code}."
        )
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    except Exception as exc:
        outcome.sent = True
        outcome.detail = (
            f"Writing {prepared.attribute_name} of {prepared.node_id} failed: "
            f"{type(exc).__name__}: {exc}"
        )
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    outcome.sent = True
    outcome.write_status_code = "Good"

    try:
        read_back = await prepared.node.read_attribute(prepared.attribute_id, raise_on_bad_status=False)
    except asyncio.CancelledError:
        raise WriteCancelled(sent=True) from None
    except Exception as exc:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            f"The {prepared.attribute_name} was written but could not be read back: "
            f"{type(exc).__name__}: {exc}. The effect on the server is unknown."
        )
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    good = read_back.StatusCode is None or read_back.StatusCode.is_good()
    outcome.read_back_status_code = status_name(read_back.StatusCode) if read_back.StatusCode else "Good"
    outcome.read_back_text = _localized_text(read_back) if good else "unreadable"
    if not good:
        outcome.status = STATUS_FAILED
        outcome.detail = (
            f"{prepared.attribute_name} was written but reading it back returned "
            f"{outcome.read_back_status_code}."
        )
    elif outcome.read_back_text != prepared.text:
        outcome.status = STATUS_FAILED
        outcome.detail = (
            f"Read-back {outcome.read_back_text!r} differs from the written "
            f"{prepared.attribute_name} {prepared.text!r}."
        )
    else:
        outcome.status = STATUS_PASSED
        outcome.detail = (
            f"Wrote {prepared.attribute_name} {prepared.text!r} to {prepared.node_id} and read it back."
        )
    outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return outcome


async def write_attribute_value(
    client: Any,
    node_id: str,
    attribute_name: str,
    text: str,
    *,
    authorization: WriteAuthority,
) -> AttributeWriteOutcome:
    """Prepare and send one DisplayName/Description write; exactly once."""
    try:
        prepared = await prepare_attribute_write(client, node_id, attribute_name, text)
    except PrepareError as exc:
        return AttributeWriteOutcome(
            node_id=node_id,
            attribute_name=attribute_name,
            status=STATUS_SKIPPED,
            detail=str(exc),
            authorization=authorization,
        )
    return await send_prepared_attribute_write(prepared, authorization=authorization)



@dataclass(slots=True)
class PreparedCall:
    """A validated method call, including the signature read from ``InputArguments``."""

    object_node: Any
    method_node: Any
    object_node_id: str
    method_node_id: str
    parsed: list[Any] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)
    signature: list[dict[str, Any]] = field(default_factory=list)
    executable_status: str = "unavailable"


@dataclass(slots=True)
class CallOutcome:
    """Result of one method call: StatusCode plus the parsed argument list and output."""

    object_node_id: str
    method_node_id: str
    status: str = STATUS_FAILED
    detail: str = ""
    arguments: list[str] = field(default_factory=list)
    output: str = ""
    #: Raw output values (asyncua returns a list of Variants).
    output_values: list[Any] = field(default_factory=list)
    status_code: str = ""
    sent: bool = False
    duration_ms: float = 0.0
    authorization: WriteAuthority | None = None


@dataclass(slots=True)
class Inspection:
    """What a dry run reports: everything is resolved and typed, nothing is sent."""

    ok: bool
    detail: str
    preview: str = ""
    data_type_label: str = ""
    value_rank: int = 0


async def prepare_write(client: Any, node_id: str, value_text: str) -> PreparedWrite:
    """Read the server's metadata and parse ``value_text``; raise ``PrepareError`` otherwise."""
    try:
        node = client.get_node(node_id)
        node_class = await node.read_node_class()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise PrepareError(f"{node_id} could not be read: {type(exc).__name__}: {exc}") from exc
    if node_class != ua.NodeClass.Variable:
        raise PrepareError(f"{node_id} is a {node_class_name(node_class)}, not a Variable; nothing was written.")
    try:
        data_type, value_rank = await read_node_data_type(client, node_id)
    except MetadataUnavailable as exc:
        raise PrepareError(str(exc)) from exc
    try:
        value_type = await type_support.resolve_value_type(client, data_type, value_rank)
    except type_support.TypeUnsupported as exc:
        raise PrepareError(str(exc)) from exc
    try:
        parsed = value_type.parse(value_text)
    except ValueParseError as exc:
        raise PrepareError(f"The value was not written: {exc}") from exc
    return PreparedWrite(
        node=node,
        node_id=node_id,
        data_type=data_type,
        value_rank=value_rank,
        variant_type=value_type.variant_type,
        parsed=parsed,
        value_type=value_type,
    )


async def inspect_write(client: Any, node_id: str, value_text: str) -> Inspection:
    """Validate a write without sending it."""
    try:
        prepared = await prepare_write(client, node_id, value_text)
    except PrepareError as exc:
        return Inspection(ok=False, detail=str(exc))
    preview = preview_value(prepared.parsed)
    label = prepared.value_type.label() if prepared.value_type is not None else prepared.data_type.to_string()
    return Inspection(
        ok=True,
        detail=(
            f"would write {preview} to {node_id} "
            f"({label}, ValueRank {prepared.value_rank}) and read it back"
        ),
        preview=preview,
        data_type_label=label,
        value_rank=int(prepared.value_rank),
    )


async def _read_previous(node: Any, outcome: WriteOutcome) -> Any:
    try:
        previous_value = await node.read_data_value()
    except asyncio.CancelledError:
        raise
    except Exception:
        outcome.previous_value = "unreadable"
        return None
    outcome.previous_status_code = status_name(previous_value.StatusCode)
    if previous_value.StatusCode.is_good():
        outcome.previous_value = preview_value(previous_value.Value.Value)
        outcome.previous_raw = previous_value.Value.Value
        outcome.previous_known = True
        return outcome.previous_raw
    outcome.previous_value = "unreadable"
    return None


async def send_prepared_write(
    prepared: PreparedWrite,
    *,
    authorization: WriteAuthority,
    verify: bool = True,
) -> WriteOutcome:
    """Send exactly one prepared write, then read it back unless ``verify`` is false."""
    node_id = prepared.node_id
    outcome = WriteOutcome(node_id=node_id, authorization=authorization)
    started = time.perf_counter()
    outcome.new_value = preview_value(prepared.parsed)
    await _read_previous(prepared.node, outcome)

    try:
        await prepared.node.write_value(ua.DataValue(ua.Variant(prepared.parsed, prepared.variant_type)))
    except asyncio.CancelledError:
        raise WriteCancelled(sent=True) from None
    except ua.UaStatusCodeError as exc:
        outcome.sent = True
        outcome.write_status_code = status_name(exc.code)
        outcome.detail = f"Writing to {node_id} returned {outcome.write_status_code}."
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    except Exception as exc:
        outcome.sent = True
        outcome.detail = f"Writing to {node_id} failed: {type(exc).__name__}: {exc}"
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    outcome.sent = True
    outcome.write_status_code = "Good"

    if not verify:
        outcome.status = STATUS_PASSED
        outcome.detail = f"Wrote {outcome.new_value} to {node_id} (read-back was not requested)."
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    try:
        read_back = await prepared.node.read_data_value()
    except asyncio.CancelledError:
        raise WriteCancelled(sent=True) from None
    except Exception as exc:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            f"The value was written but could not be read back: {type(exc).__name__}: {exc}. "
            "The effect on the server is unknown."
        )
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    outcome.read_back_status_code = status_name(read_back.StatusCode)
    outcome.read_back_value = (
        preview_value(read_back.Value.Value) if read_back.StatusCode.is_good() else "unreadable"
    )
    if not read_back.StatusCode.is_good():
        outcome.status = STATUS_FAILED
        outcome.detail = f"The value was written but reading it back returned {outcome.read_back_status_code}."
    elif outcome.read_back_value != outcome.new_value:
        outcome.status = STATUS_FAILED
        outcome.detail = (
            f"Read-back {outcome.read_back_value} differs from the written value {outcome.new_value}."
        )
    else:
        outcome.status = STATUS_PASSED
        outcome.detail = f"Wrote {outcome.new_value} to {node_id} and read it back."
    outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return outcome


async def write_variable(
    client: Any,
    node_id: str,
    value_text: str,
    *,
    authorization: WriteAuthority,
    verify: bool = True,
) -> WriteOutcome:
    """Prepare and send one write to one variable; the only write path in the application."""
    try:
        prepared = await prepare_write(client, node_id, value_text)
    except PrepareError as exc:
        return WriteOutcome(node_id=node_id, status=STATUS_SKIPPED, detail=str(exc), authorization=authorization)
    return await send_prepared_write(prepared, authorization=authorization, verify=verify)


async def restore_value(
    prepared: PreparedWrite,
    previous: Any,
    *,
    authorization: WriteAuthority,
) -> RestoreOutcome:
    """Write back one previously read value: exactly one write, then a read-back compare.

    This is the only restore path; it exists so an optional "restore the previous value" is the
    same single, typed and verified write as any other, never a second implementation.
    """
    outcome = RestoreOutcome(node_id=prepared.node_id, authorization=authorization)
    outcome.written_value = preview_value(previous)
    started = time.perf_counter()
    try:
        await prepared.node.write_value(ua.DataValue(ua.Variant(previous, prepared.variant_type)))
    except asyncio.CancelledError:
        raise WriteCancelled(sent=True) from None
    except ua.UaStatusCodeError as exc:
        outcome.sent = True
        outcome.detail = f"Restoring {prepared.node_id} returned {status_name(exc.code)}."
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    except Exception as exc:
        outcome.sent = True
        outcome.detail = f"Restoring {prepared.node_id} failed: {type(exc).__name__}: {exc}"
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    outcome.sent = True

    try:
        read_back = await prepared.node.read_data_value()
    except asyncio.CancelledError:
        raise WriteCancelled(sent=True) from None
    except Exception as exc:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            f"The value was written back but could not be read back: {type(exc).__name__}: {exc}. "
            "The effect on the server is unknown."
        )
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    outcome.read_back_status_code = status_name(read_back.StatusCode)
    outcome.read_back_value = (
        preview_value(read_back.Value.Value) if read_back.StatusCode.is_good() else "unreadable"
    )
    if not read_back.StatusCode.is_good():
        outcome.status = STATUS_FAILED
        outcome.detail = f"Restore of {prepared.node_id} was written but reading it back returned {outcome.read_back_status_code}."
    elif outcome.read_back_value != outcome.written_value:
        outcome.status = STATUS_FAILED
        outcome.detail = (
            f"Restore of {prepared.node_id} read back {outcome.read_back_value}, "
            f"which differs from {outcome.written_value}."
        )
    else:
        outcome.status = STATUS_PASSED
        outcome.detail = f"Restored {prepared.node_id} to {outcome.written_value} and read it back."
    outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return outcome


async def write_and_restore(
    client: Any,
    node_id: str,
    value_text: str,
    *,
    authorization: WriteAuthority,
    restore_previous: bool = False,
) -> WriteResult:
    """Prepare and send one write, optionally restoring the value that was read first.

    This is the interactive path (an operator typed the value in a dialog).
    It sends exactly one write, reads it back, and - only when asked - one
    restore write of the previously read value.
    """
    try:
        prepared = await prepare_write(client, node_id, value_text)
    except PrepareError as exc:
        return WriteResult(
            WriteOutcome(node_id=node_id, status=STATUS_SKIPPED, detail=str(exc), authorization=authorization)
        )
    outcome = await send_prepared_write(prepared, authorization=authorization)
    result = WriteResult(outcome)
    if not restore_previous:
        return result
    if not outcome.sent or not outcome.previous_known:
        result.restore = RestoreOutcome(
            node_id=node_id,
            status=STATUS_SKIPPED,
            detail="The previous value could not be read, so nothing was restored.",
            authorization=authorization,
        )
        return result
    result.restore = await restore_value(prepared, outcome.previous_raw, authorization=authorization)
    return result


async def resolve_method_node_id(client: Any, object_node_id: str, method_node_id: str, method_name: str) -> str:
    """Resolve a method below an object by browse name when no NodeId was given."""
    if method_node_id.strip():
        return method_node_id.strip()
    if not method_name.strip():
        raise PrepareError("The step names neither a method NodeId nor a method browse name.")
    try:
        object_node = client.get_node(object_node_id)
        method_node = await object_node.get_child([method_name.strip()])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise PrepareError(
            f"Method {method_name!r} was not found below {object_node_id}: {type(exc).__name__}: {exc}"
        ) from exc
    return str(method_node.nodeid.to_string())


def _signature_row(argument: Any) -> dict[str, Any]:
    """One ``InputArguments`` entry as a plain row for confirmation and display."""
    description = getattr(argument, "Description", None)
    description_text = (
        (description.Text or "") if isinstance(description, ua.LocalizedText) else str(description or "")
    )
    return {
        "name": argument.Name or "",
        "data_type": argument.DataType.to_string(),
        "data_type_name": describe_data_type(argument.DataType),
        "value_rank": int(argument.ValueRank),
        "description": description_text,
    }


async def read_method_signature(client: Any, method_node_id: str) -> list[dict[str, Any]]:
    """``InputArguments`` of one method as plain rows; nothing is called."""
    try:
        arguments = await read_input_arguments(client, method_node_id)
    except MetadataUnavailable as exc:
        raise PrepareError(str(exc)) from exc
    return [_signature_row(argument) for argument in arguments]


async def prepare_call(
    client: Any,
    object_node_id: str,
    method_node_id: str,
    argument_texts: list[str],
) -> PreparedCall:
    """Validate one method call against the server's ``InputArguments``."""
    try:
        arguments = await read_input_arguments(client, method_node_id)
    except MetadataUnavailable as exc:
        raise PrepareError(str(exc)) from exc

    signature = [_signature_row(argument) for argument in arguments]

    if len(argument_texts) != len(arguments):
        rendered = ", ".join(
            f"{argument.Name or describe_data_type(argument.DataType)}" for argument in arguments
        ) or "none"
        raise PrepareError(
            f"The method expects {len(arguments)} argument(s) ({rendered}) but "
            f"{len(argument_texts)} were configured; the method was not called."
        )

    parsed: list[Any] = []
    previews: list[str] = []
    for index, (text, argument) in enumerate(zip(argument_texts, arguments, strict=True), start=1):
        if not isinstance(argument.DataType, ua.NodeId):
            raise PrepareError(
                f"Argument {index} ({argument.Name or 'unnamed'}) has no DataType; the method was not called."
            )
        try:
            value_type = await type_support.resolve_value_type(
                client, argument.DataType, int(argument.ValueRank)
            )
            value = value_type.parse(text)
        except type_support.TypeUnsupported as exc:
            raise PrepareError(
                f"Argument {index} ({argument.Name or argument.DataType.to_string()}): {exc}. "
                "The method was not called."
            ) from exc
        except ValueParseError as exc:
            raise PrepareError(
                f"Argument {index} ({argument.Name or argument.DataType.to_string()}): {exc}. "
                "The method was not called."
            ) from exc
        parsed.append(value)
        previews.append(preview_value(value))

    method_node = client.get_node(method_node_id)
    executable, executable_status = await read_executable(client, method_node_id)
    if executable is False:
        raise PrepareError(f"{method_node_id} reports Executable = false; the method was not called.")
    return PreparedCall(
        object_node=client.get_node(object_node_id),
        method_node=method_node,
        object_node_id=object_node_id,
        method_node_id=method_node_id,
        parsed=parsed,
        previews=previews,
        signature=signature,
        executable_status=executable_status,
    )


async def inspect_call(
    client: Any,
    object_node_id: str,
    method_node_id: str,
    argument_texts: list[str],
) -> Inspection:
    """Validate a method call without sending it."""
    try:
        prepared = await prepare_call(client, object_node_id, method_node_id, argument_texts)
    except PrepareError as exc:
        return Inspection(ok=False, detail=str(exc))
    rendered = ", ".join(prepared.previews) if prepared.previews else "no arguments"
    return Inspection(
        ok=True,
        detail=f"would call {method_node_id} on {object_node_id} with {rendered}",
        preview=rendered,
    )


async def send_prepared_call(prepared: PreparedCall, *, authorization: WriteAuthority) -> CallOutcome:
    """Send exactly one prepared method call."""
    outcome = CallOutcome(
        object_node_id=prepared.object_node_id,
        method_node_id=prepared.method_node_id,
        authorization=authorization,
    )
    outcome.arguments = list(prepared.previews)
    started = time.perf_counter()
    try:
        result = await prepared.object_node.call_method(prepared.method_node, *prepared.parsed)
    except asyncio.CancelledError:
        raise CallCancelled(sent=True) from None
    except ua.UaStatusCodeError as exc:
        outcome.sent = True
        outcome.status_code = status_name(exc.code)
        outcome.detail = (
            f"Calling {prepared.method_node_id} on {prepared.object_node_id} returned {outcome.status_code}."
        )
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    except Exception as exc:
        outcome.sent = True
        outcome.detail = f"Calling {prepared.method_node_id} failed: {type(exc).__name__}: {exc}"
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    outcome.sent = True
    outcome.status = STATUS_PASSED
    outcome.status_code = "Good"
    # asyncua unwraps a single output argument; multi-output methods return a list.
    if result is None:
        outcome.output_values = []
    elif isinstance(result, (list, tuple)):
        outcome.output_values = list(result)
    else:
        outcome.output_values = [result]
    outcome.output = preview_value(result) if result is not None else "no output arguments"
    rendered = ", ".join(prepared.previews) if prepared.previews else "no arguments"
    outcome.detail = (
        f"Called {prepared.method_node_id} on {prepared.object_node_id} with {rendered}; "
        f"output {outcome.output}."
    )
    outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return outcome


async def call_method(
    client: Any,
    object_node_id: str,
    method_node_id: str,
    argument_texts: list[str],
    *,
    authorization: WriteAuthority,
) -> CallOutcome:
    """Prepare and send one method call on one object node; exactly once."""
    try:
        prepared = await prepare_call(client, object_node_id, method_node_id, argument_texts)
    except PrepareError as exc:
        return CallOutcome(
            object_node_id=object_node_id,
            method_node_id=method_node_id,
            status=STATUS_SKIPPED,
            detail=str(exc),
            authorization=authorization,
        )
    return await send_prepared_call(prepared, authorization=authorization)


#: Browse name of the A&C method this module calls; nothing else is ever invoked.
ACKNOWLEDGE_METHOD_NAME = "Acknowledge"


async def acknowledge_condition(
    client: Any,
    condition_node_id: str,
    event_id: bytes,
    comment: str,
    *,
    authorization: WriteAuthority,
) -> CallOutcome:
    """Acknowledge one condition event: one ``Acknowledge`` call, exactly once.

    The method is resolved below the condition node by browse name and its
    ``InputArguments`` must be ``ByteString`` (EventId) and ``LocalizedText``
    (Comment) - the A&C definition. When either does not hold, nothing is sent.
    No retry, no ``Confirm``, no comment synthesis.
    """
    outcome = CallOutcome(
        object_node_id=condition_node_id, method_node_id="", authorization=authorization
    )
    if not event_id:
        outcome.status = STATUS_SKIPPED
        outcome.detail = "The event has no EventId; nothing was called."
        return outcome

    try:
        method_node_id = await resolve_method_node_id(
            client, condition_node_id, "", ACKNOWLEDGE_METHOD_NAME
        )
    except PrepareError as exc:
        outcome.status = STATUS_SKIPPED
        outcome.detail = str(exc)
        return outcome
    outcome.method_node_id = method_node_id

    try:
        prepared = await prepare_call(
            client,
            condition_node_id,
            method_node_id,
            [base64.b64encode(event_id).decode("ascii"), comment],
        )
    except PrepareError as exc:
        outcome.status = STATUS_SKIPPED
        outcome.detail = str(exc)
        return outcome

    expected = [
        ua.NodeId(ua.ObjectIds.ByteString).to_string(),
        ua.NodeId(ua.ObjectIds.LocalizedText).to_string(),
    ]
    actual = [str(entry.get("data_type") or "") for entry in prepared.signature]
    if actual != expected:
        outcome.status = STATUS_SKIPPED
        outcome.detail = (
            f"{condition_node_id} exposes {method_node_id} but its arguments are {actual or 'none'}; "
            "an Acknowledge method takes EventId (ByteString) and Comment (LocalizedText). "
            "The method was not called."
        )
        return outcome

    outcome = await send_prepared_call(prepared, authorization=authorization)
    # Acknowledge reports its own result as an output StatusCode: a Good service
    # result does not mean the acknowledgement was accepted.
    if outcome.status == STATUS_PASSED and outcome.output_values:
        code = outcome.output_values[0]
        if isinstance(code, ua.Variant):
            code = code.Value
        if isinstance(code, ua.StatusCode) and not code.is_good():
            name = status_name(code)
            outcome.status = STATUS_FAILED
            outcome.status_code = name
            outcome.detail = (
                f"The server returned {name} from Acknowledge on {condition_node_id}; "
                "the acknowledgement was not accepted."
            )
    return outcome


__all__ = [
    "CallCancelled",
    "CallOutcome",
    "Inspection",
    "PrepareError",
    "PreparedCall",
    "PreparedWrite",
    "RestoreOutcome",
    "WriteCancelled",
    "WriteOutcome",
    "acknowledge_condition",
    "call_method",
    "inspect_call",
    "inspect_write",
    "prepare_call",
    "prepare_write",
    "resolve_method_node_id",
    "restore_value",
    "send_prepared_call",
    "send_prepared_write",
    "write_variable",
]
