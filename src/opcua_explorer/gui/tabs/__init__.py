"""Tab widgets of the main window."""

from __future__ import annotations

from .browser_tab import BrowserTab
from .connection_tab import ConnectionTab
from .export_tab import ExportTab
from .logs_tab import LogsTab
from .monitor_tab import MonitorTab
from .tests_tab import TestsTab
from .unified_monitor_tab import UnifiedMonitorTab

__all__ = [
    "BrowserTab",
    "ConnectionTab",
    "ExportTab",
    "LogsTab",
    "MonitorTab",
    "TestsTab",
    "UnifiedMonitorTab",
]
