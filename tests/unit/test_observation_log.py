"""Unit tests for the monitor/event log exporters (pure, no Qt, no server).

The GUI and the two headless commands write the same documents, so these tests pin
down the shape, the CSV quoting and the refusals.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from opcua_explorer.errors import ExportError
from opcua_explorer.exporters.observation_log import (
    EVENT_LOG_FORMAT,
    FORMAT_VERSION,
    MONITOR_COLUMNS,
    MONITOR_LOG_FORMAT,
    event_log_csv,
    event_log_document,
    monitor_log_csv,
    monitor_log_document,
    write_observation_log,
)
from opcua_explorer.models import EventNotification, MonitorNotification

_MOMENT = datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC)


def _notification(sequence: int = 1, *, value: object = 21.5, node: str = "ns=2;s=Temperature"):
    return MonitorNotification(
        sequence=sequence,
        node_id=node,
        value=value,
        value_text=str(value),
        variant_type="Double",
        status="Good",
        source_timestamp="2026-11-22T09:59:59.000Z",
        received_at=_MOMENT,
    )


def _event(sequence: int = 1, *, message: str = "pump stopped"):
    return EventNotification(
        sequence=sequence,
        event_type="BaseEventType",
        event_type_id="i=2041",
        source_node="i=2253",
        source_name="Server",
        severity=300,
        message=message,
        event_id="0102ff",
        condition_node_id="ns=2;s=Condition",
        fields={"Message": message, "Severity": "300"},
        received_at=_MOMENT,
    )


def test_monitor_document_is_versioned_and_names_the_nodes() -> None:
    document = monitor_log_document(
        [_notification(1), _notification(2, node="ns=2;s=Speed")],
        endpoint_url="opc.tcp://plc:4840",
        exported_at=_MOMENT,
    )

    assert document["format"] == MONITOR_LOG_FORMAT
    assert document["format_version"] == FORMAT_VERSION
    assert document["endpoint_url"] == "opc.tcp://plc:4840"
    assert document["exported_at_utc"] == "2026-11-22T10:00:00.000Z"
    assert document["count"] == 2
    assert document["node_ids"] == ["ns=2;s=Speed", "ns=2;s=Temperature"]
    # Each row is the canonical model document, not a second format.
    assert document["notifications"][0] == _notification(1).to_dict()


def test_monitor_csv_has_the_columns_of_the_table_and_quotes_values() -> None:
    text = monitor_log_csv([_notification(1, value={"a": 1, "b": "x,y"})])

    lines = text.splitlines()
    assert lines[0].split(",") == MONITOR_COLUMNS
    # A structure is flattened to JSON and quoted by the csv module.
    assert '"{"' in lines[1] or '"{""a"":1' in lines[1]
    assert lines[1].count('"') >= 2


def test_event_document_and_csv_keep_the_condition_identity() -> None:
    document = event_log_document([_event()], endpoint_url="opc.tcp://plc:4840", exported_at=_MOMENT)
    assert document["format"] == EVENT_LOG_FORMAT
    assert document["count"] == 1
    assert document["events"][0]["condition_node_id"] == "ns=2;s=Condition"

    text = event_log_csv([_event(message='valve "closed", again')])
    assert "condition_node_id" in text.splitlines()[0]
    assert '"valve ""closed"", again"' in text


def test_an_empty_log_still_has_a_header_and_a_count() -> None:
    assert monitor_log_document([])["count"] == 0
    assert len(monitor_log_csv([]).splitlines()) == 1
    assert len(event_log_csv([]).splitlines()) == 1


def test_write_dispatch_follows_the_suffix(tmp_path) -> None:
    json_path = write_observation_log(
        tmp_path / "log.json", kind="monitor", payloads=[_notification()], endpoint_url="opc.tcp://x"
    )
    csv_path = write_observation_log(tmp_path / "log.csv", kind="monitor", payloads=[_notification()])

    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["format"] == MONITOR_LOG_FORMAT
    assert document["endpoint_url"] == "opc.tcp://x"
    # The CSV is written with a BOM so the existing spreadsheet workflow works.
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert csv_path.read_text(encoding="utf-8-sig").splitlines()[0].startswith("received_at_utc")


def test_a_log_refuses_the_wrong_payload_or_kind(tmp_path) -> None:
    with pytest.raises(ExportError, match="monitor notifications"):
        write_observation_log(tmp_path / "x.json", kind="monitor", payloads=[_event()])
    with pytest.raises(ExportError, match="event notifications"):
        write_observation_log(tmp_path / "x.json", kind="events", payloads=[_notification()])
    with pytest.raises(ExportError, match="not a log"):
        write_observation_log(tmp_path / "x.json", kind="history", payloads=[])


def test_a_missing_folder_is_created(tmp_path) -> None:
    target = tmp_path / "nested" / "logs" / "events.json"

    written = write_observation_log(target, kind="events", payloads=[_event()])

    assert written == target
    assert target.exists()
