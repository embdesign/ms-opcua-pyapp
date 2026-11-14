"""Test report documents: JSON shape and safe HTML rendering."""

from __future__ import annotations

import json

from opcua_explorer.models import FUNCTIONAL_TEST_DISCLAIMER, STATUS_FAILED, STATUS_PASSED, STATUS_SKIPPED
from opcua_explorer.reporting import (
    REPORT_FORMAT,
    REPORT_VERSION,
    render_html,
    run_to_document,
    write_html_report,
    write_json_report,
)
from tests.support import make_test_run


def test_json_document_structure() -> None:
    run = make_test_run()
    document = run_to_document(run)

    assert document["format"] == REPORT_FORMAT
    assert document["format_version"] == REPORT_VERSION
    assert document["disclaimer"] == FUNCTIONAL_TEST_DISCLAIMER
    assert document["server"]["endpoint_url"] == "opc.tcp://demo:4840"
    assert document["server"]["connection"]["security_policy_label"] == "Basic256Sha256"
    assert document["server"]["connection"]["security_mode"] == "SignAndEncrypt"
    assert document["configuration"]["variable_node_id"] == "ns=2;s=Temperature"
    assert document["timing"]["duration_seconds"] == 1.5
    assert document["timing"]["started_at_utc"].endswith("Z")

    summary = document["summary"]
    assert summary == {
        "total": 3,
        "passed": 2,
        "failed": 0,
        "skipped": 1,
        "cancelled": False,
        "duration_seconds": 1.5,
        "started_at_utc": document["timing"]["started_at_utc"],
        "finished_at_utc": document["timing"]["finished_at_utc"],
    }

    results = {entry["id"]: entry for entry in document["results"]}
    assert results["endpoint_discovery"]["status"] == STATUS_PASSED
    assert results["subscription_data_change"]["status"] == STATUS_SKIPPED
    assert results["subscription_data_change"]["data"]["value_changes_observed"] == 0
    assert results["subscription_data_change"]["notes"] == ["This is not a failure."]
    assert results["latency"]["data"] == {}

    assert document["coverage_limitations"] == ["Static limitation note."]
    assert document["inaccessible_nodes"] == [
        {"node_id": "ns=2;s=Denied", "status": "BadUserAccessDenied"}
    ]
    assert document["notes"] == ["A run note."]
    assert document["environment"]["asyncua"] == "2.1.0"


def test_json_document_marks_failures_and_cancellation() -> None:
    document = run_to_document(make_test_run(failing=True, cancelled=True))
    assert document["summary"]["failed"] == 1
    assert document["cancelled"] is True
    failed = next(entry for entry in document["results"] if entry["status"] == STATUS_FAILED)
    assert failed["error"] == "RuntimeError: boom <bad>"


def test_write_json_report(tmp_path) -> None:
    run = make_test_run()
    target = tmp_path / "nested" / "report.json"
    result = write_json_report(run, target)

    assert result.path == target
    assert result.checks == 3
    assert result.bytes_written > 0
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["summary"]["passed"] == 2


def test_html_report_escapes_dynamic_content() -> None:
    html = render_html(make_test_run())

    # Server-provided strings must never become markup.
    assert "<script>alert('x')</script>" not in html
    assert "&lt;script&gt;alert" in html
    assert "ns=2;s=&lt;Temperature&gt;" in html
    assert "No change observed" in html


def test_html_report_escapes_error_text() -> None:
    html = render_html(make_test_run(failing=True))
    assert "<bad>" not in html
    assert "boom &lt;bad&gt;" in html


def test_html_report_contains_required_sections() -> None:
    html = render_html(make_test_run())

    assert html.startswith("<!DOCTYPE html>")
    for expected in (
        "OPC UA functional test report",
        FUNCTIONAL_TEST_DISCLAIMER,
        "Server and security",
        "Test configuration",
        "Results",
        "Check details",
        "Coverage limitations",
        "Inaccessible nodes",
        "Environment",
        "Basic256Sha256",
        "SignAndEncrypt",
        "BadUserAccessDenied",
    ):
        assert expected in html

    # Status pills carry a class per status (and therefore an accessible colour).
    assert 'class="pill passed"' in html
    assert 'class="pill skipped"' in html
    assert "opc.tcp://demo:4840" in html


def test_html_report_notes_cancellation() -> None:
    html = render_html(make_test_run(cancelled=True))
    assert "cancelled" in html.lower()


def test_write_html_report(tmp_path) -> None:
    run = make_test_run()
    target = tmp_path / "report.html"
    result = write_html_report(run, target)

    assert result.path == target
    assert result.checks == 3
    text = target.read_text(encoding="utf-8")
    assert text.endswith("</html>")
    assert result.bytes_written == len(text.encode("utf-8"))
