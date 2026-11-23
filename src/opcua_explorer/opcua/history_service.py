"""History reads and the one HistoryUpdate path.

Reads: ``ReadRawModifiedDetails`` for raw values, ``ReadProcessedDetails`` when
an aggregate is selected (average/minimum/maximum/count). The loop follows
continuation points up to a bounded number of pages and the requested maximum
number of values, and releases a continuation point it does not follow.

Update: the only ``HistoryUpdate`` implementation in this client - insert raw
values, replace raw values, or delete a raw range on **one** node. Values are
parsed with the server's DataType/ValueRank before anything is sent, every send
names its ``WriteAuthority``, and the call is sent once and never retried. An
accepted call is followed by a read-back whose timestamps are the only evidence
reported; it is never described as proof of persistence.

The server decides ordering and availability. A Good StatusCode with no values
is normal (empty range or a node without history); the node's ``Historizing``
attribute is read as an extra hint and never as a gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from asyncua import ua

from ..defaults import MAX_HISTORY_MAX_VALUES, MAX_HISTORY_UPDATE_ENTRIES
from ..errors import OpcUaExplorerError
from ..models import (
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    HistoryConfig,
    HistoryResult,
    HistoryUpdateConfig,
    HistoryUpdateOutcome,
    ValueSnapshot,
    WriteAuthority,
    iso_utc,
    utc_now,
)
from .attributes import AttributeSpec, decode_data_value, read_attributes_bulk
from .browse import to_node_id
from .method_support import MetadataUnavailable, read_node_data_type
from .type_support import TypeUnsupported, resolve_value_type
from .ua_names import status_name
from .value_parsing import ValueParseError

_logger = logging.getLogger(__name__)

#: Safety bound for continuation points; the value cap is the primary limit.
_MAX_PAGES = 50

#: ``HistoryUpdate`` action -> ``PerformUpdateType`` for the two value-writing shapes.
_UPDATE_PERFORM = {
    "insert": ua.PerformUpdateType.Insert,
    "replace": ua.PerformUpdateType.Replace,
}

#: Aggregate function keys (``HistoryConfig.aggregate``) to the standard NodeIds.
_AGGREGATE_NODE_IDS: dict[str, int] = {
    "average": ua.ObjectIds.AggregateFunction_Average,
    "minimum": ua.ObjectIds.AggregateFunction_Minimum,
    "maximum": ua.ObjectIds.AggregateFunction_Maximum,
    "count": ua.ObjectIds.AggregateFunction_Count,
}


def build_history_request(
    node_id: str,
    config: HistoryConfig,
    *,
    continuation_point: bytes = b"",
    release: bool = False,
) -> ua.HistoryReadParameters:
    """Build one ``HistoryRead`` request for a single node (raw or processed)."""
    normalised = config.normalised()
    if normalised.aggregate:
        if normalised.start is None or normalised.end is None:
            raise OpcUaExplorerError("An aggregate history read requires a start and an end time.")
        details: Any = ua.ReadProcessedDetails()
        details.StartTime = normalised.start
        details.EndTime = normalised.end
        details.ProcessingInterval = float(normalised.processing_interval_ms)
        details.AggregateType = [ua.NodeId(_AGGREGATE_NODE_IDS[normalised.aggregate])]
        aggregate_config = ua.AggregateConfiguration()
        aggregate_config.UseServerCapabilitiesDefaults = True
        details.AggregateConfiguration = aggregate_config
    else:
        details = ua.ReadRawModifiedDetails()
        details.IsReadModified = False
        details.StartTime = normalised.start or ua.get_win_epoch()
        details.EndTime = normalised.end or ua.get_win_epoch()
        details.NumValuesPerNode = int(normalised.max_values)
        details.ReturnBounds = False

    read_value = ua.HistoryReadValueId()
    read_value.NodeId = to_node_id(node_id)
    read_value.IndexRange = ""
    read_value.DataEncoding = ua.QualifiedName()
    read_value.ContinuationPoint = continuation_point

    params = ua.HistoryReadParameters()
    params.HistoryReadDetails = details
    params.TimestampsToReturn = ua.TimestampsToReturn.Both
    params.ReleaseContinuationPoints = bool(release)
    params.NodesToRead = [read_value]
    return params


async def _read_historizing(session: Any, node_id: str) -> bool | None:
    try:
        results = await read_attributes_bulk(
            session, [(node_id, AttributeSpec(ua.AttributeIds.Historizing, "Historizing"))]
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a hint must not fail the read
        _logger.debug("Reading Historizing for %s failed: %s", node_id, exc)
        return None
    result = results.get((node_id, "Historizing"))
    if result is None or not result.ok or result.value is None:
        return None
    return bool(result.value)


async def _release_quietly(session: Any, params: ua.HistoryReadParameters) -> None:
    with contextlib.suppress(Exception):
        await session.history_read(params)


async def read_history(session: Any, node_id: Any, config: HistoryConfig) -> HistoryResult:
    """Read raw history of one node, oldest value first."""
    normalised = config.normalised()
    if normalised.start and normalised.end and normalised.start > normalised.end:
        raise OpcUaExplorerError("The history start time must not be after the end time.")

    node_id_string = to_node_id(node_id).to_string()
    historizing = await _read_historizing(session, node_id_string)

    started = time.perf_counter()
    values: list[ValueSnapshot] = []
    status = "Good"
    detail = ""
    truncated = False
    pages = 0
    continuation = b""

    try:
        while True:
            params = build_history_request(
                node_id_string, normalised, continuation_point=continuation
            )
            results = await session.history_read(params)
            pages += 1
            if not results:
                status = "BadNoResult"
                detail = "The server returned no HistoryRead result."
                break

            result = results[0]
            if not result.StatusCode.is_good():
                status = status_name(result.StatusCode)
                detail = f"HistoryRead returned {status}."
                if normalised.aggregate and status in ("BadNotImplemented", "BadHistoryOperationUnsupported"):
                    detail += " The server does not provide aggregate history for this node."
                break

            history_data = getattr(result, "HistoryData", None)
            if history_data is not None:
                for data_value in history_data.DataValues or []:
                    values.append(decode_data_value(data_value))

            continuation = getattr(result, "ContinuationPoint", b"") or b""
            if not continuation:
                break

            if len(values) >= normalised.max_values:
                await _release_quietly(
                    session,
                    build_history_request(
                        node_id_string, normalised, continuation_point=continuation, release=True
                    ),
                )
                continuation = b""
                truncated = True
                detail = "Stopped at the requested maximum number of values."
                break

            if pages >= _MAX_PAGES:
                await _release_quietly(
                    session,
                    build_history_request(
                        node_id_string, normalised, continuation_point=continuation, release=True
                    ),
                )
                continuation = b""
                truncated = True
                detail = f"Stopped after {pages} pages."
                break
    finally:
        if continuation:  # pragma: no cover - release best effort on cancellation
            await _release_quietly(
                session,
                build_history_request(
                    node_id_string, normalised, continuation_point=continuation, release=True
                ),
            )

    # The server decides ordering; the UI shows a chronological table. When the server ignores the
    # cap and returns more, keep the newest values and say that the result was cut.
    values.sort(key=lambda snapshot: snapshot.source_timestamp or snapshot.server_timestamp or "")
    if len(values) > normalised.max_values:
        values = values[-normalised.max_values :]
        truncated = True
        detail = detail or "Stopped at the requested maximum number of values."

    if status == "Good" and not values:
        if historizing is False:
            detail = detail or "The node reports Historizing = false; the server holds no history for it."
        else:
            detail = detail or "The server returned no values for this range."

    duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return HistoryResult(
        node_id=node_id_string,
        values=values,
        status=status,
        historizing=historizing,
        truncated=truncated,
        requested_max_values=normalised.max_values,
        pages=pages,
        detail=detail,
        duration_ms=duration_ms,
        received_at=utc_now(),
    )


class PrepareHistoryError(ValueError):
    """The history update cannot be built safely; nothing was sent."""


@dataclass(slots=True)
class PreparedHistoryUpdate:
    """One validated ``HistoryUpdate`` request plus the text it was parsed from."""

    node_id: str
    config: HistoryUpdateConfig
    details: Any
    value_type: Any | None = None
    parsed: list[Any] | None = None
    timestamps: list[datetime] | None = None

    @property
    def sent_value_count(self) -> int:
        return 0 if self.config.action == "delete" else len(self.parsed or [])


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def build_history_update_details(
    node_id: str,
    config: HistoryUpdateConfig,
    *,
    value_type: Any | None = None,
    values: list[ua.DataValue] | None = None,
) -> Any:
    """Build the ``HistoryUpdateDetails`` for one action; refuse an incomplete request.

    ``delete`` needs a start and an end and carries no values. ``insert`` and
    ``replace`` need at least one ``DataValue``; they use
    ``UpdateStructureDataDetails`` for a structure type and ``UpdateDataDetails``
    otherwise, as the service definition requires.
    """
    settings = config.normalised()
    target = to_node_id(node_id)
    if settings.action == "delete":
        if settings.start is None or settings.end is None:
            raise PrepareHistoryError("A history delete needs a start and an end time.")
        if _aware(settings.start) > _aware(settings.end):
            raise PrepareHistoryError("The history delete start must not be after its end.")
        details = ua.DeleteRawModifiedDetails()
        details.NodeId = target
        details.StartTime = _aware(settings.start)
        details.EndTime = _aware(settings.end)
        details.IsDeleteModified = False
        return details

    if settings.action not in _UPDATE_PERFORM:
        raise PrepareHistoryError(
            f"{settings.action or '(none)'} is not a history update action; "
            "use insert, replace or delete."
        )
    if not values:
        raise PrepareHistoryError("A history insert/replace needs at least one value.")
    structure = value_type is not None and getattr(value_type, "kind", "") == "structure"
    details = ua.UpdateStructureDataDetails() if structure else ua.UpdateDataDetails()
    details.NodeId = target
    details.PerformInsertReplace = _UPDATE_PERFORM[settings.action]
    details.UpdateValues = list(values)
    return details


async def prepare_history_update(client: Any, config: HistoryUpdateConfig) -> PreparedHistoryUpdate:
    """Resolve the node's type and parse every value; raise ``PrepareHistoryError``."""
    settings = config.normalised()
    if not settings.node_id:
        raise PrepareHistoryError("A history update needs a NodeId.")
    try:
        node = client.get_node(settings.node_id)
        node_class = await node.read_node_class()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise PrepareHistoryError(
            f"{settings.node_id} could not be read: {type(exc).__name__}: {exc}"
        ) from exc
    if node_class != ua.NodeClass.Variable:
        raise PrepareHistoryError(
            f"{settings.node_id} is a {node_class.name}; only a Variable has history."
        )

    if settings.action == "delete":
        details = build_history_update_details(settings.node_id, settings)
        return PreparedHistoryUpdate(
            node_id=settings.node_id, config=settings, details=details, parsed=[], timestamps=[]
        )

    if settings.action not in _UPDATE_PERFORM:
        raise PrepareHistoryError(
            f"{settings.action or '(none)'} is not a history update action; use insert, replace or delete."
        )
    if len(settings.entries) > MAX_HISTORY_UPDATE_ENTRIES:
        raise PrepareHistoryError(
            f"At most {MAX_HISTORY_UPDATE_ENTRIES} values can be sent in one HistoryUpdate; nothing was sent."
        )
    if not settings.entries:
        raise PrepareHistoryError("A history insert/replace needs at least one value.")
    try:
        data_type, value_rank = await read_node_data_type(client, settings.node_id)
    except MetadataUnavailable as exc:
        raise PrepareHistoryError(str(exc)) from exc
    try:
        value_type = await resolve_value_type(client, data_type, value_rank)
    except TypeUnsupported as exc:
        raise PrepareHistoryError(str(exc)) from exc

    parsed: list[Any] = []
    values: list[ua.DataValue] = []
    timestamps: list[datetime] = []
    for entry in settings.entries:
        try:
            value = value_type.parse(entry.value_text)
        except ValueParseError as exc:
            raise PrepareHistoryError(
                f"The history update was not sent: {exc} (entry {iso_utc(entry.source_timestamp)})."
            ) from exc
        data_value = ua.DataValue(ua.Variant(value, value_type.variant_type))
        source_timestamp = _aware(entry.source_timestamp)
        data_value.SourceTimestamp = source_timestamp
        if entry.server_timestamp is not None:
            data_value.ServerTimestamp = _aware(entry.server_timestamp)
        parsed.append(value)
        values.append(data_value)
        timestamps.append(source_timestamp)

    details = build_history_update_details(
        settings.node_id, settings, value_type=value_type, values=values
    )
    return PreparedHistoryUpdate(
        node_id=settings.node_id,
        config=settings,
        details=details,
        value_type=value_type,
        parsed=parsed,
        timestamps=timestamps,
    )


