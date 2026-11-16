"""Read an exported JSON address-space document back into a snapshot.

The JSON writer in :mod:`opcua_explorer.exporters.json_exporter` is the only
persisted address-space format; being able to read it again turns a saved export
into a comparison baseline, so a device that is no longer reachable can still be
compared with its replacement (``opcua-compare --reference-file …`` and the
GUI **Compare** tab).

The loader is deliberately strict about identity — ``format`` and
``format_version`` — and tolerant about *extra* keys, so a document written by a
newer build still loads as long as the fields it needs are present. It never
invents data: a node id, node class or reference that is missing stays missing
and is reported by the comparison as an incomplete snapshot, not repaired here.

This module is Qt-free and asyncua-free, like the writers next to it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import ExportError
from ..models import AddressSpace, ConnectionInfo, Edge, NamespaceEntry, NodeRecord, ValueSnapshot
from .json_exporter import FORMAT_NAME, FORMAT_VERSION


def _as_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExportError(f"{what} is not a JSON object.")
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _value_snapshot(raw: Any) -> ValueSnapshot | None:
    if not isinstance(raw, Mapping):
        return None
    return ValueSnapshot(
        value=raw.get("value"),
        status=str(raw.get("status") or "Good"),
        variant_type=_text(raw.get("variant_type")),
        source_timestamp=_text(raw.get("source_timestamp")),
        server_timestamp=_text(raw.get("server_timestamp")),
        encoding=str(raw.get("encoding") or "json"),
        unsupported=[dict(item) for item in raw.get("unsupported_encodings") or () if isinstance(item, Mapping)],
    )


def _connection(raw: Any) -> ConnectionInfo | None:
    if not isinstance(raw, Mapping):
        return None
    auth_mode = raw.get("authentication") or raw.get("auth_mode")
    return ConnectionInfo(
        endpoint_url=str(raw.get("endpoint_url") or ""),
        security_policy_uri=str(raw.get("security_policy_uri") or ""),
        security_policy_label=str(raw.get("security_policy_label") or ""),
        security_mode=str(raw.get("security_mode") or ""),
        auth_mode=str(auth_mode or "unknown"),
        application_uri=_text(raw.get("application_uri")),
        session_timeout_ms=raw.get("session_timeout_ms"),
        server_application_name=_text(raw.get("server_application_name")),
        server_application_uri=_text(raw.get("server_application_uri")),
        product_uri=_text(raw.get("product_uri")),
        manufacturer_name=_text(raw.get("manufacturer_name")),
        software_version=_text(raw.get("software_version")),
        build_number=_text(raw.get("build_number")),
        server_certificate_sha256=_text(raw.get("server_certificate_sha256")),
    )


def _node_record(node_id: str, raw: Any) -> NodeRecord:
    data = _as_mapping(raw, f"Node {node_id!r}")
    record = NodeRecord(node_id=str(data.get("node_id") or node_id))
    record.node_class = str(data.get("node_class") or "Unspecified")
    record.browse_name = str(data.get("browse_name") or "")
    record.browse_name_namespace_index = int(data.get("browse_name_namespace_index") or 0)
    record.display_name = str(data.get("display_name") or "")
    record.namespace_uri = _text(data.get("namespace_uri"))
    record.description = _text(data.get("description"))
    record.data_type = _text(data.get("data_type"))
    record.data_type_name = _text(data.get("data_type_name"))
    record.value_rank = None if data.get("value_rank") is None else int(data["value_rank"])
    dimensions = data.get("array_dimensions")
    record.array_dimensions = None if dimensions is None else [int(item) for item in dimensions]
    record.access_level = None if data.get("access_level") is None else int(data["access_level"])
    record.access_level_names = [str(item) for item in data.get("access_level_names") or ()]
    record.user_access_level = None if data.get("user_access_level") is None else int(data["user_access_level"])
    record.user_access_level_names = [str(item) for item in data.get("user_access_level_names") or ()]
    record.type_definition = _text(data.get("type_definition"))
    record.type_definition_name = _text(data.get("type_definition_name"))
    record.value = _value_snapshot(data.get("value"))
    record.unavailable = {
        str(name): str(status) for name, status in (data.get("unavailable_attributes") or {}).items()
    }
    record.errors = [str(item) for item in data.get("errors") or ()]
    return record


def snapshot_from_document(document: Any, *, name: str | None = None) -> AddressSpace:
    """Rebuild an :class:`AddressSpace` from a JSON address-space document.

    ``name`` is used for the endpoint label when the document does not carry one
    (typically the file name), so a report never shows an empty side.
    """
    data = _as_mapping(document, "The document")
    if data.get("format") != FORMAT_NAME:
        raise ExportError(
            f"Not an OPC UA address-space document (format={data.get('format')!r}, expected {FORMAT_NAME!r})."
        )
    version = data.get("format_version")
    if version != FORMAT_VERSION:
        raise ExportError(
            f"Unsupported format_version {version!r}; this build reads version {FORMAT_VERSION}."
        )

    nodes_document = data.get("nodes")
    if not isinstance(nodes_document, Mapping) or not nodes_document:
        raise ExportError("The document contains no nodes, so there is nothing to compare.")

    nodes = {str(node_id): _node_record(str(node_id), raw) for node_id, raw in nodes_document.items()}

    namespaces: list[NamespaceEntry] = []
    for entry in data.get("namespaces") or ():
        if not isinstance(entry, Mapping):
            continue
        namespaces.append(NamespaceEntry(index=int(entry.get("index") or 0), uri=str(entry.get("uri") or "")))

    references: list[Edge] = []
    for entry in data.get("references") or ():
        if not isinstance(entry, Mapping):
            continue
        source = _text(entry.get("source"))
        target = _text(entry.get("target"))
        if not source or not target:
            raise ExportError("A reference is missing its source or target NodeId.")
        references.append(
            Edge(
                source=source,
                target=target,
                reference_type=str(entry.get("reference_type") or ""),
                reference_type_name=_text(entry.get("reference_type_name")),
            )
        )

    source_document = data.get("source") if isinstance(data.get("source"), Mapping) else {}
    endpoint_url = _text(source_document.get("endpoint_url")) or name or "imported snapshot"
    snapshot = AddressSpace(
        endpoint_url=endpoint_url,
        roots=[str(item) for item in source_document.get("download_roots") or ()],
        nodes=nodes,
        references=references,
        namespaces=namespaces,
        connection=_connection(source_document.get("connection")),
        values_included=bool(source_document.get("values_included")),
        limitations=[str(item) for item in source_document.get("limitations") or ()],
        inaccessible={
            str(entry.get("node_id")): str(entry.get("status") or "")
            for entry in data.get("inaccessible_nodes") or ()
            if isinstance(entry, Mapping) and entry.get("node_id")
        },
        unsupported_encodings=[
            dict(item) for item in data.get("unsupported_encodings") or () if isinstance(item, Mapping)
        ],
        errors=[str(item) for item in data.get("errors") or ()],
    )
    # ``summary`` is the only place that records whether the download was cut short;
    # without it a truncated export would look like a complete model.
    summary_document = data.get("summary") if isinstance(data.get("summary"), Mapping) else {}
    snapshot.limits_hit = [str(item) for item in summary_document.get("limits_hit") or ()]
    snapshot.cancelled = bool(summary_document.get("cancelled"))
    snapshot.finished_at = snapshot.started_at
    return snapshot


def load_json_snapshot(path: str | os.PathLike[str]) -> AddressSpace:
    """Read ``path`` and return the snapshot it describes (raises :class:`ExportError`)."""
    target = Path(path)
    try:
        # utf-8-sig also accepts a plain UTF-8 file written by another tool.
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ExportError(f"{target} could not be read: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExportError(f"{target} is not valid JSON: {exc}") from exc
    return snapshot_from_document(document, name=str(target))
