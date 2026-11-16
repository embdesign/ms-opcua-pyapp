"""Read and write operator-written sequence documents (``opcua-explorer-sequence``).

The GUI, a project file and the ``opcua-run-sequence`` command all use this parser, so a document
is validated **once, before anything is executed**. An unknown step kind, a missing node or an
unusable gate is refused with a message naming the step, instead of being discovered halfway
through a device sequence.

The parser is deliberately Qt-free and asyncua-free; it produces the plain dataclasses of
:mod:`opcua_explorer.models`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    KEEPALIVE_ABORT,
    KEEPALIVE_WARN,
    SEQUENCE_FORMAT,
    SEQUENCE_STEP_KINDS,
    SEQUENCE_VERSION,
    Sequence,
    SequenceGate,
    SequenceStep,
)

SEQUENCE_SUFFIX = ".opcua-sequence.json"
SEQUENCE_FILE_FILTER = "OPC UA Explorer sequence (*.opcua-sequence.json *.json);;All files (*)"

#: Kinds that only read; they are the only ones allowed as a gate ``verify`` step.
_READ_ONLY_KINDS = frozenset({"expect", "assert", "wait_until"})

#: Kinds that must name a target node.
_TARGETED_KINDS = frozenset({"write", "ensure", "expect", "assert", "wait_until", "call"})


class SequenceDocumentError(ValueError):
    """A sequence document is malformed or uses something this build cannot run."""


def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _flag(value: Any, default: bool) -> bool:
    return bool(default) if value is None else bool(value)


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise SequenceDocumentError(f"a browse path must be a list of names, not {type(value).__name__}")


def _step_from_dict(data: Any, where: str) -> SequenceStep:
    if not isinstance(data, dict):
        raise SequenceDocumentError(f"{where}: a step must be an object")
    kind = _text(data.get("kind")).strip()
    if kind not in SEQUENCE_STEP_KINDS:
        raise SequenceDocumentError(
            f"{where}: unsupported step kind {kind!r}; expected one of {', '.join(SEQUENCE_STEP_KINDS)}"
        )

    node_id = _text(data.get("node_id", data.get("node"))).strip()
    node_path = _names(data.get("node_path", data.get("path")))
    step = SequenceStep(
        kind=kind,
        id=_text(data.get("id")).strip(),
        node_id=node_id,
        node_path=node_path,
        path_root=_text(data.get("path_root"), "i=85").strip() or "i=85",
        value=_text(data.get("value")),
        expected=_text(data.get("expected")),
        tolerance=(
            None if data.get("tolerance") is None else _number(data.get("tolerance"), 0.0)
        ),
        method_node_id=_text(data.get("method_node_id")).strip(),
        method_name=_text(data.get("method_name", data.get("method"))).strip(),
        arguments=[_text(item) for item in data.get("arguments", []) or []],
        seconds=_number(data.get("seconds"), 0.0),
        timeout_seconds=_number(data.get("timeout_seconds"), 10.0),
        poll_seconds=_number(data.get("poll_seconds"), 0.2),
        verify=_flag(data.get("verify"), False),
        optional=_flag(data.get("optional"), False),
        note=_text(data.get("note")),
    )

    label = f"{where} ({kind} {step.id or 'step'})"
    if kind in _TARGETED_KINDS and not node_id and not node_path:
        raise SequenceDocumentError(f"{label}: set 'node_id' or 'path'")
    if kind in ("write", "ensure") and not step.value.strip():
        raise SequenceDocumentError(f"{label}: set the 'value' to write")
    if kind in ("expect", "assert", "wait_until") and not (step.expected or step.value).strip():
        raise SequenceDocumentError(f"{label}: set 'expected' (or 'value') to compare with")
    if kind == "call" and not step.method_node_id and not step.method_name:
        raise SequenceDocumentError(f"{label}: set 'method_node_id' or 'method' (browse name)")
    if kind == "delay" and step.seconds <= 0 and not step.value.strip():
        raise SequenceDocumentError(f"{label}: set 'seconds'")
    if step.timeout_seconds <= 0:
        raise SequenceDocumentError(f"{label}: 'timeout_seconds' must be positive")
    if step.poll_seconds <= 0:
        raise SequenceDocumentError(f"{label}: 'poll_seconds' must be positive")
    return step


def _steps_from_list(data: Any, where: str) -> list[SequenceStep]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise SequenceDocumentError(f"{where}: expected a list of steps")
    return [
        _step_from_dict(entry, f"{where}[{index}]") for index, entry in enumerate(data, start=1)
    ]


def _gate_from_dict(data: Any) -> SequenceGate:
    if data is None:
        return SequenceGate()
    if not isinstance(data, dict):
        raise SequenceDocumentError("gate: expected an object")
    on_steps = _steps_from_list(data.get("on"), "gate.on")
    off_steps = _steps_from_list(data.get("off"), "gate.off")
    verify = None
    if data.get("verify") is not None:
        verify = _step_from_dict(data["verify"], "gate.verify")
        if verify.kind not in _READ_ONLY_KINDS:
            raise SequenceDocumentError(
                f"gate.verify ({verify.kind}): only read-only steps can verify a gate "
                f"({', '.join(sorted(_READ_ONLY_KINDS))})"
            )
    keepalive = None
    if data.get("keepalive") is not None:
        keepalive = _step_from_dict(data["keepalive"], "gate.keepalive")
    every = _number(data.get("keepalive_every_seconds", data.get("every_s")), 5.0)
    if every <= 0:
        raise SequenceDocumentError("gate.keepalive_every_seconds: must be positive")
    on_failure = _text(data.get("keepalive_on_failure", data.get("on_failure")), KEEPALIVE_ABORT).strip()
    if on_failure not in (KEEPALIVE_ABORT, KEEPALIVE_WARN):
        raise SequenceDocumentError(
            f"gate.keepalive_on_failure: expected {KEEPALIVE_ABORT!r} or {KEEPALIVE_WARN!r}"
        )
    return SequenceGate(
        on=on_steps,
        off=off_steps,
        verify=verify,
        keepalive=keepalive,
        keepalive_every_seconds=every,
        keepalive_on_failure=on_failure,
    )


def sequence_from_document(document: Any) -> Sequence:
    """Turn a parsed JSON document into a ``Sequence``; raise ``SequenceDocumentError``."""
    if not isinstance(document, dict):
        raise SequenceDocumentError("a sequence document must be a JSON object")
    file_format = document.get("format")
    if file_format is not None and file_format != SEQUENCE_FORMAT:
        raise SequenceDocumentError(
            f"not an OPC UA Explorer sequence document (format {file_format!r})"
        )
    try:
        version = int(document.get("version", SEQUENCE_VERSION))
    except (TypeError, ValueError) as exc:
        raise SequenceDocumentError("the sequence version is not a number") from exc
    if version <= 0:
        raise SequenceDocumentError("the sequence has no usable version")
    if version > SEQUENCE_VERSION:
        raise SequenceDocumentError(
            f"the sequence was written for a newer build (version {version}; this build supports "
            f"{SEQUENCE_VERSION})"
        )

    raw_inputs = document.get("inputs")
    if raw_inputs is None:
        raw_inputs = {}
    if not isinstance(raw_inputs, dict):
        raise SequenceDocumentError("inputs: expected an object of name/value pairs")
    inputs = {str(name): _text(value) for name, value in raw_inputs.items()}

    gate = _gate_from_dict(document.get("gate"))
    if not gate.on and gate.verify is not None:
        raise SequenceDocumentError("gate.verify: a gate without 'on' steps has nothing to verify")
    if not gate.on and gate.keepalive is not None:
        raise SequenceDocumentError("gate.keepalive: a gate without 'on' steps has nothing to keep alive")

    return Sequence(
        name=_text(document.get("name")).strip() or "Sequence",
        description=_text(document.get("description")),
        inputs=inputs,
        preconditions=_steps_from_list(document.get("preconditions"), "preconditions"),
        gate=gate,
        steps=_steps_from_list(document.get("steps"), "steps"),
        abort_on_failure=_flag(document.get("abort_on_failure"), True),
    )


def load_sequence_file(path: str | Path) -> Sequence:
    """Read and validate one sequence document from disk."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SequenceDocumentError(f"Could not read {target}: {exc}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise SequenceDocumentError(f"{target} is not valid JSON: {exc}") from exc
    try:
        return sequence_from_document(document)
    except SequenceDocumentError as exc:
        raise SequenceDocumentError(f"{target}: {exc}") from exc


def save_sequence_file(sequence: Sequence, path: str | Path) -> Path:
    """Write a sequence document; returns the path it was written to."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(sequence.to_dict(), indent=2, ensure_ascii=False) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


__all__ = [
    "SequenceDocumentError",
    "SEQUENCE_FILE_FILTER",
    "SEQUENCE_SUFFIX",
    "load_sequence_file",
    "save_sequence_file",
    "sequence_from_document",
]
