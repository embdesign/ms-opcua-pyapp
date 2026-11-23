"""Live event subscriptions and stored-event reads on an OPC UA session.

One :class:`LiveEvents` owns exactly one subscription whose monitored items
listen to the ``EventNotifier`` attribute of one or more source nodes (usually
the Server object). Every event the server publishes is reduced to display-safe
text and handed to ``emit``. :func:`read_event_history` does the same for a
server's stored events: one bounded ``HistoryRead`` with ``ReadEventDetails``,
the same field mapping and the same continuation-point handling as the live path.

This module never writes: the one action of event monitoring, a condition
acknowledgement, is a separate guarded call in
:mod:`opcua_explorer.opcua.write_service`. Condition *state* is read on demand in
:mod:`opcua_explorer.opcua.condition_state`.

Threading contract
------------------
Everything in this module runs on the asyncio worker loop. ``emit`` is supplied
by the GUI as a thread-safe reporter; no asyncio object ever escapes to the GUI
thread.

Field selection is server-driven: the subscription requests the fields of the
configured event type (``BaseEventType`` by default), so a custom event type
contributes its own properties without this module hard-coding a field list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from asyncua import ua
from asyncua.common.events import Event as UaEvent
from asyncua.common.subscription import EventNotificationHandlerAsync

from ..errors import InvalidNodeIdError, MonitorNotActiveError, OpcUaExplorerError
from ..models import (
    EVENT_FIELD_CHAR_LIMIT,
    REFRESH_END_EVENT_TYPE_ID,
    REFRESH_START_EVENT_TYPE_ID,
    STATUS_PASSED,
    ConditionRefreshResult,
    EventConfig,
    EventHistoryConfig,
    EventHistoryResult,
    EventNotification,
    EventSourceInfo,
    EventStart,
    MonitorStatusEvent,
    WriteAuthority,
    iso_utc,
    utc_now,
)
from . import write_service
from .attributes import status_name
from .browse import to_node_id
from .path_support import parse_browse_name
from .serialize import SerializationContext, serialize_value

_logger = logging.getLogger(__name__)

#: The OPC UA Server object: the only source most servers expose events for.
SERVER_OBJECT_NODE_ID = ua.NodeId(ua.ObjectIds.Server).to_string()
#: Standard base event type: every event, with the standard fields.
BASE_EVENT_TYPE_NODE_ID = ua.NodeId(ua.ObjectIds.BaseEventType).to_string()

#: Requested lifetime/keep-alive pair, matching the data-change subscription.
_LIFETIME_COUNT = 10_000
_MAX_KEEP_ALIVE_COUNT = 20
#: Events arrive in bursts far more readily than data changes; keep more per publish.
_MAX_NOTIFICATIONS_PER_PUBLISH = 1_000
#: Safety bound for stored-event continuation points; the count is the primary limit.
_MAX_HISTORY_PAGES = 50

#: How long a ``ConditionRefresh`` waits for the server's RefreshStart/RefreshEnd
#: events after the call returned; the subscription publishes on its own interval.
#: Minimum observation window after a ``ConditionRefresh`` call. The actual
#: window also covers three publishing intervals of the running subscription (see
#: ``LiveEvents._refresh_observation_timeout``): a refresh event can only be
#: delivered with a publish response, so a slow or busy server needs more than a
#: fixed two seconds without anything being wrong.
REFRESH_OBSERVATION_TIMEOUT_S = 3.0
#: Poll interval while waiting; the publish loop runs on this same event loop.
_REFRESH_POLL_INTERVAL_S = 0.05

#: Fields every event subscription selects, so the summary columns always work.
DEFAULT_EVENT_FIELDS = (
    "EventId",
    "EventType",
    "SourceNode",
    "SourceName",
    "Time",
    "ReceiveTime",
    "Message",
    "Severity",
)


def _select_names(config: EventConfig) -> list[ua.QualifiedName]:
    """Standard plus extra field names, deduplicated in request order."""
    names: list[ua.QualifiedName] = []
    seen: set[tuple[str, int]] = set()
    for text in (*DEFAULT_EVENT_FIELDS, *config.select_fields):
        try:
            name = parse_browse_name(text)
        except ValueError:  # pragma: no cover - normalised() removed empty entries
            continue
        key = (name.Name or "", int(name.NamespaceIndex or 0))
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _event_filter(event_type: ua.NodeId, config: EventConfig) -> ua.EventFilter:
    select = [
        ua.SimpleAttributeOperand(
            TypeDefinitionId=event_type,
            BrowsePath=[name],
            AttributeId=ua.AttributeIds.Value,
        )
        for name in _select_names(config)
    ]

    where = ua.ContentFilter(Elements=[])
    if config.min_severity is not None:
        severity = ua.SimpleAttributeOperand(
            TypeDefinitionId=event_type,
            BrowsePath=[ua.QualifiedName("Severity", 0)],
            AttributeId=ua.AttributeIds.Value,
        )
        literal = ua.LiteralOperand(Value=ua.Variant(int(config.min_severity), ua.VariantType.UInt16))
        element = ua.ContentFilterElement(
            FilterOperator=ua.FilterOperator.GreaterThanOrEqual,
            FilterOperands=[severity, literal],
        )
        where = ua.ContentFilter(Elements=[element])
    return ua.EventFilter(SelectClauses=select, WhereClause=where)


def build_event_filter(event_type: ua.NodeId, config: EventConfig) -> ua.EventFilter | None:
    """Build the server-side filter for ``config``; ``None`` keeps asyncua's default.

    The standard fields are always selected so the summary columns never depend
    on what the operator typed. Extra fields are added to the select clauses;
    ``min_severity`` becomes a ``WhereClause`` (Severity >= n), so the server
    discards low-severity events instead of the client receiving and dropping
    them. A plain default subscription keeps ``None`` because asyncua then asks
    the server for the event type's own fields.
    """
    settings = config.normalised()
    if not settings.select_fields and settings.min_severity is None:
        return None
    return _event_filter(event_type, settings)


def build_event_history_filter(event_type: ua.NodeId, config: EventHistoryConfig) -> ua.EventFilter:
    """Filter for a stored-event read; ``ReadEventDetails`` always needs one."""
    settings = EventConfig(
        event_type_node_id=config.event_type_node_id,
        select_fields=list(config.select_fields),
        min_severity=config.min_severity,
    ).normalised()
    return _event_filter(event_type, settings)


def build_event_history_request(
    node_id: Any,
    config: EventHistoryConfig,
    *,
    continuation_point: bytes = b"",
    release: bool = False,
) -> ua.HistoryReadParameters:
    """Build one ``HistoryRead`` for the stored events of a single source node."""
    settings = config.normalised()
    event_type = _parse_event_type(settings.event_type_node_id)
    details = ua.ReadEventDetails()
    details.StartTime = settings.start or ua.get_win_epoch()
    details.EndTime = settings.end or ua.get_win_epoch()
    details.NumValuesPerNode = int(settings.max_values)
    details.Filter = build_event_history_filter(event_type, settings)

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


#: Attributes of ``asyncua.common.subscription.Event`` that are bookkeeping, not fields.
_INTERNAL_EVENT_ATTRIBUTES = frozenset(
    {"server_handle", "select_clauses", "event_fields", "data_types", "emitting_node", "internal_properties"}
)

#: Top-level event-type properties that only a condition subtype selects. Their
#: presence in the accepted filter proves the subscription is a condition one.
_CONDITION_SELECTIONS = frozenset(
    {
        "AckedState",
        "ConfirmedState",
        "EnabledState",
        "Retain",
        "ConditionName",
        "BranchId",
        "ConditionId",
        "NodeId",
    }
)


def event_id_bytes(text: str) -> bytes | None:
    """Decode the hex ``EventId`` stored on a notification; ``None`` when unusable."""
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) % 2:
        return None
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return None


def _selects_condition_fields(event: Any) -> bool:
    """True when the filter selected a condition-only property.

    A subscription built from a condition type selects components such as
    ``EnabledState/Id``; the first path element is what identifies the condition.
    """
    for clause in getattr(event, "select_clauses", None) or []:
        browse_path = getattr(clause, "BrowsePath", None) or []
        if not browse_path:
            continue
        name = str(getattr(browse_path[0], "Name", "") or "")
        if name in _CONDITION_SELECTIONS or name.split("/", 1)[0] in _CONDITION_SELECTIONS:
            return True
    return False


def event_type_name(value: Any) -> str:
    """Display name of an ``EventType`` NodeId, falling back to its string form."""
    if isinstance(value, ua.Variant):
        value = value.Value
    if isinstance(value, ua.NodeId):
        if value.NamespaceIndex == 0 and isinstance(value.Identifier, int):
            return ua.ObjectIdNames.get(value.Identifier, value.to_string())
        return value.to_string()
    return "" if value is None else str(value)


def event_type_id(value: Any) -> str:
    """Canonical NodeId string of an ``EventType`` value."""
    if isinstance(value, ua.Variant):
        value = value.Value
    if isinstance(value, ua.NodeId):
        return value.to_string()
    return "" if value is None else str(value)


def event_field_text(value: Any, *, limit: int = EVENT_FIELD_CHAR_LIMIT) -> str:
    """Render one event field (any OPC UA value) as bounded display text."""
    text = _field_text(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, ua.Variant):
        return _field_text(value.Value)
    if isinstance(value, ua.LocalizedText):
        if value.Text:
            return value.Text
        return "" if not value.Locale else f"<{value.Locale}>"
    if isinstance(value, ua.NodeId):
        return value.to_string()
    if isinstance(value, ua.QualifiedName):
        return value.Name if not value.NamespaceIndex else f"{value.NamespaceIndex}:{value.Name}"
    if isinstance(value, ua.StatusCode):
        return status_name(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, datetime):
        return iso_utc(value) or ""
    if isinstance(value, ua.TimeZoneDataType):
        return f"offset={value.Offset} min, daylight={value.DaylightSavingInOffset}"
    if isinstance(value, (bool, int, float, str)):
        return str(value)

    context = SerializationContext()
    try:
        encoded = serialize_value(value, context)
        return json.dumps(encoded, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:  # pragma: no cover - the encoder is designed not to raise
        _logger.debug("Encoding an event field failed", exc_info=True)
        return f"<unrepresentable {type(value).__name__}>"


def build_notification(event: Any, sequence: int = 0) -> EventNotification:
    """Reduce one asyncua ``Event`` (live or from history) to display-safe text.

    Live notifications and stored events use the same mapping, so a field is
    never named one way in the log and another way in a history read.
    """
    fields: dict[str, str] = {}
    names = getattr(event, "data_types", None) or {}
    for name in names:
        if name in _INTERNAL_EVENT_ATTRIBUTES:
            continue
        fields[str(name)] = event_field_text(getattr(event, name, None))

    event_type_value = getattr(event, "EventType", None)
    severity_value = getattr(event, "Severity", None)
    if isinstance(severity_value, ua.Variant):
        severity_value = severity_value.Value
    try:
        severity = int(severity_value) if severity_value is not None else None
    except (TypeError, ValueError):  # pragma: no cover - defensive
        severity = None

    condition_node_id = ""
    if _selects_condition_fields(event):
        # ConditionId/NodeId identify the condition; the demo server leaves both
        # empty, where SourceNode is the condition object by definition.
        condition_node_id = (
            fields.get("ConditionId") or fields.get("NodeId") or fields.get("SourceNode", "")
        )

    return EventNotification(
        sequence=sequence,
        event_type=event_type_name(event_type_value),
        event_type_id=event_type_id(event_type_value),
        source_node=fields.get("SourceNode", ""),
        source_name=fields.get("SourceName", ""),
        severity=severity,
        message=fields.get("Message", ""),
        time=fields.get("Time") or None,
        receive_time=fields.get("ReceiveTime") or None,
        event_id=fields.get("EventId", ""),
        condition_node_id=condition_node_id,
        fields=fields,
    )


def _parse_event_type(text: str) -> ua.NodeId:
    """Parse an event-type NodeId; empty means the standard BaseEventType."""
    if not text:
        return ua.NodeId(ua.ObjectIds.BaseEventType)
    try:
        return ua.NodeId.from_string(text)
    except Exception as exc:
        raise InvalidNodeIdError(f"The event type NodeId {text!r} cannot be parsed.") from exc


class _LiveEventsHandler(EventNotificationHandlerAsync):
    """Forwards event and status notifications to the subscription owner."""

    def __init__(self, events: LiveEvents) -> None:
        self._events = events

    async def event_notification(self, event: Any) -> None:
        self._events._handle_event(event)

    async def status_change_notification(self, status: Any) -> None:
        self._events._handle_status_change(status)


class LiveEvents:
    """Owns one event subscription and its per-source monitored items."""

    def __init__(self, client: Any, emit: Callable[[Any], None]) -> None:
        self._client = client
        self._emit = emit
        self._subscription: Any | None = None
        self._config = EventConfig()
        self._event_type: ua.NodeId = ua.NodeId(ua.ObjectIds.BaseEventType)
        self._event_filter: ua.EventFilter | None = None
        self._sources: dict[int, EventSourceInfo] = {}
        self._sequence = 0
        #: Counters for the refresh evidence; ``refresh_conditions`` compares
        #: them before and after its single call.
        self._refresh_started = 0
        self._refresh_ended = 0

    # -- state -------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self._subscription is not None

    @property
    def subscription_id(self) -> int | None:
        if self._subscription is None:
            return None
        return getattr(self._subscription, "subscription_id", None)

    def source_count(self) -> int:
        return len(self._sources)

    # -- lifecycle ---------------------------------------------------------
    async def start(self, source_node_ids: Iterable[str], config: EventConfig) -> EventStart:
        """Create the subscription and listen to events on the given source nodes."""
        ids = list(dict.fromkeys(source_node_ids))
        if not ids:
            raise OpcUaExplorerError("At least one source NodeId is required to monitor events.")

        self._config = config.normalised()
        self._event_type = self._parse_event_type(self._config.event_type_node_id)
        self._event_filter = build_event_filter(self._event_type, self._config)

        if self._subscription is not None:
            await self.stop()

        params = ua.CreateSubscriptionParameters()
        params.RequestedPublishingInterval = float(self._config.publishing_interval_ms)
        params.RequestedLifetimeCount = _LIFETIME_COUNT
        params.RequestedMaxKeepAliveCount = _MAX_KEEP_ALIVE_COUNT
        params.MaxNotificationsPerPublish = _MAX_NOTIFICATIONS_PER_PUBLISH
        params.PublishingEnabled = True
        params.Priority = 0

        subscription = await self._client.create_subscription(params, _LiveEventsHandler(self))
        self._subscription = subscription
        self._sources = {}
        self._refresh_started = 0
        self._refresh_ended = 0
        _logger.info("Event subscription %s created", self.subscription_id)

        result = EventStart(
            subscription_id=self.subscription_id,
            requested_publishing_interval_ms=float(self._config.publishing_interval_ms),
        )
        result.sources = await self.add(ids)
        return result

    async def add(self, source_node_ids: Iterable[str]) -> list[EventSourceInfo]:
        """Listen to events of additional source nodes on the running subscription."""
        if self._subscription is None:
            raise MonitorNotActiveError("No event subscription is running. Start event monitoring first.")

        ids = list(dict.fromkeys(source_node_ids))
        infos: list[EventSourceInfo] = []
        for node_id in ids:
            info = EventSourceInfo(node_id=node_id)
            try:
                handle = await self._subscription.subscribe_events(
                    self._client.get_node(node_id),
                    self._event_type,
                    evfilter=self._event_filter,
                    queuesize=int(self._config.queue_size),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad source must not abort the rest
                code = getattr(exc, "code", None)
                info.ok = False
                info.status = status_name(code) if code is not None else "Failed"
                info.detail = f"The server refused event notifications for {node_id}: {exc}"
            else:
                if isinstance(handle, ua.StatusCode):
                    info.ok = False
                    info.status = status_name(handle)
                    info.detail = f"The server refused event notifications for {node_id} (see the StatusCode)."
                else:
                    info.client_handle = handle
                    info.status = "Good"
                    self._sources[handle] = info
            infos.append(info)
            if not info.ok:
                _logger.warning("Event source %s was not accepted: %s", node_id, info.status)
        return infos

    async def remove(self, handles: Iterable[int]) -> None:
        """Stop listening to the source nodes with the given handles."""
        if self._subscription is None:
            raise MonitorNotActiveError("No event subscription is running. Start event monitoring first.")
        wanted = [handle for handle in handles if handle in self._sources]
        if not wanted:
            return
        await self._subscription.unsubscribe(wanted)
        for handle in wanted:
            self._sources.pop(handle, None)

    async def stop(self) -> None:
        """Delete the subscription; safe to call when nothing runs."""
        subscription, self._subscription = self._subscription, None
        self._sources = {}
        if subscription is None:
            return
        try:
            await subscription.delete()
        except Exception as exc:  # pragma: no cover - server teardown is best effort
            _logger.warning("Deleting the event subscription reported: %s", exc)
        else:
            _logger.info("Event subscription deleted")

    # -- condition refresh -------------------------------------------------
    async def refresh_conditions(
        self,
        *,
        authorization: WriteAuthority,
        object_node_id: str = "",
        observation_timeout_s: float | None = None,
    ) -> ConditionRefreshResult:
        """Ask the server to re-publish this subscription's retained Conditions.

        One ``ConditionRefresh`` call (see
        :func:`opcua_explorer.opcua.write_service.refresh_conditions`) followed by
        a bounded wait for the ``RefreshStart``/``RefreshEnd`` events. The wait is
        an observation, not a retry: the call itself is sent once either way, and
        a missing event is reported instead of being turned into a verdict.
        """
        if self._subscription is None:
            raise MonitorNotActiveError("No event subscription is running. Start event monitoring first.")
        subscription_id = self.subscription_id
        started_before, ended_before = self._refresh_started, self._refresh_ended
        started = time.perf_counter()
        outcome = await write_service.refresh_conditions(
            self._client,
            int(subscription_id or 0),
            object_node_id=object_node_id,
            authorization=authorization,
        )
        window = (
            self._refresh_observation_timeout()
            if observation_timeout_s is None
            else max(0.0, float(observation_timeout_s))
        )
        observed_ms = await self._await_refresh_events(ended_before, window)

        result = ConditionRefreshResult(
            subscription_id=subscription_id,
            object_node_id=outcome.object_node_id,
            method_node_id=outcome.method_node_id,
            status=outcome.status,
            status_code=outcome.status_code,
            detail=outcome.detail,
            sent=outcome.sent,
            refresh_started=self._refresh_started > started_before,
            refresh_ended=self._refresh_ended > ended_before,
            observation_ms=observed_ms,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
            authorization=authorization,
        )
        if result.status == STATUS_PASSED and not result.refresh_started:
            hint = (
                "The server accepted ConditionRefresh but published no RefreshStart event on this "
                "subscription within "
                f"{window:.1f} s (publishing interval {self._config.publishing_interval_ms:.0f} ms); "
                "a server-side severity filter can hide it."
            )
            result.detail = f"{result.detail} {hint}".strip()
        return result

    def _refresh_observation_timeout(self) -> float:
        """Observation window that also fits the subscription's publishing rate."""
        publishing_s = max(0.0, float(self._config.publishing_interval_ms)) / 1000.0
        return max(REFRESH_OBSERVATION_TIMEOUT_S, 3.0 * publishing_s)

    async def _await_refresh_events(self, ended_before: int, timeout_s: float) -> float:
        """Wait bounded for a *new* RefreshEnd event; returns the observed wait in ms."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + max(0.0, float(timeout_s))
        while loop.time() < deadline:
            if self._refresh_ended > ended_before:
                break
            await asyncio.sleep(_REFRESH_POLL_INTERVAL_S)
        return round((loop.time() - started) * 1000.0, 2)

    # -- notification handling (called on the asyncio thread) --------------
    def _handle_event(self, event: Any) -> None:
        try:
            notification = self._build_notification(event)
        except Exception:  # pragma: no cover - a bad payload must not kill the publish loop
            _logger.exception("Building an event notification failed")
            return
        self._sequence += 1
        notification.sequence = self._sequence
        self._count_refresh_event(notification)
        try:
            self._emit(notification)
        except Exception:  # pragma: no cover - reporter must not raise
            _logger.exception("Delivering an event notification failed")

    def _count_refresh_event(self, notification: EventNotification) -> None:
        """Record a ``RefreshStart``/``RefreshEnd`` event as refresh evidence."""
        if notification.event_type_id == REFRESH_START_EVENT_TYPE_ID:
            self._refresh_started += 1
        elif notification.event_type_id == REFRESH_END_EVENT_TYPE_ID:
            self._refresh_ended += 1

    def _build_notification(self, event: Any) -> EventNotification:
        return build_notification(event)

    def _handle_status_change(self, status: Any) -> None:
        code = getattr(status, "Status", status)
        name = status_name(code)
        diagnostic = getattr(status, "DiagnosticInfos", None) or []
        detail = f"{len(diagnostic)} diagnostic info entr(y/ies)" if diagnostic else ""
        _logger.warning("Event subscription status changed: %s", name)
        try:
            self._emit(MonitorStatusEvent(status=name, detail=detail))
        except Exception:  # pragma: no cover - reporter must not raise
            _logger.exception("Delivering an event subscription status event failed")

    # -- configuration -----------------------------------------------------
    @staticmethod
    def _parse_event_type(text: str) -> ua.NodeId:
        return _parse_event_type(text)


async def read_event_history(
    session: Any, node_id: Any, config: EventHistoryConfig
) -> EventHistoryResult:
    """Read the stored events of one source node, oldest first.

    One ``ReadEventDetails`` request per page, continuation points followed up to
    a fixed cap and released when they are not followed. The server decides what
    it kept and in which order; this client only bounds the range and the count
    and sorts the decoded events chronologically for display.
    """
    settings = config.normalised()
    if settings.start and settings.end and settings.start > settings.end:
        raise OpcUaExplorerError("The event-history start time must not be after the end time.")

    node_id_string = to_node_id(node_id).to_string()
    event_type = _parse_event_type(settings.event_type_node_id)
    select_clauses = build_event_history_filter(event_type, settings).SelectClauses
    started = time.perf_counter()
    events: list[EventNotification] = []
    status = "Good"
    detail = ""
    truncated = False
    pages = 0
    continuation = b""

    try:
        while True:
            params = build_event_history_request(
                node_id_string, settings, continuation_point=continuation
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
                break

            history_data = getattr(result, "HistoryData", None)
            field_lists = getattr(history_data, "Events", None) or []
            for field_list in field_lists:
                fields = list(getattr(field_list, "EventFields", None) or [])
                event = UaEvent.from_event_fields(select_clauses, fields)
                events.append(build_notification(event, sequence=len(events) + 1))

            continuation = getattr(result, "ContinuationPoint", b"") or b""
            if not continuation:
                break

            if len(events) >= settings.max_values:
                await _release_event_continuation(session, node_id_string, settings, continuation)
                continuation = b""
                truncated = True
                detail = "Stopped at the requested maximum number of events."
                break

            if pages >= _MAX_HISTORY_PAGES:
                await _release_event_continuation(session, node_id_string, settings, continuation)
                continuation = b""
                truncated = True
                detail = f"Stopped after {pages} pages."
                break
    finally:
        if continuation:  # pragma: no cover - best effort on cancellation
            await _release_event_continuation(session, node_id_string, settings, continuation)

    events.sort(key=lambda event: event.time or event.receive_time or "")
    if len(events) > settings.max_values:
        events = events[-settings.max_values :]
        truncated = True
        detail = detail or "Stopped at the requested maximum number of events."

    if status == "Good" and not events:
        detail = detail or "The server returned no stored events for this source and range."

    return EventHistoryResult(
        node_id=node_id_string,
        events=events,
        status=status,
        truncated=truncated,
        requested_max_values=settings.max_values,
        pages=pages,
        detail=detail,
        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
        received_at=utc_now(),
    )


async def _release_event_continuation(
    session: Any, node_id: str, config: EventHistoryConfig, continuation: bytes
) -> None:
    with contextlib.suppress(Exception):
        await session.history_read(
            build_event_history_request(
                node_id, config, continuation_point=continuation, release=True
            )
        )


__all__ = [
    "BASE_EVENT_TYPE_NODE_ID",
    "SERVER_OBJECT_NODE_ID",
    "LiveEvents",
    "build_event_history_filter",
    "build_event_history_request",
    "build_notification",
    "event_field_text",
    "event_id_bytes",
    "event_type_id",
    "event_type_name",
    "read_event_history",
]
