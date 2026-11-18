"""Live data-change monitoring on an existing OPC UA session.

One :class:`LiveMonitor` owns exactly one subscription. Monitored items can be
added and removed while the subscription runs; stopping deletes the subscription
and therefore all of its monitored items.

Threading contract
------------------
Everything in this module runs on the asyncio worker loop. Notifications are
handed to ``emit``, which the GUI supplies as a thread-safe reporter (a Qt signal
emitter); no asyncio object ever escapes to the GUI thread.

The server, not the client, decides the effective publishing and sampling
interval. The values reported in :class:`MonitorStart` are the requested ones.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

from asyncua import ua
from asyncua.common.subscription import DataChangeNotificationHandlerAsync

from ..errors import MonitorNotActiveError, OpcUaExplorerError
from ..models import (
    MONITOR_VALUE_CHAR_LIMIT,
    MonitorConfig,
    MonitoredItemInfo,
    MonitorNotification,
    MonitorStart,
    MonitorStatusEvent,
    iso_utc,
    utc_now,
)
from .attributes import status_name
from .serialize import SerializationContext, serialize_value
from .ua_names import variant_type_name

_logger = logging.getLogger(__name__)

#: Requested lifetime/keep-alive pair. Ten seconds of publishing per keep-alive
#: is the usual safe choice for a diagnostics client.
_LIFETIME_COUNT = 10_000
_MAX_KEEP_ALIVE_COUNT = 20
_MAX_NOTIFICATIONS_PER_PUBLISH = 100

_DATA_CHANGE_TRIGGERS: dict[str, ua.DataChangeTrigger] = {
    "Status": ua.DataChangeTrigger.Status,
    "StatusValue": ua.DataChangeTrigger.StatusValue,
    "StatusValueTimestamp": ua.DataChangeTrigger.StatusValueTimestamp,
}
_DEADBAND_TYPES: dict[str, ua.DeadbandType] = {
    "None": ua.DeadbandType.None_,
    "Absolute": ua.DeadbandType.Absolute,
    "Percent": ua.DeadbandType.Percent,
}


def build_data_change_filter(config: MonitorConfig) -> ua.DataChangeFilter | None:
    """Build the ``DataChangeFilter`` for a config, or ``None`` for the UA default.

    asyncua's ``subscribe_data_change`` cannot carry a filter, so a filtered
    subscription creates its monitored items through ``create_monitored_items``
    (see :meth:`LiveMonitor.add`).
    """
    normalised = config.normalised()
    if not normalised.needs_filter:
        return None
    data_filter = ua.DataChangeFilter()
    data_filter.Trigger = _DATA_CHANGE_TRIGGERS[normalised.trigger]
    data_filter.DeadbandType = _DEADBAND_TYPES[normalised.deadband_type]
    data_filter.DeadbandValue = float(normalised.deadband_value)
    return data_filter


def _variant_type_name(data_value: Any) -> str | None:
    variant = getattr(data_value, "Value", None)
    if not isinstance(variant, ua.Variant):
        return None
    return variant_type_name(variant.VariantType)


def _encode_value(value: Any) -> tuple[Any, str, bool]:
    """Return ``(encoded, preview_text, truncated)`` for a notification value."""
    context = SerializationContext()
    try:
        encoded = serialize_value(value, context)
        text = json.dumps(encoded, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:  # pragma: no cover - the encoder is designed not to raise
        _logger.debug("Encoding a notification value failed", exc_info=True)
        return None, f"<unrepresentable {type(value).__name__}>", True

    if len(text) <= MONITOR_VALUE_CHAR_LIMIT:
        return encoded, text, False
    return None, text[: MONITOR_VALUE_CHAR_LIMIT - 1] + "…", True


class _LiveMonitorHandler(DataChangeNotificationHandlerAsync):
    """Forwards data-change and status-change notifications to the monitor."""

    def __init__(self, monitor: LiveMonitor) -> None:
        self._monitor = monitor

    async def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        self._monitor._handle_data_change(node, val, data)

    async def status_change_notification(self, status: Any) -> None:
        self._monitor._handle_status_change(status)


class LiveMonitor:
    """Owns one data-change subscription and its monitored items."""

    def __init__(self, client: Any, emit: Callable[[Any], None]) -> None:
        self._client = client
        self._emit = emit
        self._subscription: Any | None = None
        self._config = MonitorConfig()
        self._items: dict[int, MonitoredItemInfo] = {}
        self._sequence = 0
        self._client_handle = 0
        self._monitoring_mode = "Reporting"

    # -- state -------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self._subscription is not None

    @property
    def subscription_id(self) -> int | None:
        if self._subscription is None:
            return None
        return getattr(self._subscription, "subscription_id", None)

    def item_count(self) -> int:
        return len(self._items)

    def items_snapshot(self) -> list[MonitoredItemInfo]:
        """Public read-only copy of the monitored items (client view)."""
        return list(self._items.values())

    @property
    def monitoring_mode(self) -> str:
        """Server-side monitoring mode applied to every monitored item."""
        return self._monitoring_mode

    # -- lifecycle ---------------------------------------------------------
    async def start(self, node_ids: Iterable[str], config: MonitorConfig) -> MonitorStart:
        """Create the subscription and subscribe the given NodeIds."""
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            raise OpcUaExplorerError("At least one NodeId is required to start monitoring.")

        if self._subscription is not None:
            await self.stop()

        self._config = config.normalised()
        params = ua.CreateSubscriptionParameters()
        params.RequestedPublishingInterval = float(self._config.publishing_interval_ms)
        params.RequestedLifetimeCount = _LIFETIME_COUNT
        params.RequestedMaxKeepAliveCount = _MAX_KEEP_ALIVE_COUNT
        params.MaxNotificationsPerPublish = _MAX_NOTIFICATIONS_PER_PUBLISH
        params.PublishingEnabled = True
        params.Priority = 0

        subscription = await self._client.create_subscription(params, _LiveMonitorHandler(self))
        self._subscription = subscription
        self._items = {}
        self._client_handle = 0
        self._monitoring_mode = "Reporting"
        _logger.info("Live monitor subscription %s created", self.subscription_id)

        result = MonitorStart(
            subscription_id=self.subscription_id,
            requested_publishing_interval_ms=float(self._config.publishing_interval_ms),
            sampling_interval_ms=float(self._config.sampling_interval_ms),
            started_at=utc_now(),
        )
        result.items = await self.add(ids)
        return result

    async def add(self, node_ids: Iterable[str]) -> list[MonitoredItemInfo]:
        """Subscribe additional nodes to the running subscription."""
        if self._subscription is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")

        ids = list(dict.fromkeys(node_ids))
        if not ids:
            return []

        nodes = [self._client.get_node(node_id) for node_id in ids]
        data_filter = build_data_change_filter(self._config)
        if data_filter is None:
            results = await self._subscription.subscribe_data_change(
                nodes,
                queuesize=int(self._config.queue_size),
                sampling_interval=float(self._config.sampling_interval_ms),
            )
        else:
            # asyncua's subscribe_data_change has no filter parameter; the public
            # low-level path passes the DataChangeFilter unchanged to the server.
            results = await self._subscription.create_monitored_items(
                [self._monitored_item_request(node, data_filter) for node in nodes]
            )
        if isinstance(results, int):
            results = [results]

        infos: list[MonitoredItemInfo] = []
        for node_id, outcome in zip(ids, results, strict=False):
            if isinstance(outcome, int):
                info = MonitoredItemInfo(
                    node_id=node_id,
                    status="Good",
                    ok=True,
                    client_handle=outcome,
                    sampling_interval_ms=float(self._config.sampling_interval_ms),
                )
                self._items[outcome] = info
            else:
                info = MonitoredItemInfo(
                    node_id=node_id,
                    status=status_name(outcome),
                    ok=False,
                    detail="The server rejected this monitored item (see the StatusCode).",
                )
            infos.append(info)

        for info in infos:
            if not info.ok:
                _logger.warning("Monitored item for %s was rejected: %s", info.node_id, info.status)
        return infos

    async def remove(self, handles: Iterable[int]) -> None:
        """Delete the monitored items with the given client handles."""
        if self._subscription is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")
        wanted = [handle for handle in handles if handle in self._items]
        if not wanted:
            return
        await self._subscription.unsubscribe(wanted)
        for handle in wanted:
            self._items.pop(handle, None)

    async def set_monitoring_mode(self, mode: str) -> str:
        """Apply ``SetMonitoringMode`` to every monitored item of this subscription."""
        if self._subscription is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")
        try:
            monitoring = ua.MonitoringMode[mode]
        except KeyError as exc:
            raise OpcUaExplorerError(
                f"Unknown monitoring mode {mode!r}; use Reporting, Sampling or Disabled."
            ) from exc

        statuses = await self._subscription.set_monitoring_mode(monitoring)
        refused = [status_name(status) for status in statuses if not status.is_good()]
        if refused:
            raise OpcUaExplorerError(
                f"The server refused monitoring mode {mode}: " + ", ".join(refused)
            )
        self._monitoring_mode = mode
        _logger.info("Monitoring mode set to %s for subscription %s", mode, self.subscription_id)
        return mode

    async def set_publishing_interval(self, interval_ms: float) -> float:
        """Modify the publishing interval; returns the interval the server revised to."""
        if self._subscription is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")

        parameters = self._subscription.parameters
        params = ua.ModifySubscriptionParameters()
        params.SubscriptionId = self.subscription_id
        params.RequestedPublishingInterval = float(interval_ms)
        params.RequestedLifetimeCount = parameters.RequestedLifetimeCount
        params.RequestedMaxKeepAliveCount = parameters.RequestedMaxKeepAliveCount
        params.MaxNotificationsPerPublish = parameters.MaxNotificationsPerPublish
        params.Priority = parameters.Priority
        result = await self._subscription.update(params)
        self._config.publishing_interval_ms = float(interval_ms)
        revised = float(getattr(result, "RevisedPublishingInterval", interval_ms))
        _logger.info(
            "Publishing interval of subscription %s requested %s ms, revised %s ms",
            self.subscription_id,
            interval_ms,
            revised,
        )
        return revised

    async def set_sampling_interval(self, interval_ms: float) -> list[str]:
        """Modify every monitored item's sampling interval; returns refusal texts."""
        if self._subscription is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")

        failures: list[str] = []
        for handle in list(self._items):
            try:
                results = await self._subscription.modify_monitored_item(
                    handle, float(interval_ms), int(self._config.queue_size)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one item must not abort the rest
                failures.append(f"{handle}: {type(exc).__name__}: {exc}")
                continue
            for result in results:
                code = getattr(result, "StatusCode", None)
                if code is not None and not code.is_good():
                    failures.append(f"{handle}: {status_name(code)}")

        self._config.sampling_interval_ms = float(interval_ms)
        for info in self._items.values():
            info.sampling_interval_ms = float(interval_ms)
        return failures

    def _monitored_item_request(
        self, node: Any, data_filter: ua.DataChangeFilter
    ) -> ua.MonitoredItemCreateRequest:
        """One ``MonitoredItemCreateRequest`` carrying the requested filter."""
        self._client_handle += 1
        read_value = ua.ReadValueId()
        read_value.NodeId = node.nodeid
        read_value.AttributeId = ua.AttributeIds.Value

        parameters = ua.MonitoringParameters()
        parameters.ClientHandle = self._client_handle
        parameters.SamplingInterval = float(self._config.sampling_interval_ms)
        parameters.QueueSize = int(self._config.queue_size)
        parameters.DiscardOldest = True
        parameters.Filter = data_filter

        request = ua.MonitoredItemCreateRequest()
        request.ItemToMonitor = read_value
        request.MonitoringMode = ua.MonitoringMode.Reporting
        request.RequestedParameters = parameters
        return request

    async def stop(self) -> None:
        """Delete the subscription; safe to call when nothing runs."""
        subscription, self._subscription = self._subscription, None
        self._items = {}
        self._monitoring_mode = "Reporting"
        if subscription is None:
            return
        try:
            await subscription.delete()
        except Exception as exc:  # pragma: no cover - server teardown is best effort
            _logger.warning("Deleting the live monitor subscription reported: %s", exc)
        else:
            _logger.info("Live monitor subscription deleted")

    # -- notification handling (called on the asyncio thread) --------------
    def _handle_data_change(self, node: Any, val: Any, data: Any) -> None:
        try:
            notification = self._build_notification(node, val, data)
        except Exception:  # pragma: no cover - a bad payload must not kill the publish loop
            _logger.exception("Building a monitor notification failed")
            return
        self._sequence += 1
        notification.sequence = self._sequence
        try:
            self._emit(notification)
        except Exception:  # pragma: no cover - reporter must not raise
            _logger.exception("Delivering a monitor notification failed")

    def _build_notification(self, node: Any, val: Any, data: Any) -> MonitorNotification:
        node_id = ""
        nodeid = getattr(node, "nodeid", None)
        if nodeid is not None and hasattr(nodeid, "to_string"):
            node_id = nodeid.to_string()

        data_value = getattr(getattr(data, "monitored_item", None), "Value", None)
        status = "Good"
        source_timestamp: str | None = None
        server_timestamp: str | None = None
        variant_type: str | None = None
        if isinstance(data_value, ua.DataValue):
            status = status_name(data_value.StatusCode)
            source_timestamp = iso_utc(data_value.SourceTimestamp)
            server_timestamp = iso_utc(data_value.ServerTimestamp)
            variant_type = _variant_type_name(data_value)

        encoded, text, truncated = _encode_value(val)
        return MonitorNotification(
            sequence=0,
            node_id=node_id,
            value=encoded,
            value_text=text,
            status=status,
            variant_type=variant_type,
            source_timestamp=source_timestamp,
            server_timestamp=server_timestamp,
            truncated=truncated,
        )

    def _handle_status_change(self, status: Any) -> None:
        code = getattr(status, "Status", status)
        name = status_name(code)
        diagnostic = getattr(status, "DiagnosticInfos", None) or []
        detail = f"{len(diagnostic)} diagnostic info entr(y/ies)" if diagnostic else ""
        _logger.warning("Live monitor subscription status changed: %s", name)
        try:
            self._emit(MonitorStatusEvent(status=name, detail=detail))
        except Exception:  # pragma: no cover - reporter must not raise
            _logger.exception("Delivering a subscription status event failed")