async def send_prepared_history_update(
    session: Any,
    prepared: PreparedHistoryUpdate,
    *,
    authorization: WriteAuthority,
) -> HistoryUpdateOutcome:
    """Send one prepared ``HistoryUpdate`` request; never retried."""
    outcome = HistoryUpdateOutcome(
        action=prepared.config.action,
        node_id=prepared.node_id,
        entries_sent=prepared.sent_value_count,
        authorization=authorization,
    )
    started = time.perf_counter()
    params = ua.HistoryUpdateParameters()
    params.HistoryUpdateDetails = [prepared.details]
    try:
        results = await session.history_update(params)
    except asyncio.CancelledError:
        raise
    except ua.UaStatusCodeError as exc:
        outcome.sent = True
        outcome.status_code = status_name(exc.code)
        outcome.status = STATUS_FAILED
        outcome.detail = f"The server refused the HistoryUpdate with {outcome.status_code}."
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome
    except Exception as exc:  # noqa: BLE001 - the request may have reached the server
        outcome.sent = True
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            "The HistoryUpdate was sent but no result was received: "
            f"{type(exc).__name__}: {exc}"
        )
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    outcome.sent = True
    if not results:
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = "The server returned no HistoryUpdate result."
        outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return outcome

    result = results[0]
    outcome.status_code = status_name(result.StatusCode)
    outcome.operation_results = [status_name(code) for code in (result.OperationResults or [])]
    bad = [text for text in outcome.operation_results if text != "Good"]
    if result.StatusCode.is_good() and not bad:
        outcome.status = STATUS_PASSED
        outcome.detail = ""
    else:
        outcome.status = STATUS_FAILED
        outcome.detail = f"HistoryUpdate returned {outcome.status_code}."
        if bad:
            outcome.detail += " Per-operation result(s): " + ", ".join(bad) + "."
    outcome.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return outcome


