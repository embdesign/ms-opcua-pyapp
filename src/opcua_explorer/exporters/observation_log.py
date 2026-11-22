"""Versioned JSON and CSV exports of the live-monitor and event tables (pure).

The Live monitor and Events tabs show bounded tables; this module turns the rows a
front end currently holds into one of the two formats the rest of the project
already speaks, so a captured log can be attached to a report, diffed or loaded by
another tool:

* **JSON** — a small versioned document (``format``, ``format_version``,
  ``exported_at_utc``, the endpoint, the rows). Each row is the canonical
  ``to_dict()`` of ``MonitorNotification`` / ``EventNotification``.
* **CSV** — one header row and one row per notification, UTF-8 with BOM (like the
  address-space inventory), quoted by the ``csv`` module.

These writers export exactly the rows they are given — the GUI passes the rows it
displays, so the table's bound is the export's bound — and never read, subscribe or
connect.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..errors import ExportError
from ..formatting import flatten_for_csv
from ..models import EventNotification, MonitorNotification, iso_utc, utc_now

MONITOR_LOG_FORMAT = "opcua-explorer-monitor-log"
EVENT_LOG_FORMAT = "opcua-explorer-event-log"
FORMAT_VERSION = 1

#: Columns of the notification-log CSV, in table order.
MONITOR_COLUMNS = [
    "received_at_utc",
    "sequence",
    "node_id",
    "status",
    "variant_type",
    "value",
    "source_timestamp",
    "server_timestamp",
    "truncated",
]

#: Columns of the event-log CSV, in table order.
EVENT_COLUMNS = [
    "received_at_utc",
    "sequence",
    "time",
    "event_type",
    "event_type_id",
    "source_node",
    "source_name",
    "severity",
    "message",
    "event_id",
    "condition_node_id",
    "fields",
]


def _monitor_row(notification: MonitorNotification) -> dict[str, Any]:
    return {
        "received_at_utc": iso_utc(notification.received_at),
        "sequence": notification.sequence,
        "node_id": notification.node_id,
        "status": notification.status,
        "variant_type": notification.variant_type or "",
        "value": flatten_for_csv(notification.value),
        "source_timestamp": notification.source_timestamp or "",
        "server_timestamp": notification.server_timestamp or "",
        "truncated": "true" if notification.truncated else "false",
    }


def _event_row(event: EventNotification) -> dict[str, Any]:
    return {
        "received_at_utc": iso_utc(event.received_at),
        "sequence": event.sequence,
        "time": event.time or "",
        "event_type": event.event_type,
        "event_type_id": event.event_type_id,
        "source_node": event.source_node,
        "source_name": event.source_name,
        "severity": "" if event.severity is None else event.severity,
        "message": event.message,
        "event_id": event.event_id,
        "condition_node_id": event.condition_node_id,
        "fields": flatten_for_csv(dict(event.fields)),
    }


def monitor_log_document(
    notifications: Sequence[MonitorNotification],
    *,
    endpoint_url: str = "",
    exported_at: Any = None,
) -> dict[str, Any]:
    """The canonical monitor-log document for the rows given (never a second read)."""
    return {
        "format": MONITOR_LOG_FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at_utc": iso_utc(exported_at or utc_now()),
        "endpoint_url": endpoint_url,
        "node_ids": sorted({notification.node_id for notification in notifications}),
        "count": len(notifications),
        "notifications": [notification.to_dict() for notification in notifications],
    }


def event_log_document(
    events: Sequence[EventNotification],
    *,
    endpoint_url: str = "",
    exported_at: Any = None,
) -> dict[str, Any]:
    """The canonical event-log document for the rows given."""
    return {
        "format": EVENT_LOG_FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at_utc": iso_utc(exported_at or utc_now()),
        "endpoint_url": endpoint_url,
        "count": len(events),
        "events": [event.to_dict() for event in events],
    }


def _csv_text(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def monitor_log_csv(notifications: Sequence[MonitorNotification]) -> str:
    """The notification log as CSV text (header included, no BOM)."""
    return _csv_text([_monitor_row(notification) for notification in notifications], MONITOR_COLUMNS)


def event_log_csv(events: Sequence[EventNotification]) -> str:
    """The event log as CSV text (header included, no BOM)."""
    return _csv_text([_event_row(event) for event in events], EVENT_COLUMNS)


def write_observation_log(
    path: str | os.PathLike[str],
    *,
    kind: str,
    payloads: Sequence[MonitorNotification] | Sequence[EventNotification],
    endpoint_url: str = "",
    exported_at: Any = None,
) -> Path:
    """Write one log file; ``.csv`` selects CSV, anything else the JSON document.

    ``kind`` is ``"monitor"`` or ``"events"`` and must match the payload type, so a
    notification can never be written into an event document.
    """
    target = Path(path)
    if kind == "monitor":
        if not all(isinstance(payload, MonitorNotification) for payload in payloads):
            raise ExportError("The monitor log can only contain monitor notifications.")
        document: dict[str, Any] = monitor_log_document(
            list(payloads), endpoint_url=endpoint_url, exported_at=exported_at
        )
        csv_text = monitor_log_csv(list(payloads))
    elif kind == "events":
        if not all(isinstance(payload, EventNotification) for payload in payloads):
            raise ExportError("The event log can only contain event notifications.")
        document = event_log_document(
            list(payloads), endpoint_url=endpoint_url, exported_at=exported_at
        )
        csv_text = event_log_csv(list(payloads))
    else:
        raise ExportError(f"{kind!r} is not a log this exporter writes.")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix.lower() == ".csv":
            target.write_text(csv_text, encoding="utf-8-sig", newline="")
        else:
            target.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc
    return target


__all__ = [
    "EVENT_COLUMNS",
    "EVENT_LOG_FORMAT",
    "FORMAT_VERSION",
    "MONITOR_COLUMNS",
    "MONITOR_LOG_FORMAT",
    "event_log_csv",
    "event_log_document",
    "monitor_log_csv",
    "monitor_log_document",
    "write_observation_log",
]
