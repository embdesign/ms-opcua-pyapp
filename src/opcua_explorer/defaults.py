"""Numeric defaults shared by the model, project files and the GUI.

These values used to be repeated in ``models.py``, ``project.py``, ``nodeset2.py``
and several tabs. One definition keeps a saved project, a freshly opened tab and
a normalised config in agreement.

This module is Qt-free and asyncua-free.
"""

from __future__ import annotations

DEFAULT_MAX_NODES = 20_000
DEFAULT_MAX_DEPENDENCIES = 2_000
DEFAULT_MONITOR_KEEP_ROWS = 2_000

DEFAULT_PUBLISHING_INTERVAL_MS = 500.0
DEFAULT_SAMPLING_INTERVAL_MS = 250.0
DEFAULT_NOTIFICATION_WAIT_SECONDS = 5.0
DEFAULT_LATENCY_SAMPLES = 10

DEFAULT_CONCURRENCY = 4
DEFAULT_BATCH_SIZE = 50