async def history_update(
    client: Any, config: HistoryUpdateConfig, *, authorization: WriteAuthority
) -> HistoryUpdateOutcome:
    """Prepare, send and read back one history update.

    The call is sent once; a successful status is followed by exactly one raw
    read of the affected range. The read-back only reports which requested
    timestamps are present (or, for a delete, whether values remain); it is
    evidence, not a guarantee that the historian kept the values.
    """
    settings = config.normalised()
    try:
        prepared = await prepare_history_update(client, settings)
    except PrepareHistoryError as exc:
        return HistoryUpdateOutcome(
            action=settings.action,
            node_id=settings.node_id,
            status=STATUS_SKIPPED,
            detail=str(exc),
            authorization=authorization,
        )

    session = getattr(getattr(client, "uaclient", None), "session", None)
    if session is None:
        raise OpcUaExplorerError("No active OPC UA session. Connect to a server first.")

    outcome = await send_prepared_history_update(session, prepared, authorization=authorization)
    if outcome.status != STATUS_PASSED:
        return outcome

    timestamps = prepared.timestamps or []
    if settings.action == "delete":
        read_config = HistoryConfig(
            start=settings.start, end=settings.end, max_values=MAX_HISTORY_MAX_VALUES
        )
    else:
        read_config = HistoryConfig(
            start=min(timestamps),
            end=max(timestamps),
            max_values=min(MAX_HISTORY_MAX_VALUES, max(50, len(timestamps) + 50)),
        )
    try:
        verification = await read_history(session, settings.node_id, read_config)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the update itself already happened
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            f"{outcome.detail} The read-back failed ({type(exc).__name__}: {exc}), so the "
            "stored result is unknown."
        ).strip()
        return outcome

    outcome.verification = verification
    if verification.status != "Good":
        outcome.status = STATUS_OUTCOME_UNKNOWN
        outcome.detail = (
            f"{outcome.detail} The read-back returned {verification.status}, so the stored "
            "result is unknown."
        ).strip()
        return outcome

    if settings.action == "delete":
        outcome.verified = not verification.values
        outcome.detail = (
            "The read-back range is empty."
            if outcome.verified
            else f"The read-back still contains {len(verification.values)} value(s) in the range."
        )
        return outcome

    requested = {iso_utc(timestamp) for timestamp in timestamps}
    present = {
        snapshot.source_timestamp
        for snapshot in verification.values
        if snapshot.source_timestamp is not None
    }
    matched = len(requested & present)
    outcome.verified = requested.issubset(present)
    outcome.detail = (
        f"The read-back contains {matched} of {len(requested)} requested timestamp(s)."
    )
    return outcome


__all__ = [
    "PrepareHistoryError",
    "PreparedHistoryUpdate",
    "build_history_request",
    "build_history_update_details",
    "history_update",
    "prepare_history_update",
    "read_history",
    "send_prepared_history_update",
]
