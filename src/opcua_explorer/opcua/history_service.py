"""Read-only history reads: raw or processed (aggregate).

One call path: ``ReadRawModifiedDetails`` for raw values, ``ReadProcessedDetails``
when an aggregate is selected (average/minimum/maximum/count). There is no
``HistoryUpdate``, no event history and no at-time read. The loop follows
continuation points up to a bounded number of pages and the requested maximum
number of values, and releases a continuation point it does not follow.

The server decides ordering and availability. A Good StatusCode with no values
is normal (empty range or a node without history); the node's ``Historizing``
attribute is read as an extra hint and never as a gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from asyncua import ua

from ..errors import OpcUaExplorerError
from ..models import HistoryConfig, HistoryResult, ValueSnapshot, utc_now
from .attributes import AttributeSpec, decode_data_value, read_attributes_bulk
from .browse import to_node_id
from .ua_names import status_name

_logger = logging.getLogger(__name__)

#: Safety bound for continuation points; the value cap is the primary limit.
_MAX_PAGES = 50

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


__all__ = ["build_history_request", "read_history"]
