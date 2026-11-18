"""Tab widgets of the main window."""

from __future__ import annotations

from .browser_tab import BrowserTab
from .compare_tab import CompareTab
from .connection_tab import ConnectionTab
from .events_tab import EventsTab
from .export_tab import ExportTab
from .history_tab import HistoryTab
from .logs_tab import LogsTab
from .monitor_tab import MonitorTab
from .sequences_tab import SequencesTab
from .server_tab import ServerTab
from .tests_tab import TestsTab
from .unified_monitor_tab import UnifiedMonitorTab
from .write_tab import WriteCallTab

__all__ = [
    "BrowserTab",
    "CompareTab",
    "ConnectionTab",
    "EventsTab",
    "ExportTab",
    "HistoryTab",
    "LogsTab",
    "MonitorTab",
    "SequencesTab",
    "ServerTab",
    "TestsTab",
    "UnifiedMonitorTab",
    "WriteCallTab",
]
