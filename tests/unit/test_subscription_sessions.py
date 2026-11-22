"""Subscription collaborators keep the façade's public contract."""

from __future__ import annotations

import pytest

from opcua_explorer.errors import MonitorNotActiveError
from opcua_explorer.opcua.event_session import EventSession
from opcua_explorer.opcua.monitor_session import MonitorSession


def test_monitor_session_starts_inactive() -> None:
    session = MonitorSession()
    assert session.is_active is False
    assert session.subscription_id is None
    assert session.items_snapshot() == []


def test_event_session_starts_inactive() -> None:
    assert EventSession().is_active is False


async def test_monitor_operations_require_a_running_subscription() -> None:
    session = MonitorSession()
    with pytest.raises(MonitorNotActiveError):
        await session.add(["i=2258"])
    with pytest.raises(MonitorNotActiveError):
        await session.remove([1])
    with pytest.raises(MonitorNotActiveError):
        await session.set_monitoring_mode("Reporting")
    with pytest.raises(MonitorNotActiveError):
        await session.apply_intervals(publishing_interval_ms=1000.0, sampling_interval_ms=0.0)


async def test_event_operations_require_a_running_subscription() -> None:
    session = EventSession()
    with pytest.raises(MonitorNotActiveError):
        await session.add(["i=2253"])
    with pytest.raises(MonitorNotActiveError):
        await session.remove([1])


async def test_stopping_an_idle_subscription_is_safe() -> None:
    await MonitorSession().stop()
    await EventSession().stop()
