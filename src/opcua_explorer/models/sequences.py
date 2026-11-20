"""Universal sequence documents and run results.

A sequence is an operator-written document: preconditions, an optional gate
(user-defined acquire / keepalive / release steps) and a body. Nothing in the
model names a device, a vendor or a lock protocol - see
``opcua/sequence_runner.py`` for the mechanical rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .common import STATUS_SKIPPED, iso_utc, utc_now

#: Identifier and version of the sequence document written to and read from disk.
SEQUENCE_FORMAT = "opcua-explorer-sequence"
SEQUENCE_VERSION = 1

STEP_WRITE = "write"
STEP_EXPECT = "expect"
STEP_ASSERT = "assert"
STEP_ENSURE = "ensure"
STEP_CALL = "call"
STEP_DELAY = "delay"
STEP_WAIT_UNTIL = "wait_until"

#: Every supported step kind. A sequence may only contain these; anything else is refused
#: before the first request is sent.
SEQUENCE_STEP_KINDS = (
    STEP_WRITE,
    STEP_EXPECT,
    STEP_ASSERT,
    STEP_ENSURE,
    STEP_CALL,
    STEP_DELAY,
    STEP_WAIT_UNTIL,
)

#: Where a result was produced, so a gate release is visibly not a body step.
PHASE_PRECONDITION = "precondition"
PHASE_GATE_ON = "gate_on"
PHASE_GATE_VERIFY = "gate_verify"
PHASE_STEP = "step"
PHASE_KEEPALIVE = "keepalive"
PHASE_GATE_OFF = "gate_off"

#: Failure policies for the optional keepalive step.
KEEPALIVE_ABORT = "abort"
KEEPALIVE_WARN = "warn"


@dataclass(slots=True)
class SequenceStep:
    """One step of an operator-written sequence.

    The fields are generic; a step only uses the ones its ``kind`` needs. Texts may contain
    ``${name}`` placeholders that are substituted from the sequence inputs before the step runs.
    """

    kind: str
    id: str = ""
    node_id: str = ""
    #: Browse path resolved with TranslateBrowsePathsToNodeIds when ``node_id`` is empty.
    node_path: list[str] = field(default_factory=list)
    #: Start node for ``node_path``; the Objects folder by default.
    path_root: str = "i=85"
    #: Value to write (``write``, ``ensure``) or the expected value (``expect``, ``assert``, ``wait_until``).
    value: str = ""
    expected: str = ""
    #: Absolute tolerance for numeric comparisons; ``None`` means exact.
    tolerance: float | None = None
    method_node_id: str = ""
    #: Browse name resolved below the object node when ``method_node_id`` is empty.
    method_name: str = ""
    arguments: list[str] = field(default_factory=list)
    seconds: float = 0.0
    timeout_seconds: float = 10.0
    poll_seconds: float = 0.2
    #: Read back after a write (default off, so a step says explicitly whether it verifies).
    verify: bool = False
    #: ``ensure`` only: a missing node is skipped with a reason instead of failing.
    optional: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "node_id": self.node_id,
            "node_path": list(self.node_path),
            "path_root": self.path_root,
            "value": self.value,
            "expected": self.expected,
            "tolerance": self.tolerance,
            "method_node_id": self.method_node_id,
            "method_name": self.method_name,
            "arguments": list(self.arguments),
            "seconds": self.seconds,
            "timeout_seconds": self.timeout_seconds,
            "poll_seconds": self.poll_seconds,
            "verify": self.verify,
            "optional": self.optional,
            "note": self.note,
        }


@dataclass(slots=True)
class SequenceGate:
    """Operator-defined exclusive access around the body.

    ``on`` runs before the body, ``off`` runs unconditionally afterwards. ``keepalive`` is an
    optional step repeated every ``keepalive_every_seconds`` while the body runs (renewal,
    heartbeat, watchdog kick). No step here is device-specific by construction.
    """

    on: list[SequenceStep] = field(default_factory=list)
    off: list[SequenceStep] = field(default_factory=list)
    #: Must pass after ``on``; otherwise the body never starts.
    verify: SequenceStep | None = None
    keepalive: SequenceStep | None = None
    keepalive_every_seconds: float = 5.0
    #: ``abort`` stops the body when a keepalive step fails, ``warn`` only records it.
    keepalive_on_failure: str = KEEPALIVE_ABORT

    def is_empty(self) -> bool:
        return not self.on and not self.off and self.verify is None and self.keepalive is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "on": [step.to_dict() for step in self.on],
            "off": [step.to_dict() for step in self.off],
            "verify": self.verify.to_dict() if self.verify is not None else None,
            "keepalive": self.keepalive.to_dict() if self.keepalive is not None else None,
            "keepalive_every_seconds": self.keepalive_every_seconds,
            "keepalive_on_failure": self.keepalive_on_failure,
        }


@dataclass(slots=True)
class Sequence:
    """One complete operator-written sequence."""

    name: str = ""
    description: str = ""
    #: ``${name}`` substitutions; a caller-supplied value wins over the document value.
    inputs: dict[str, str] = field(default_factory=dict)
    preconditions: list[SequenceStep] = field(default_factory=list)
    gate: SequenceGate = field(default_factory=SequenceGate)
    steps: list[SequenceStep] = field(default_factory=list)
    #: ``False`` keeps running after a failed body step; the run is still reported as failed.
    abort_on_failure: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": SEQUENCE_FORMAT,
            "version": SEQUENCE_VERSION,
            "name": self.name,
            "description": self.description,
            "inputs": dict(self.inputs),
            "preconditions": [step.to_dict() for step in self.preconditions],
            "gate": self.gate.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "abort_on_failure": self.abort_on_failure,
        }


@dataclass(slots=True)
class SequenceStepResult:
    """Outcome of one executed step (body, precondition, gate or keepalive)."""

    step_id: str
    kind: str
    phase: str = PHASE_STEP
    status: str = STATUS_SKIPPED
    detail: str = ""
    node_id: str = ""
    previous_value: str = ""
    new_value: str = ""
    status_code: str = ""
    #: Only set for a step that sent a write or a call: the authority it was sent under.
    authorization: str = ""
    started_at: datetime = field(default_factory=utc_now)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "phase": self.phase,
            "status": self.status,
            "detail": self.detail,
            "node_id": self.node_id,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "status_code": self.status_code,
            "authorization": self.authorization,
            "started_at": iso_utc(self.started_at),
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class SequenceRun:
    """Result of one sequence run, including the gate release outcome."""

    name: str = ""
    status: str = STATUS_SKIPPED
    dry_run: bool = False
    inputs: dict[str, str] = field(default_factory=dict)
    results: list[SequenceStepResult] = field(default_factory=list)
    error: str = ""
    #: "not engaged", "released", "release failed: ..." - never left empty after a run.
    gate_state: str = ""
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    duration_ms: float = 0.0
    #: The step currently awaiting a response; used to report a cancellation honestly.
    in_flight: SequenceStepResult | None = None

    def step_results(self, phase: str) -> list[SequenceStepResult]:
        return [result for result in self.results if result.phase == phase]

    def summary_text(self) -> str:
        prefix = "Dry run" if self.dry_run else "Sequence"
        text = f"{prefix} {self.name or '(unnamed)'}: {self.status}"
        if self.error:
            text += f" - {self.error}"
        if self.gate_state:
            text += f" ({self.gate_state})"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "dry_run": self.dry_run,
            "inputs": dict(self.inputs),
            "error": self.error,
            "gate_state": self.gate_state,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "duration_ms": self.duration_ms,
            "steps": [result.to_dict() for result in self.results],
        }
