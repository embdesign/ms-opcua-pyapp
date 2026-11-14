"""JSON address-space export.

Schema (``format_version`` 1)
-----------------------------
::

    {
      "format": "opcua-address-space-snapshot",
      "format_version": 1,
      "generator": {...},
      "generated_at_utc": "...,",
      "source": {"endpoint_url": ..., "connection": {...}, "download_roots": [...],
                 "values_included": false, "limitations": [...]},
      "namespaces": [{"index": 0, "uri": "http://opcfoundation.org/UA/"}],
      "summary": {...},
      "nodes": {"ns=2;i=1": {...}},
      "references": [{"source": ..., "target": ..., "reference_type": ...,
                      "reference_type_name": ..., "target_included": true}],
      "inaccessible_nodes": [{"node_id": ..., "status": ...}],
      "unsupported_encodings": [...],
      "errors": [...]
    }

* Nodes are keyed by their session NodeId string; the namespace **URI** table is
  always included because namespace indexes are session-local and can change
  between connections.
* References are a separate, de-duplicated list of **forward** edges. An inverse
  reference observed while browsing is normalised, so the same relationship is
  never exported twice.
* ``target_included`` is false when a reference points to a node outside the
  downloaded region (depth/node limit, namespace filter or access denied).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import ExportError
from ..models import AddressSpace, iso_utc, utc_now

FORMAT_NAME = "opcua-address-space-snapshot"
FORMAT_VERSION = 1


@dataclass(slots=True)
class JsonExportResult:
    path: Path
    nodes: int
    references: int
    bytes_written: int


def snapshot_to_document(snapshot: AddressSpace) -> dict[str, Any]:
    """Build the canonical JSON document for a snapshot (no file I/O)."""
    included = set(snapshot.nodes)

    nodes: dict[str, Any] = {}
    for node_id in sorted(snapshot.nodes):
        data = snapshot.nodes[node_id].to_dict()
        if not snapshot.values_included:
            # Defensive contract: values never leave the process unless the
            # download was explicitly configured to include them.
            data["value"] = None
        nodes[node_id] = data

    # De-duplicate on the normalised key; traversal already does this, but a
    # snapshot assembled by other means must not produce duplicate edges.
    unique_edges: dict[tuple[str, str, str], Any] = {}
    for edge in snapshot.references:
        unique_edges.setdefault(edge.key, edge)

    references = []
    for edge in sorted(unique_edges.values(), key=lambda item: (item.source, item.reference_type, item.target)):
        references.append(
            {
                "source": edge.source,
                "target": edge.target,
                "reference_type": edge.reference_type,
                "reference_type_name": edge.reference_type_name,
                "target_included": edge.target in included,
            }
        )

    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "generator": {"name": "opcua-explorer", "version": __version__},
        "generated_at_utc": iso_utc(utc_now()),
        "source": {
            "endpoint_url": snapshot.endpoint_url,
            "connection": None if snapshot.connection is None else snapshot.connection.to_dict(),
            "download_roots": list(snapshot.roots),
            "values_included": snapshot.values_included,
            "traversal": {
                "max_depth": snapshot.config.max_depth,
                "max_nodes": snapshot.config.max_nodes,
                "concurrency": snapshot.config.concurrency,
                "batch_size": snapshot.config.batch_size,
                "namespace_filter": (
                    None if snapshot.config.namespace_filter is None else sorted(snapshot.config.namespace_filter)
                ),
                "include_values": snapshot.config.include_values,
            },
            "limitations": list(snapshot.limitations),
        },
        "namespaces": [entry.to_dict() for entry in sorted(snapshot.namespaces, key=lambda item: item.index)],
        "summary": snapshot.summary(),
        "nodes": nodes,
        "references": references,
        "inaccessible_nodes": [
            {"node_id": node_id, "status": status} for node_id, status in sorted(snapshot.inaccessible.items())
        ],
        "unsupported_encodings": list(snapshot.unsupported_encodings),
        "errors": list(snapshot.errors),
    }


def export_json(snapshot: AddressSpace, path: str | os.PathLike[str], *, indent: int = 2) -> JsonExportResult:
    """Write the snapshot as JSON, atomically replacing ``path``."""
    target = Path(path)
    document = snapshot_to_document(snapshot)
    text = json.dumps(document, indent=indent, ensure_ascii=False, default=str)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc

    return JsonExportResult(
        path=target,
        nodes=len(document["nodes"]),
        references=len(document["references"]),
        bytes_written=len(text.encode("utf-8")),
    )
