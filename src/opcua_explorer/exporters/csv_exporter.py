"""Flat CSV inventory export.

One row per node. Reference counts are computed from the reference list so the
inventory is useful without a second file. Values are only present when the
download explicitly included them; a refused or undecodable value keeps its
StatusCode in ``value_status``/``unavailable_attributes`` so it cannot be
mistaken for a value that was simply not requested.

The file is written as UTF-8 with BOM so that Excel on Windows detects the
encoding; descriptions with commas, quotes or newlines are quoted by the csv
module.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ExportError
from ..formatting import flatten_for_csv
from ..models import AddressSpace, NodeRecord

COLUMNS = [
    "node_id",
    "node_class",
    "browse_name",
    "browse_name_namespace_index",
    "display_name",
    "namespace_uri",
    "data_type",
    "data_type_name",
    "value_rank",
    "array_dimensions",
    "access_level",
    "access_level_names",
    "user_access_level",
    "user_access_level_names",
    "type_definition",
    "type_definition_name",
    "description",
    "value_status",
    "value_encoding",
    "value_variant_type",
    "value",
    "value_source_timestamp",
    "value_server_timestamp",
    "references_out",
    "references_in",
    "unavailable_attributes",
    "errors",
]


@dataclass(slots=True)
class CsvExportResult:
    path: Path
    rows: int
    bytes_written: int


def _unavailable_text(record: NodeRecord) -> str:
    """``Attribute=StatusCode`` pairs, sorted, for every attribute the server refused."""
    return ";".join(f"{name}={status}" for name, status in sorted(record.unavailable.items()))


def _row(record: NodeRecord, out_count: int, in_count: int, include_values: bool) -> dict[str, Any]:
    value = record.value if include_values else None
    refused_value = record.unavailable.get("Value")
    return {
        "node_id": record.node_id,
        "node_class": record.node_class,
        "browse_name": record.browse_name,
        "browse_name_namespace_index": record.browse_name_namespace_index,
        "display_name": record.display_name,
        "namespace_uri": record.namespace_uri or "",
        "data_type": record.data_type or "",
        "data_type_name": record.data_type_name or "",
        "value_rank": "" if record.value_rank is None else record.value_rank,
        "array_dimensions": "" if not record.array_dimensions else ";".join(str(item) for item in record.array_dimensions),
        "access_level": "" if record.access_level is None else record.access_level,
        "access_level_names": ";".join(record.access_level_names),
        "user_access_level": "" if record.user_access_level is None else record.user_access_level,
        "user_access_level_names": ";".join(record.user_access_level_names),
        "type_definition": record.type_definition or "",
        "type_definition_name": record.type_definition_name or "",
        "description": record.description or "",
        # A refused/undecodable Value keeps its StatusCode instead of looking absent.
        "value_status": value.status if value is not None else (refused_value or ""),
        "value_encoding": (
            value.encoding
            if value is not None
            else ("unavailable" if refused_value else "")
        ),
        "value_variant_type": "" if value is None else (value.variant_type or ""),
        "value": "" if value is None else flatten_for_csv(value.value),
        "value_source_timestamp": "" if value is None else (value.source_timestamp or ""),
        "value_server_timestamp": "" if value is None else (value.server_timestamp or ""),
        "references_out": out_count,
        "references_in": in_count,
        "unavailable_attributes": _unavailable_text(record),
        "errors": " | ".join(record.errors),
    }


def export_csv(snapshot: AddressSpace, path: str | os.PathLike[str]) -> CsvExportResult:
    """Write a flat node inventory as CSV, atomically replacing ``path``."""
    target = Path(path)

    out_counts: dict[str, int] = {}
    in_counts: dict[str, int] = {}
    for edge in snapshot.references:
        out_counts[edge.source] = out_counts.get(edge.source, 0) + 1
        in_counts[edge.target] = in_counts.get(edge.target, 0) + 1

    rows = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for node_id in sorted(snapshot.nodes):
                writer.writerow(
                    _row(
                        snapshot.nodes[node_id],
                        out_counts.get(node_id, 0),
                        in_counts.get(node_id, 0),
                        snapshot.values_included,
                    )
                )
                rows += 1
        tmp.replace(target)
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc

    return CsvExportResult(path=target, rows=rows, bytes_written=target.stat().st_size)
