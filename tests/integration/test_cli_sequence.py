"""Headless ``opcua-run-sequence`` end to end against the demo server."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opcua_explorer import cli_sequence


def _document(node_id: str, *, value: str = "42.5") -> dict:
    return {
        "format": "opcua-explorer-sequence",
        "version": 1,
        "name": "Demo setpoint",
        "inputs": {"value": value},
        "steps": [
            {"kind": "write", "id": "set", "node_id": node_id, "value": "${value}", "verify": True},
            {"kind": "expect", "id": "check", "node_id": node_id, "expected": "${value}"},
        ],
    }


def _write_document(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "demo.opcua-sequence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _argv(tmp_path: Path, demo_model, document: Path, *extra: str) -> list[str]:
    return [
        str(document),
        "--endpoint",
        demo_model.endpoint,
        "--trust-store",
        str(tmp_path / "trust.json"),
        "--quiet",
        *extra,
    ]


async def _run(tmp_path: Path, demo_model, document: Path, *extra: str) -> int:
    return await asyncio.to_thread(cli_sequence.main, _argv(tmp_path, demo_model, document, *extra))


async def test_a_dry_run_resolves_the_document_and_sends_nothing(service, demo_model, tmp_path):
    temperature = demo_model.node_ids["temperature"]
    document = _write_document(tmp_path, _document(temperature))
    report = tmp_path / "dry-run.json"

    code = await _run(tmp_path, demo_model, document, "--dry-run", "--json", str(report))

    assert code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["status"] == "passed"
    assert len(payload["steps"]) == 2
    assert all(step["status"] == "skipped" for step in payload["steps"])
    assert (await service.read_value(temperature)).value == pytest.approx(21.5)


async def test_a_sequence_writes_reads_back_and_reports_each_step(service, demo_model, tmp_path):
    temperature = demo_model.node_ids["temperature"]
    document = _write_document(tmp_path, _document(temperature))
    report = tmp_path / "run.json"

    code = await _run(tmp_path, demo_model, document, "--json", str(report))

    assert code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert [step["status"] for step in payload["steps"]] == ["passed", "passed"]
    assert payload["steps"][0]["new_value"] == "42.5"
    assert (await service.read_value(temperature)).value == pytest.approx(42.5)


async def test_an_input_overrides_the_documents_default(service, demo_model, tmp_path):
    temperature = demo_model.node_ids["temperature"]
    document = _write_document(tmp_path, _document(temperature))

    code = await _run(tmp_path, demo_model, document, "--input", "value=12.0")

    assert code == 0
    assert (await service.read_value(temperature)).value == pytest.approx(12.0)


async def test_a_rejected_write_makes_the_command_fail(service, demo_model, tmp_path):
    # The serial number is read-only on the demo server, so the write returns a Bad status.
    document = _write_document(tmp_path, _document(demo_model.node_ids["serial_number"], value="SN-1"))
    report = tmp_path / "failed.json"

    code = await _run(tmp_path, demo_model, document, "--json", str(report))

    assert code == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert "Bad" in payload["error"] or "Bad" in payload["steps"][0]["detail"]


async def test_an_unresolvable_sequence_fails_without_touching_the_device(service, demo_model, tmp_path):
    document = _write_document(
        tmp_path,
        {
            "format": "opcua-explorer-sequence",
            "version": 1,
            "name": "Missing target",
            "steps": [{"kind": "write", "id": "set", "node_id": "ns=2;s=DoesNotExist", "value": "1"}],
        },
    )

    code = await _run(tmp_path, demo_model, document)

    assert code == 1
    assert (await service.read_value(demo_model.node_ids["temperature"])).value == pytest.approx(21.5)
