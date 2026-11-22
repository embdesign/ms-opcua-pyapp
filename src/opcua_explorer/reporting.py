"""Test report writers: JSON and a self-contained HTML document.

Both formats carry the same facts: endpoint, selected security configuration,
test configuration, UTC timestamps and durations, per-check status with OPC UA
StatusCodes, coverage limitations and inaccessible nodes.

The HTML writer escapes every dynamic value (server-provided strings such as
display names or error text end up in the report) and embeds no external assets,
so a report can be attached to a ticket or opened offline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from .compare import (
    COMPARISON_FORMAT,
    COMPARISON_FORMAT_VERSION,
    COMPARISON_LIMITATION_NOTE,
    KIND_CHANGED,
    KIND_EXTRA_IN_CANDIDATE,
    KIND_LABELS,
    KIND_MISSING_IN_CANDIDATE,
    KIND_NOT_COMPARABLE,
    ComparisonResult,
)
from .errors import ExportError
from .models import (
    FUNCTIONAL_TEST_DISCLAIMER,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    TestRun,
    iso_utc,
    utc_now,
)

REPORT_FORMAT = "opcua-functional-test-report"
REPORT_VERSION = 1


@dataclass(slots=True)
class ReportResult:
    path: Path
    bytes_written: int
    checks: int


def run_to_document(run: TestRun) -> dict[str, Any]:
    """Canonical JSON document for a test run."""
    connection = run.connection.to_dict() if run.connection is not None else None
    return {
        "format": REPORT_FORMAT,
        "format_version": REPORT_VERSION,
        "disclaimer": FUNCTIONAL_TEST_DISCLAIMER,
        "generated_at_utc": iso_utc(utc_now()),
        "environment": dict(run.environment),
        "server": {
            "endpoint_url": run.endpoint_url,
            "connection": connection,
        },
        "configuration": run.config.to_dict(),
        "timing": {
            "started_at_utc": iso_utc(run.started_at),
            "finished_at_utc": iso_utc(run.finished_at),
            "duration_seconds": None if run.duration_seconds is None else round(run.duration_seconds, 3),
        },
        "summary": run.summary(),
        "results": [check.to_dict() for check in run.checks],
        "coverage_limitations": list(run.coverage_limitations),
        "inaccessible_nodes": [
            {"node_id": node_id, "status": status} for node_id, status in sorted(run.inaccessible_nodes.items())
        ],
        "notes": list(run.notes),
        "cancelled": run.cancelled,
    }


def write_json_report(run: TestRun, path: str | os.PathLike[str], *, indent: int = 2) -> ReportResult:
    """Write the JSON report atomically."""
    target = Path(path)
    document = run_to_document(run)
    text = json.dumps(document, indent=indent, ensure_ascii=False, default=str)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc
    return ReportResult(path=target, bytes_written=len(text.encode("utf-8")), checks=len(document["results"]))


_STATUS_LABELS = {STATUS_PASSED: "passed", STATUS_FAILED: "failed", STATUS_SKIPPED: "skipped"}

_STYLE = """
:root {
  color-scheme: light;
  --text: #1b2533;
  --muted: #5b6a7d;
  --border: #d9dfe7;
  --surface: #ffffff;
  --page: #f2f4f7;
  --accent: #14639e;
  --passed-bg: #e2f4ea; --passed-fg: #17603a;
  --failed-bg: #fdeceb; --failed-fg: #8c1d18;
  --skipped-bg: #fff3d6; --skipped-fg: #7a5200;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 64px;
  background: var(--page); color: var(--text);
  font: 14px/1.5 "Segoe UI", "Noto Sans", "DejaVu Sans", system-ui, sans-serif;
}
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 32px 0 10px; }
.subtitle { color: var(--muted); margin: 0 0 16px; }
.disclaimer {
  background: #fff6e0; border: 1px solid #f0d9a6; color: #7a5200;
  padding: 10px 14px; border-radius: 8px; margin: 16px 0 24px; font-weight: 600;
}
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 16px;
}
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin: 8px 0 0; }
.tile {
  flex: 1 1 150px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px;
}
.tile .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.tile .value { font-size: 26px; font-weight: 700; margin-top: 2px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { background: #eef2f7; color: #42536a; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
tr:last-child td { border-bottom: none; }
tbody tr:nth-child(even) { background: #f7f9fc; }
code, pre { font-family: "JetBrains Mono", "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; }
pre {
  background: #f7f9fc; border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-word;
}
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-weight: 700; font-size: 12px; }
.pill.passed { background: var(--passed-bg); color: var(--passed-fg); }
.pill.failed { background: var(--failed-bg); color: var(--failed-fg); }
.pill.skipped { background: var(--skipped-bg); color: var(--skipped-fg); }
.check { margin-bottom: 14px; }
.check h3 { font-size: 15px; margin: 0 0 4px; }
.meta { color: var(--muted); font-size: 12px; }
.footer { color: var(--muted); font-size: 12px; margin-top: 32px; }
a { color: var(--accent); }
"""


def _pill(status: str) -> str:
    label = _STATUS_LABELS.get(status, status)
    return f'<span class="pill {escape(status)}">{escape(label)}</span>'


def _row(cells: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def _definition_table(rows: list[tuple[str, Any]]) -> str:
    body = "".join(
        f"<tr><th scope=\"row\">{escape(str(label))}</th><td>{escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def _format_data(data: dict[str, Any]) -> str:
    if not data:
        return ""
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - runner data is JSON-safe
        text = str(data)
    return f"<pre>{escape(text)}</pre>"


def _check_section(check: Any) -> str:
    parts = [
        f'<div class="check" id="check-{escape(check.id)}">',
        f"<h3>{escape(check.name)} {_pill(check.status)}</h3>",
        f'<div class="meta">{escape(check.category)} · {check.duration_ms:.1f} ms · '
        f"started {escape(check.started_at.isoformat(timespec='milliseconds'))}</div>",
    ]
    if check.detail:
        parts.append(f"<p>{escape(check.detail)}</p>")
    if check.error:
        parts.append(f"<p><strong>Error:</strong> {escape(check.error)}</p>")
    if check.notes:
        items = "".join(f"<li>{escape(note)}</li>" for note in check.notes)
        parts.append(f"<ul>{items}</ul>")
    if check.status_codes:
        rows = "".join(
            _row(
                [
                    escape(str(entry.get("operation", ""))),
                    f"<code>{escape(str(entry.get('status', '')))}</code>",
                    escape(str(entry.get("node_id", ""))),
                ]
            )
            for entry in check.status_codes
        )
        parts.append(
            "<table><thead><tr><th>Operation</th><th>StatusCode</th><th>Node</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    if check.data:
        parts.append(_format_data(check.data))
    parts.append("</div>")
    return "".join(parts)


def render_html(run: TestRun) -> str:
    """Render the report as a standalone HTML document."""
    summary = run.summary()
    connection = run.connection.to_dict() if run.connection is not None else {}

    server_rows: list[tuple[str, Any]] = [
        ("Endpoint", run.endpoint_url),
        ("Security policy", connection.get("security_policy_label", "unknown")),
        ("Message security mode", connection.get("security_mode", "unknown")),
        ("Authentication", connection.get("authentication", "unknown")),
        ("Server application", connection.get("server_application_name") or "unknown"),
        ("Manufacturer", connection.get("manufacturer_name") or "unknown"),
        ("Software version", connection.get("software_version") or "unknown"),
        ("Session timeout", f"{connection.get('session_timeout_ms', 'unknown')} ms"),
        ("Server certificate SHA-256", connection.get("server_certificate_sha256") or "not applicable"),
    ]

    config_rows = [(key, value) for key, value in run.config.to_dict().items()]

    result_rows = "".join(
        _row(
            [
                str(index),
                f'<a href="#check-{escape(check.id)}">{escape(check.name)}</a>',
                escape(check.category),
                _pill(check.status),
                f"{check.duration_ms:.1f}",
                escape(check.detail),
            ]
        )
        for index, check in enumerate(run.checks, start=1)
    )

    limitation_items = "".join(f"<li>{escape(item)}</li>" for item in run.coverage_limitations)
    note_items = "".join(f"<li>{escape(item)}</li>" for item in run.notes)
    inaccessible_rows = "".join(
        _row([f"<code>{escape(node_id)}</code>", escape(status)])
        for node_id, status in sorted(run.inaccessible_nodes.items())
    )

    environment_rows = "".join(
        f"<tr><th scope=\"row\">{escape(key)}</th><td>{escape(str(value))}</td></tr>"
        for key, value in sorted(run.environment.items())
    )

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>OPC UA functional test report</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        "<h1>OPC UA functional test report</h1>",
        f'<p class="subtitle">{escape(run.endpoint_url)} · '
        f"generated {escape(str(iso_utc(utc_now())))}</p>",
        f'<p class="disclaimer">{escape(FUNCTIONAL_TEST_DISCLAIMER)}</p>',
        '<div class="tiles">',
        f'<div class="tile"><div class="label">Checks</div><div class="value">{summary["total"]}</div></div>',
        f'<div class="tile"><div class="label">Passed</div><div class="value">{summary["passed"]}</div></div>',
        f'<div class="tile"><div class="label">Failed</div><div class="value">{summary["failed"]}</div></div>',
        f'<div class="tile"><div class="label">Skipped</div><div class="value">{summary["skipped"]}</div></div>',
        f'<div class="tile"><div class="label">Duration</div>'
        f'<div class="value">{summary["duration_seconds"]} s</div></div>',
        "</div>",
        "<h2>Server and security</h2>",
        f'<div class="card">{_definition_table(server_rows)}</div>',
        "<h2>Test configuration</h2>",
        f'<div class="card">{_definition_table(config_rows)}</div>',
        "<h2>Results</h2>",
        '<div class="card"><table><thead><tr><th>#</th><th>Check</th><th>Category</th><th>Status</th>'
        "<th>Duration (ms)</th><th>Detail</th></tr></thead>"
        f"<tbody>{result_rows}</tbody></table></div>",
        "<h2>Check details</h2>",
        f'<div class="card">{"".join(_check_section(check) for check in run.checks)}</div>',
        "<h2>Coverage limitations</h2>",
        f'<div class="card"><ul>{limitation_items}</ul></div>',
    ]
    if inaccessible_rows:
        parts.extend(
            [
                "<h2>Inaccessible nodes</h2>",
                '<div class="card"><table><thead><tr><th>NodeId</th><th>StatusCode</th></tr></thead>'
                f"<tbody>{inaccessible_rows}</tbody></table></div>",
            ]
        )
    if note_items:
        parts.extend(["<h2>Notes</h2>", f'<div class="card"><ul>{note_items}</ul></div>'])
    parts.extend(
        [
            "<h2>Environment</h2>",
            f'<div class="card"><table><tbody>{environment_rows}</tbody></table></div>',
            f'<p class="footer">Timing: {escape(str(summary["started_at_utc"]))} → '
            f'{escape(str(summary["finished_at_utc"]))} (UTC). '
            + ("This run was cancelled; results are partial." if run.cancelled else "")
            + "</p>",
            "</main></body></html>",
        ]
    )
    return "\n".join(parts)


def write_html_report(run: TestRun, path: str | os.PathLike[str]) -> ReportResult:
    """Write the HTML report atomically."""
    target = Path(path)
    text = render_html(run)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc
    return ReportResult(path=target, bytes_written=len(text.encode("utf-8")), checks=len(run.checks))


# ---------------------------------------------------------------------------
# Address-space comparison reports
# ---------------------------------------------------------------------------

COMPARISON_REPORT_FORMAT = COMPARISON_FORMAT
COMPARISON_REPORT_VERSION = COMPARISON_FORMAT_VERSION

_BUCKETS: tuple[tuple[str, str], ...] = (
    (KIND_MISSING_IN_CANDIDATE, "Only on the reference"),
    (KIND_EXTRA_IN_CANDIDATE, "Only on the candidate"),
    (KIND_CHANGED, "Different attributes"),
    (KIND_NOT_COMPARABLE, "Not comparable"),
)


@dataclass(slots=True)
class ComparisonReport:
    path: Path
    bytes_written: int
    differences: int


def comparison_to_document(result: ComparisonResult) -> dict[str, Any]:
    """Canonical JSON document for a comparison (versioned by ``compare.py``)."""
    document = result.to_dict()
    document["generated_at_utc"] = iso_utc(utc_now())
    document["disclaimer"] = COMPARISON_LIMITATION_NOTE
    return document


def write_comparison_json(
    result: ComparisonResult,
    path: str | os.PathLike[str],
    *,
    indent: int = 2,
) -> ComparisonReport:
    """Write the comparison JSON report atomically."""
    target = Path(path)
    document = comparison_to_document(result)
    text = json.dumps(document, indent=indent, ensure_ascii=False, default=str)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc
    return ComparisonReport(path=target, bytes_written=len(text.encode("utf-8")), differences=result.difference_count)


def _comparison_value(value: Any) -> str:
    """Render one attribute value for the HTML table (scalars inline, structures as text)."""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - snapshots are JSON-safe
            text = str(value)
        return f"<code>{escape(text)}</code>"
    if value is None:
        return '<span class="meta">not set</span>'
    return f"<code>{escape(str(value))}</code>"


def _comparison_attribute_rows(difference: Any) -> str:
    if not difference.attributes:
        return '<span class="meta">no attribute detail</span>'
    rows = "".join(
        f"<tr><th scope=\"row\">{escape(item.attribute)}</th>"
        f"<td>{_comparison_value(item.reference)}</td>"
        f"<td>{_comparison_value(item.candidate)}</td>"
        f"<td>{escape(item.note)}</td></tr>"
        for item in difference.attributes
    )
    return f"<table><thead><tr><th>Attribute</th><th>Reference</th><th>Candidate</th><th>Note</th></tr></thead><tbody>{rows}</tbody></table>"


def _comparison_bucket_table(items: list[Any]) -> str:
    rows = "".join(
        _row(
            [
                escape(item.browse_path) or '<span class="meta">(no path)</span>',
                escape(item.node_class),
                f"<code>{escape(item.reference_node_id or '—')}</code>",
                f"<code>{escape(item.candidate_node_id or '—')}</code>",
                escape(item.note),
                _comparison_attribute_rows(item),
            ]
        )
        for item in items
    )
    return (
        "<table><thead><tr><th>Browse path</th><th>Node class</th><th>Reference NodeId</th>"
        "<th>Candidate NodeId</th><th>Note</th><th>Attributes</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def render_comparison_html(result: ComparisonResult) -> str:
    """Render the comparison as a standalone HTML document."""
    tiles = [
        ("Matched", result.matched),
        ("Missing", len(result.missing_in_candidate)),
        ("Extra", len(result.extra_in_candidate)),
        ("Changed", len(result.changed)),
        ("Not comparable", len(result.not_comparable)),
        ("Ignored", result.ignored),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(str(value))}</div></div>'
        for label, value in tiles
    )
    config_rows: list[tuple[str, Any]] = [
        ("Reference", result.reference_endpoint),
        ("Candidate", result.candidate_endpoint),
        ("Tolerance", result.config.tolerance),
        ("Match by browse path", "yes" if result.config.match_by_browse_path else "no"),
        ("References compared", "yes" if result.config.compare_references else "no"),
        (
            "Device attributes compared",
            "yes" if result.config.device and result.device_attributes_compared
            else ("no (not in a snapshot)" if result.config.device else "no"),
        ),
        ("Values compared for", ", ".join(sorted(result.config.value_node_ids)) or "no node"),
        ("Float tolerance", result.config.float_tolerance),
        ("Ignore list", ", ".join(sorted(result.config.ignore_node_ids)) or "empty"),
        ("Reference nodes", result.reference_summary.get("nodes")),
        ("Candidate nodes", result.candidate_summary.get("nodes")),
        ("Generated", iso_utc(utc_now())),
    ]

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>OPC UA address-space comparison</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        "<h1>OPC UA address-space comparison</h1>",
        f'<p class="subtitle">{escape(result.reference_endpoint)} → '
        f"{escape(result.candidate_endpoint)}</p>",
        f'<p class="disclaimer">{escape(COMPARISON_LIMITATION_NOTE)}</p>',
        f'<div class="tiles">{tile_html}</div>',
        "<h2>Comparison configuration</h2>",
        f'<div class="card">{_definition_table(config_rows)}</div>',
        "<h2>Result</h2>",
        '<div class="card"><p>'
        + (
            "No difference found in the compared scope."
            if result.is_equal
            else f"{result.difference_count} difference(s) found."
        )
        + (" Warnings apply — read them before trusting the result." if result.has_warnings else "")
        + "</p></div>",
    ]

    if result.warnings:
        items = "".join(f"<li>{escape(warning)}</li>" for warning in result.warnings)
        parts.extend(["<h2>Warnings</h2>", f'<div class="card"><ul>{items}</ul></div>'])

    if result.truncated_differences:
        parts.append(
            f'<div class="card"><p>{result.truncated_differences} further difference(s) are counted but not '
            "listed (row limit). Raise <code>max_differences</code> for the complete list.</p></div>"
        )

    for kind, title in _BUCKETS:
        items = [item for item in result.differences if item.kind == kind]
        if not items:
            continue
        parts.extend(
            [f"<h2>{escape(title)} ({len(items)})</h2>", f'<div class="card">{_comparison_bucket_table(items)}</div>']
        )

    if result.namespace_differences:
        rows = "".join(
            _row(
                [
                    f"<code>{escape(str(entry.get('uri', '')))}</code>",
                    escape(str(entry.get("reference_index") if entry.get("reference_index") is not None else "—")),
                    escape(str(entry.get("candidate_index") if entry.get("candidate_index") is not None else "—")),
                ]
            )
            for entry in result.namespace_differences
        )
        parts.extend(
            [
                "<h2>Namespaces</h2>",
                '<div class="card"><table><thead><tr><th>Namespace URI</th><th>Reference index</th>'
                f"<th>Candidate index</th></tr></thead><tbody>{rows}</tbody></table></div>",
            ]
        )

    if result.reference_edges_not_compared:
        parts.append(
            f'<div class="card"><p>{result.reference_edges_not_compared} reference edge(s) have an '
            "endpoint outside the compared downloads, so they could not be compared. "
            "They are counted here instead of being reported as a difference.</p></div>"
        )

    if result.reference_differences:
        rows = "".join(
            _row(
                [
                    escape(KIND_LABELS.get(entry.kind, entry.kind)),
                    escape(entry.reference_type_name),
                    f"<code>{escape(entry.source)}</code>",
                    f"<code>{escape(entry.target)}</code>",
                ]
            )
            for entry in result.reference_differences
        )
        parts.extend(
            [
                f"<h2>References ({len(result.reference_differences)})</h2>",
                '<div class="card"><table><thead><tr><th>Kind</th><th>Reference type</th>'
                f"<th>Source</th><th>Target</th></tr></thead><tbody>{rows}</tbody></table></div>",
            ]
        )

    parts.extend(
        [
            '<p class="footer">Node identity: namespace URI + identifier; a NodeId printed above is the one '
            "that session used and is not portable to the other server.</p>",
            "</main></body></html>",
        ]
    )
    return "\n".join(parts)


def write_comparison_html(
    result: ComparisonResult,
    path: str | os.PathLike[str],
) -> ComparisonReport:
    """Write the comparison HTML report atomically."""
    target = Path(path)
    text = render_comparison_html(result)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc
    return ComparisonReport(path=target, bytes_written=len(text.encode("utf-8")), differences=result.difference_count)
