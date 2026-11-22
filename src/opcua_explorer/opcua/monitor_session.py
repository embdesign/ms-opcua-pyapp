"""Lifecycle of one live data-change subscription.

The subscription outlives the coroutine that created it, so it is owned by a
small collaborator instead of the client-service façade. ``OpcUaService`` keeps
the public ``start_monitor`` / ``monitor_add`` / … methods and delegates here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from ..errors import MonitorNotActiveError
from ..models import MonitorConfig, MonitoredItemInfo, MonitorStart
from .monitor_service import LiveMonitor

_logger = logging.getLogger(__name__)


class MonitorSession:
    """One live-monitor subscription, or none."""

    def __init__(self) -> None:
        self._monitor: LiveMonitor | None = None

    @property
    def is_active(self) -> bool:
        return self._monitor is not None and self._monitor.is_active

    @property
    def subscription_id(self) -> int | None:
        """Subscription id for the server-status snapshot; ``None`` when stopped."""
        return self._monitor.subscription_id if self._monitor is not None else None

    def items_snapshot(self) -> list[MonitoredItemInfo]:
        """The client's own view of its monitored items (never a server call)."""
        return self._monitor.items_snapshot() if self._monitor is not None else []

    def _running(self) -> LiveMonitor:
        if self._monitor is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")
        return self._monitor

    async def start(
        self,
        client: Any,
        node_ids: list[str],
        config: MonitorConfig,
        *,
        emit: Callable[[Any], None],
    ) -> MonitorStart:
        """Replace any running subscription with one for ``node_ids``."""
        await self.stop()
        monitor = LiveMonitor(client, emit)
        start = await monitor.start(node_ids, config)
        self._monitor = monitor
        _logger.info(
            "Live monitor started: subscription %s, %d of %d item(s)",
            start.subscription_id,
            sum(1 for item in start.items if item.ok),
            len(start.items),
        )
        return start

    async def add(self, node_ids: Iterable[str]) -> list[MonitoredItemInfo]:
        monitor = self._running()
        ids = list(node_ids)
        if not ids:
            return []
        return await monitor.add(ids)

    async def remove(self, handles: Iterable[int]) -> None:
        await self._running().remove(handles)

    async def set_monitoring_mode(self, mode: str) -> str:
        return await self._running().set_monitoring_mode(mode)

    async def apply_intervals(
        self, *, publishing_interval_ms: float, sampling_interval_ms: float
    ) -> tuple[float, list[str]]:
        """Modify publishing and sampling intervals on the running subscription."""
        monitor = self._running()
        revised = await monitor.set_publishing_interval(publishing_interval_ms)
        failures = await monitor.set_sampling_interval(sampling_interval_ms)
        return revised, failures

    async def stop(self) -> None:
        """Delete the subscription; safe to call when none runs."""
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            await monitor.stop()
