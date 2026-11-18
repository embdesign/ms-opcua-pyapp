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
DEFAULT_EVENT_KEEP_ROWS = 2_000

#: Default and hard upper bound for one raw-history read.
DEFAULT_HISTORY_MAX_VALUES = 1_000
MAX_HISTORY_MAX_VALUES = 10_000

DEFAULT_PUBLISHING_INTERVAL_MS = 500.0
DEFAULT_SAMPLING_INTERVAL_MS = 250.0
#: Event subscriptions publish whole event fields, so a slower default than a
#: data-change subscription is deliberate.
DEFAULT_EVENT_PUBLISHING_INTERVAL_MS = 1_000.0
DEFAULT_NOTIFICATION_WAIT_SECONDS = 5.0
DEFAULT_LATENCY_SAMPLES = 10

DEFAULT_CONCURRENCY = 4
DEFAULT_BATCH_SIZE = 50

#: MaxAge of an explicit Value read in milliseconds. ``0`` lets the server answer
#: from a cache; the protocol maximum (uint32) means "come from the device".
DEFAULT_READ_MAX_AGE_MS = 0
MAX_READ_MAX_AGE_MS = 4_294_967_295

#: Rows kept per difference bucket by the address-space comparison; the counts
#: stay complete, only the listed rows stop.
DEFAULT_MAX_COMPARISON_DIFFERENCES = 2_000
