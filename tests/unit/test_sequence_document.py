"""Unit tests for sequence document parsing and validation (no Qt, no server)."""

from __future__ import annotations

import json

import pytest

from opcua_explorer.models import SEQUENCE_FORMAT, SEQUENCE_VERSION, Sequence
from opcua_explorer.sequence_document import (
    SequenceDocumentError,
    load_sequence_file,
    save_sequence_file,
    sequence_from_document,
)


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "format": SEQUENCE_FORMAT,
        "version": SEQUENCE_VERSION,
        "name": "PSU setpoint",
        "inputs": {"voltage": "24.0"},
        "steps": [
            {"kind": "write", "id": "set", "node": "ns=2;s=SetVoltage", "value": "${voltage}", "verify": True}
        ],
    }
    document.update(overrides)
    return document


def test_a_minimal_document_is_parsed_with_aliases():
    sequence = sequence_from_document(_document())

    assert isinstance(sequence, Sequence)
    assert sequence.name == "PSU setpoint"
    assert sequence.inputs == {"voltage": "24.0"}
    assert sequence.abort_on_failure is True
    step = sequence.steps[0]
    assert step.kind == "write"
    assert step.node_id == "ns=2;s=SetVoltage"
    assert step.value == "${voltage}"
    assert step.verify is True


def test_paths_gates_and_keepalive_aliases_are_parsed():
    document = _document(
        preconditions=[{"kind": "assert", "path": ["2:Plant", "2:Line1"], "expected": "true"}],
        gate={
            "on": [{"kind": "call", "node": "ns=2;s=Lock", "method": "InitLock"}],
            "verify": {"kind": "expect", "node": "ns=2;s=Held", "expected": "true"},
            "keepalive": {"kind": "call", "node": "ns=2;s=Lock", "method": "RenewLock"},
            "every_s": 3,
            "on_failure": "warn",
            "off": [{"kind": "call", "node": "ns=2;s=Lock", "method": "ExitLock"}],
        },
    )

    sequence = sequence_from_document(document)

    assert sequence.preconditions[0].node_path == ["2:Plant", "2:Line1"]
    assert sequence.gate.on[0].method_name == "InitLock"
    assert sequence.gate.verify is not None
    assert sequence.gate.keepalive is not None
    assert sequence.gate.keepalive_every_seconds == 3.0
    assert sequence.gate.keepalive_on_failure == "warn"
    assert sequence.gate.off[0].method_name == "ExitLock"


def test_a_document_round_trips_through_to_dict():
    original = sequence_from_document(_document(gate={"on": [{"kind": "write", "node": "ns=2;s=Lock", "value": "true"}]}))

    again = sequence_from_document(original.to_dict())

    assert again.to_dict() == original.to_dict()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (_document(steps=[{"kind": "explode"}]), "unsupported step kind"),
        (_document(steps=[{"kind": "write", "node": "ns=2;s=X"}]), "set the 'value'"),
        (_document(steps=[{"kind": "write", "value": "1"}]), "set 'node_id' or 'path'"),
        (_document(steps=[{"kind": "expect", "node": "ns=2;s=X"}]), "set 'expected'"),
        (_document(steps=[{"kind": "call", "node": "ns=2;s=Obj"}]), "'method_node_id' or 'method'"),
        (_document(steps=[{"kind": "delay"}]), "set 'seconds'"),
        (_document(steps=[{"kind": "delay", "seconds": 1, "timeout_seconds": 0}]), "timeout_seconds"),
        (_document(format="something-else"), "not an OPC UA Explorer sequence"),
        (_document(version=SEQUENCE_VERSION + 1), "newer build"),
        (_document(version=0), "no usable version"),
        (_document(inputs=[]), "inputs: expected an object"),
    ],
)
def test_invalid_documents_are_refused_with_a_reason(document, message):
    with pytest.raises(SequenceDocumentError, match=message):
        sequence_from_document(document)


def test_a_gate_verify_step_must_be_read_only():
    document = _document(
        gate={
            "on": [{"kind": "write", "node": "ns=2;s=Lock", "value": "true"}],
            "verify": {"kind": "write", "node": "ns=2;s=Held", "value": "true"},
        }
    )

    with pytest.raises(SequenceDocumentError, match="only read-only steps"):
        sequence_from_document(document)


def test_a_verify_or_keepalive_without_acquire_steps_is_refused():
    with pytest.raises(SequenceDocumentError, match="nothing to verify"):
        sequence_from_document(_document(gate={"verify": {"kind": "expect", "node": "ns=2;s=X", "expected": "1"}}))
    with pytest.raises(SequenceDocumentError, match="nothing to keep alive"):
        sequence_from_document(
            _document(gate={"keepalive": {"kind": "call", "node": "ns=2;s=X", "method": "Renew"}})
        )


def test_an_invalid_keepalive_policy_is_refused():
    document = _document(
        gate={
            "on": [{"kind": "write", "node": "ns=2;s=Lock", "value": "true"}],
            "keepalive": {"kind": "call", "node": "ns=2;s=Lock", "method": "Renew"},
            "on_failure": "ignore",
        }
    )

    with pytest.raises(SequenceDocumentError, match="keepalive_on_failure"):
        sequence_from_document(document)


def test_numbers_in_json_are_accepted_as_text_values():
    sequence = sequence_from_document(
        _document(steps=[{"kind": "write", "node": "ns=2;s=X", "value": 24.5}])
    )

    assert sequence.steps[0].value == "24.5"


def test_files_are_saved_and_loaded(tmp_path):
    sequence = sequence_from_document(_document())
    path = save_sequence_file(sequence, tmp_path / "psu.opcua-sequence.json")

    assert path.is_file()
    loaded = load_sequence_file(path)
    assert loaded.to_dict() == sequence.to_dict()


def test_file_errors_name_the_file(tmp_path):
    missing = tmp_path / "missing.opcua-sequence.json"
    with pytest.raises(SequenceDocumentError, match="Could not read"):
        load_sequence_file(missing)

    broken = tmp_path / "broken.opcua-sequence.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SequenceDocumentError, match="not valid JSON"):
        load_sequence_file(broken)

    invalid = tmp_path / "invalid.opcua-sequence.json"
    invalid.write_text(json.dumps(_document(steps=[{"kind": "explode"}])), encoding="utf-8")
    with pytest.raises(SequenceDocumentError) as excinfo:
        load_sequence_file(invalid)
    assert str(invalid) in str(excinfo.value)
    assert "unsupported step kind" in str(excinfo.value)
