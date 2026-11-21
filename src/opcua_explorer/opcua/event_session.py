"""Lifecycle of one event subscription.

Like :mod:`opcua.monitor_session`, the subscription outlives the coroutine that
created it; ``OpcUaService`` owns the public methods and delegates here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from ..errors import MonitorNotActiveError
from ..models import (
    AUTHORITY_OPERATOR_DIALOG,
    ConditionRefreshResult,
    EventConfig,
    EventSourceInfo,
    EventStart,
    WriteAuthority,
)
from .event_service import LiveEvents

_logger = logging.getLogger(__name__)


class EventSession:
    """One event subscription, or none."""

    def __init__(self) -> None:
        self._events: LiveEvents | None = None

    @property
    def is_active(self) -> bool:
        return self._events is not None and self._events.is_active

    def _running(self) -> LiveEvents:
        if self._events is None:
            raise MonitorNotActiveError("No event subscription is running. Start event monitoring first.")
        return self._events

    async def start(
        self,
        client: Any,
        source_node_ids: list[str],
        config: EventConfig,
        *,
        emit: Callable[[Any], None],
    ) -> EventStart:
        """Replace any running event subscription with one for ``source_node_ids``."""
        await self.stop()
        events = LiveEvents(client, emit)
        start = await events.start(source_node_ids, config)
        self._events = events
        _logger.info(
            "Event subscription started: subscription %s, %d of %d source(s) accepted",
            start.subscription_id,
            sum(1 for source in start.sources if source.ok),
            len(start.sources),
        )
        return start

    async def add(self, source_node_ids: Iterable[str]) -> list[EventSourceInfo]:
        events = self._running()
        ids = list(source_node_ids)
        if not ids:
            return []
        return await events.add(ids)

    async def remove(self, handles: Iterable[int]) -> None:
        await self._running().remove(handles)

    async def refresh_conditions(self, *, reference: str = "") -> ConditionRefreshResult:
        """Ask the server to re-publish the retained conditions of the running subscription.

        The operator pressed the button in the Events tab, so the dialog is the
        authority; nothing is stored, acknowledged or changed by this call.
        """
        events = self._running()
        authorization = WriteAuthority(
            AUTHORITY_OPERATOR_DIALOG, reference or f"condition-refresh {events.subscription_id}"
        )
        return await events.refresh_conditions(authorization=authorization)

    async def stop(self) -> None:
        """Delete the subscription; safe to call when none runs."""
        events, self._events = self._events, None
        if events is not None:
            await events.stop()
