"""Universal sequence runner: operator-written control workflows.

A sequence is an operator-written document (``models.Sequence``): preconditions (usually read-only
checks, but an ``ensure`` may write a prerequisite), an optional **gate** made of user-defined steps,
and a body of generic operations. Nothing in this
module knows a device, a vendor or a lock protocol; the only rules enforced here are mechanical:

* the gate's ``off`` steps run in ``finally`` - after success, after a failed step, after a
  cancellation and after an abort triggered by a failed keepalive;
* a gate ``verify`` step gates the body, so a refused acquisition aborts before the first write;
* every step runs exactly once - nothing is retried;
* a cancellation while a write or a call is in flight is reported as ``outcome_unknown``;
* only one sequence runs at a time on one runner (``SequenceBusyError``).

Exclusivity *across* sessions of the same endpoint is the caller's responsibility: the GUI owns
one runner per session and must not start two.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from asyncua import ua

from ..errors import OpcUaExplorerError
from ..models import (
    AUTHORITY_SEQUENCE_DOCUMENT,
    KEEPALIVE_ABORT,
    PHASE_GATE_OFF,
    PHASE_GATE_ON,
    PHASE_GATE_VERIFY,
    PHASE_KEEPALIVE,
    PHASE_PRECONDITION,
    PHASE_STEP,
    SEQUENCE_STEP_KINDS,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    Sequence,
    SequenceGate,
    SequenceRun,
    SequenceStep,
    SequenceStepResult,
    WriteAuthority,
    utc_now,
)
from . import path_support, type_support, write_service
from .check_support import preview_value
from .method_support import MetadataUnavailable, read_node_data_type
from .ua_names import node_class_name, status_name
from .value_parsing import ValueParseError

_INPUT_PATTERN = re.compile(r"\$\{([A-Za-z0-9_.\-]+)\}")

_logger = logging.getLogger(__name__)


class SequenceBusyError(OpcUaExplorerError):
    """A sequence is already running on this runner."""


class SequenceAborted(OpcUaExplorerError):
    """The run stopped early; the message is shown to the operator."""


class _SequenceCancelled(Exception):
    """Cooperative cancellation through ``asyncio.Event``-like token."""


class _ReadFailed(Exception):
    """A read that a step needs did not return a usable value."""


def _resolve(text: str, inputs: dict[str, str]) -> str:
    """Substitute ``${name}`` placeholders; a missing input aborts the step."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            missing.append(name)
            return match.group(0)
        return str(inputs[name])

    resolved = _INPUT_PATTERN.sub(replace, text or "")
    if missing:
        raise SequenceAborted(f"Missing sequence input(s): {', '.join(sorted(set(missing)))}.")
    return resolved


def _values_match(actual: Any, expected: Any, tolerance: float | None) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if tolerance is not None:
            return abs(float(actual) - float(expected)) <= float(tolerance)
        return float(actual) == float(expected)
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        if len(actual) != len(expected):
            return False
        return all(_values_match(a, e, tolerance) for a, e in zip(actual, expected, strict=True))
    return actual == expected


class SequenceRunner:
    """Runs one ``Sequence`` at a time against one client; no Qt, no device knowledge."""

    def __init__(self, client: Any, *, clock: Any = time.perf_counter, sleep: Any = asyncio.sleep) -> None:
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._running = False
        self._last_run: SequenceRun | None = None
        self._on_step: Callable[[SequenceStepResult], None] | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_run(self) -> SequenceRun | None:
        """The most recent run, including a cancelled one (whose ``run()`` re-raises)."""
        return self._last_run

    async def run(
        self,
        sequence: Sequence,
        *,
        inputs: dict[str, str] | None = None,
        dry_run: bool = False,
        cancel: Any = None,
        on_step: Callable[[SequenceStepResult], None] | None = None,
    ) -> SequenceRun:
        """Run ``sequence`` once; ``cancel`` is an ``asyncio.Event``-like token."""
        if self._running:
            raise SequenceBusyError("A sequence is already running; wait for it to finish or stop it first.")
        self._running = True
        started = self._clock()
        resolved_inputs = dict(sequence.inputs)
        resolved_inputs.update({str(key): str(value) for key, value in (inputs or {}).items()})
        run = SequenceRun(
            name=sequence.name,
            dry_run=dry_run,
            inputs=resolved_inputs,
            status=STATUS_PASSED,
            gate_state="not engaged",
        )
        gate_engaged = False
        keepalive_task: asyncio.Task[None] | None = None
        abort_event = asyncio.Event()
        self._on_step = on_step
        try:
            try:
                self._raise_if_cancelled(cancel)
                for step in sequence.preconditions:
                    await self._run_step(
                        run, step, PHASE_PRECONDITION, resolved_inputs, dry_run, cancel, abort_on_failure=True
                    )
                for step in sequence.gate.on:
                    gate_engaged = True
                    await self._run_step(run, step, PHASE_GATE_ON, resolved_inputs, dry_run, cancel, abort_on_failure=True)
                if gate_engaged:
                    run.gate_state = "engaged"
                if sequence.gate.verify is not None:
                    await self._run_step(
                        run, sequence.gate.verify, PHASE_GATE_VERIFY, resolved_inputs, dry_run, cancel, abort_on_failure=True
                    )
                if sequence.gate.keepalive is not None and not dry_run:
                    keepalive_task = asyncio.create_task(
                        self._keepalive_loop(run, sequence.gate, resolved_inputs, abort_event, cancel)
                    )
                for step in sequence.steps:
                    if abort_event.is_set():
                        raise SequenceAborted("The keepalive failed; the sequence stopped before the next step.")
                    await self._run_step(
                        run,
                        step,
                        PHASE_STEP,
                        resolved_inputs,
                        dry_run,
                        cancel,
                        abort_on_failure=sequence.abort_on_failure,
                    )
            except _SequenceCancelled:
                run.status = STATUS_CANCELLED
                run.error = "Cancelled by the operator."
            except SequenceAborted as exc:
                run.status = STATUS_FAILED
                run.error = str(exc)
            except asyncio.CancelledError:
                run.status = STATUS_CANCELLED
                run.error = "Cancelled."
                raise
            finally:
                if keepalive_task is not None:
                    keepalive_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive_task
                if gate_engaged:
                    await self._release_gate(run, sequence.gate, resolved_inputs)
                self._finish(run, started)
        finally:
            self._running = False
            self._on_step = None
            self._last_run = run
        return run

    async def release_gate(
        self,
        sequence: Sequence,
        *,
        inputs: dict[str, str] | None = None,
    ) -> SequenceRun:
        """Run only the gate's release steps, for an operator-triggered release."""
        if self._running:
            raise SequenceBusyError("A sequence is already running; wait for it to finish or stop it first.")
        self._running = True
        started = self._clock()
        resolved_inputs = dict(sequence.inputs)
        resolved_inputs.update({str(key): str(value) for key, value in (inputs or {}).items()})
        run = SequenceRun(
            name=f"{sequence.name or 'sequence'} (gate release)",
            inputs=resolved_inputs,
            status=STATUS_PASSED,
            gate_state="not engaged",
        )
        try:
            if not sequence.gate.off:
                run.status = STATUS_SKIPPED
                run.gate_state = "no release steps are defined"
                run.error = "The sequence defines no gate release steps."
            else:
                await self._release_gate(run, sequence.gate, resolved_inputs)
                failures = [
                    result
                    for result in run.results
                    if result.status in (STATUS_FAILED, STATUS_OUTCOME_UNKNOWN)
                ]
                if failures:
                    run.status = STATUS_FAILED
                    run.error = f"{len(failures)} release step(s) did not pass."
        finally:
            self._running = False
            self._last_run = run
            self._finish(run, started)
        return run

    def _finish(self, run: SequenceRun, started: float) -> None:
        run.finished_at = utc_now()
        run.duration_ms = round((self._clock() - started) * 1000.0, 2)
        if run.status != STATUS_PASSED:
            return
        failed = [
            result
            for result in run.results
            if result.status in (STATUS_FAILED, STATUS_OUTCOME_UNKNOWN)
        ]
        if failed:
            run.status = STATUS_FAILED
            run.error = run.error or f"{len(failed)} step(s) did not pass."

    async def _release_gate(self, run: SequenceRun, gate: SequenceGate, inputs: dict[str, str]) -> None:
        if not gate.off:
            run.gate_state = "engaged; no release steps were defined"
            return
        if run.dry_run:
            run.gate_state = "dry run; the release steps were not sent"
            return
        failures: list[str] = []
        for step in gate.off:
            try:
                result = await self._run_step(run, step, PHASE_GATE_OFF, inputs, False, None, abort_on_failure=False)
            except (asyncio.CancelledError, _SequenceCancelled):
                failures.append(f"{step.id or step.kind}: the release was interrupted")
                continue
            except SequenceAborted as exc:
                failures.append(str(exc))
                continue
            if result.status in (STATUS_FAILED, STATUS_OUTCOME_UNKNOWN):
                failures.append(result.detail or f"{result.step_id} did not pass")
        run.gate_state = "released" if not failures else "release failed: " + "; ".join(failures)

    # -- keepalive -------------------------------------------------------------------

    async def _keepalive_loop(
        self,
        run: SequenceRun,
        gate: SequenceGate,
        inputs: dict[str, str],
        abort_event: asyncio.Event,
        cancel: Any,
    ) -> None:
        interval = max(0.1, float(gate.keepalive_every_seconds))
        try:
            while True:
                await self._sleep_or_cancel(interval, cancel)
                if gate.keepalive is None:
                    return
                try:
                    result = await self._run_step(
                        run, gate.keepalive, PHASE_KEEPALIVE, inputs, False, cancel, abort_on_failure=False
                    )
                except _SequenceCancelled:
                    return
                if result.status not in (STATUS_FAILED, STATUS_OUTCOME_UNKNOWN):
                    continue
                if (gate.keepalive_on_failure or KEEPALIVE_ABORT) == KEEPALIVE_ABORT:
                    abort_event.set()
                    return
        except asyncio.CancelledError:
            return

    # -- step plumbing ---------------------------------------------------------------

    async def _run_step(
        self,
        run: SequenceRun,
        step: SequenceStep,
        phase: str,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
        *,
        abort_on_failure: bool,
    ) -> SequenceStepResult:
        result = SequenceStepResult(
            step_id=step.id or f"{step.kind}-{len(run.results) + 1}",
            kind=step.kind,
            phase=phase,
        )
        run.in_flight = result
        started = self._clock()
        try:
            await self._execute(step, result, inputs=inputs, dry_run=dry_run, cancel=cancel)
        except _SequenceCancelled:
            result.status = STATUS_CANCELLED
            result.detail = result.detail or "Cancelled before the step completed."
            raise
        except asyncio.CancelledError:
            if result.status == STATUS_SKIPPED:
                result.status = STATUS_CANCELLED
                result.detail = result.detail or "Cancelled before the step completed."
            raise
        finally:
            result.duration_ms = round((self._clock() - started) * 1000.0, 2)
            run.results.append(result)
            run.in_flight = None
            self._notify_step(result)
        if abort_on_failure and result.status in (STATUS_FAILED, STATUS_OUTCOME_UNKNOWN):
            raise SequenceAborted(result.detail or f"{result.step_id} did not pass.")
        return result

    async def _execute(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        kind = (step.kind or "").strip()
        if kind not in SEQUENCE_STEP_KINDS:
            self._fail(result, f"Unsupported step kind {step.kind!r}; nothing was sent.")
            return
        handler = getattr(self, f"_step_{kind}")
        try:
            await handler(step, result, inputs=inputs, dry_run=dry_run, cancel=cancel)
        except SequenceAborted as exc:
            self._fail(result, str(exc))

    def _fail(self, result: SequenceStepResult, message: str) -> None:
        result.status = STATUS_FAILED
        result.detail = message

    def _notify_step(self, result: SequenceStepResult) -> None:
        callback = self._on_step
        if callback is None:
            return
        try:
            callback(result)
        except Exception:  # pragma: no cover - a UI callback must not abort a device sequence
            _logger.debug("sequence step callback raised", exc_info=True)

    async def _node_id_for(self, step: SequenceStep, inputs: dict[str, str]) -> str:
        """The step's node: an explicit NodeId, or a browse path resolved by the server."""
        if not step.node_path:
            return _resolve(step.node_id, inputs)
        names = [_resolve(name, inputs) for name in step.node_path]
        root = _resolve(step.path_root or "i=85", inputs)
        try:
            resolution = await path_support.resolve_path(self._client, root, names)
        except path_support.PathResolutionError as exc:
            raise SequenceAborted(str(exc)) from exc
        if not resolution.ok:
            raise SequenceAborted(resolution.detail or f"{'/'.join(names)} was not resolved.")
        return resolution.node_id

    # -- steps -----------------------------------------------------------------------

    async def _step_write(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        node_id = await self._node_id_for(step, inputs)
        value_text = _resolve(step.value, inputs)
        await self._write(node_id, value_text, result, dry_run=dry_run, verify=step.verify)

    async def _step_ensure(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        try:
            node_id = await self._node_id_for(step, inputs)
        except SequenceAborted as exc:
            if step.optional:
                result.status = STATUS_SKIPPED
                result.detail = f"{exc} The optional step was skipped."
                return
            raise
        value_text = _resolve(step.value, inputs)
        result.node_id = node_id
        try:
            node = self._client.get_node(node_id)
            node_class = await node.read_node_class()
        except asyncio.CancelledError:
            raise
        except ua.UaStatusCodeError as exc:
            if step.optional and exc.code == ua.StatusCodes.BadNodeIdUnknown:
                result.status = STATUS_SKIPPED
                result.detail = f"{node_id} does not exist; the optional step was skipped."
                return
            self._fail(result, f"{node_id} could not be read: {status_name(exc.code)}")
            return
        except Exception as exc:
            self._fail(result, f"{node_id} could not be read: {type(exc).__name__}: {exc}")
            return
        if node_class != ua.NodeClass.Variable:
            self._fail(result, f"{node_id} is a {node_class_name(node_class)}, not a Variable; nothing was written.")
            return

        try:
            data_type, value_rank = await read_node_data_type(self._client, node_id)
            value_type = await type_support.resolve_value_type(self._client, data_type, value_rank)
            wanted = value_type.parse(value_text)
            current = await node.read_data_value()
        except asyncio.CancelledError:
            raise
        except MetadataUnavailable as exc:
            self._fail(result, str(exc))
            return
        except type_support.TypeUnsupported as exc:
            self._fail(result, str(exc))
            return
        except ValueParseError as exc:
            self._fail(result, f"The value was not written: {exc}")
            return
        except Exception as exc:
            self._fail(result, f"{node_id} could not be read: {type(exc).__name__}: {exc}")
            return

        if current.StatusCode.is_good() and _values_match(current.Value.Value, wanted, step.tolerance):
            result.previous_value = preview_value(current.Value.Value)
            result.status = STATUS_SKIPPED
            result.detail = f"{node_id} already has {preview_value(wanted)}; nothing was written."
            return
        await self._write(node_id, value_text, result, dry_run=dry_run, verify=True)

    async def _step_expect(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        await self._read_and_compare(step, result, inputs=inputs, dry_run=dry_run, label="Expectation")

    async def _step_assert(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        await self._read_and_compare(step, result, inputs=inputs, dry_run=dry_run, label="Precondition")

    async def _step_delay(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        try:
            seconds = float(_resolve(step.value, inputs)) if step.value.strip() else float(step.seconds)
        except (SequenceAborted, ValueError) as exc:
            self._fail(result, f"The delay is not a number of seconds: {exc}")
            return
        if dry_run:
            result.status = STATUS_SKIPPED
            result.detail = f"Dry run: would wait {seconds:g} s."
            return
        await self._sleep_or_cancel(seconds, cancel)
        result.status = STATUS_PASSED
        result.detail = f"Waited {seconds:g} s."

    async def _step_wait_until(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        node_id = await self._node_id_for(step, inputs)
        result.node_id = node_id
        expected_text = _resolve(step.expected or step.value, inputs)
        if dry_run:
            result.status = STATUS_SKIPPED
            result.detail = f"Dry run: would wait until {node_id} is {expected_text}."
            return

        deadline = self._clock() + max(0.1, float(step.timeout_seconds))
        poll = max(0.05, float(step.poll_seconds))
        last = "<not read>"
        while True:
            try:
                current, expected = await self._read_comparable(node_id, expected_text)
            except (MetadataUnavailable, ValueParseError) as exc:
                self._fail(result, str(exc))
                return
            except _ReadFailed as exc:
                self._fail(result, str(exc))
                return
            result.new_value = preview_value(expected)
            last = preview_value(current)
            result.previous_value = last
            if _values_match(current, expected, step.tolerance):
                result.status = STATUS_PASSED
                result.detail = f"{node_id} reached {last}."
                return
            if self._clock() >= deadline:
                self._fail(
                    result,
                    f"{node_id} did not reach {preview_value(expected)} within "
                    f"{float(step.timeout_seconds):g} s (last value {last}).",
                )
                return
            await self._sleep_or_cancel(poll, cancel)

    # -- shared step helpers ---------------------------------------------------------

    def _authority(self, result: SequenceStepResult) -> WriteAuthority:
        """The sequence document authorises its own steps; the reference names the step."""
        authorization = WriteAuthority(
            AUTHORITY_SEQUENCE_DOCUMENT, f"{result.phase} {result.step_id}"
        )
        result.authorization = authorization.describe()
        return authorization

    async def _write(
        self,
        node_id: str,
        value_text: str,
        result: SequenceStepResult,
        *,
        dry_run: bool,
        verify: bool,
    ) -> None:
        result.node_id = node_id
        if dry_run:
            try:
                inspection = await write_service.inspect_write(self._client, node_id, value_text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(result, f"{node_id} could not be inspected: {type(exc).__name__}: {exc}")
                return
            if inspection.ok:
                result.new_value = inspection.preview
                result.status = STATUS_SKIPPED
                result.detail = f"Dry run: {inspection.detail}."
            else:
                self._fail(result, inspection.detail)
            return

        authorization = self._authority(result)
        try:
            outcome = await write_service.write_variable(
                self._client, node_id, value_text, authorization=authorization, verify=verify
            )
        except asyncio.CancelledError as exc:
            sent = bool(getattr(exc, "sent", False))
            result.status = STATUS_OUTCOME_UNKNOWN if sent else STATUS_CANCELLED
            result.detail = (
                "Cancelled while the write was in flight; the server may have applied it."
                if sent
                else "Cancelled before the write was sent."
            )
            raise
        result.status = STATUS_FAILED if outcome.status == STATUS_SKIPPED else outcome.status
        result.detail = outcome.detail
        result.previous_value = outcome.previous_value
        result.new_value = outcome.new_value
        result.status_code = outcome.read_back_status_code or outcome.write_status_code

    async def _read_and_compare(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        label: str,
    ) -> None:
        node_id = await self._node_id_for(step, inputs)
        result.node_id = node_id
        expected_text = _resolve(step.expected or step.value, inputs)
        try:
            current, expected = await self._read_comparable(node_id, expected_text)
        except (MetadataUnavailable, ValueParseError) as exc:
            self._fail(result, str(exc))
            return
        except _ReadFailed as exc:
            self._fail(result, str(exc))
            return

        result.new_value = preview_value(expected)
        result.previous_value = preview_value(current)
        if dry_run:
            result.status = STATUS_SKIPPED
            result.detail = f"Dry run: would compare {node_id} with {preview_value(expected)}."
            return
        if _values_match(current, expected, step.tolerance):
            result.status = STATUS_PASSED
            result.detail = f"{node_id} is {preview_value(current)}."
            return
        self._fail(
            result,
            f"{label} not met: {node_id} is {preview_value(current)}, expected {preview_value(expected)}.",
        )

    async def _read_comparable(self, node_id: str, expected_text: str) -> tuple[Any, Any]:
        """Read a variable and type the expected text against the server's DataType."""
        try:
            node = self._client.get_node(node_id)
            node_class = await node.read_node_class()
            if node_class != ua.NodeClass.Variable:
                raise _ReadFailed(f"{node_id} is a {node_class_name(node_class)}, not a Variable.")
            data_type, value_rank = await read_node_data_type(self._client, node_id)
            value_type = await type_support.resolve_value_type(self._client, data_type, value_rank)
            expected = value_type.parse(expected_text)
            data_value = await node.read_data_value()
        except asyncio.CancelledError:
            raise
        except ua.UaStatusCodeError as exc:
            raise _ReadFailed(f"{node_id} could not be read: {status_name(exc.code)}") from exc
        except type_support.TypeUnsupported as exc:
            raise _ReadFailed(str(exc)) from exc
        except ValueParseError:
            raise
        except Exception as exc:
            raise _ReadFailed(f"{node_id} could not be read: {type(exc).__name__}: {exc}") from exc
        if not data_value.StatusCode.is_good():
            raise _ReadFailed(f"Reading {node_id} returned {status_name(data_value.StatusCode)}.")
        return data_value.Value.Value, expected

    # -- call ------------------------------------------------------------------------

    async def _step_call(
        self,
        step: SequenceStep,
        result: SequenceStepResult,
        *,
        inputs: dict[str, str],
        dry_run: bool,
        cancel: Any,
    ) -> None:
        object_node_id = await self._node_id_for(step, inputs)
        try:
            method_name = _resolve(step.method_name, inputs)
            method_node_id = await write_service.resolve_method_node_id(
                self._client, object_node_id, _resolve(step.method_node_id, inputs), method_name
            )
            arguments = [_resolve(argument, inputs) for argument in step.arguments]
        except SequenceAborted as exc:
            self._fail(result, str(exc))
            return
        except write_service.PrepareError as exc:
            self._fail(result, str(exc))
            return
        result.node_id = object_node_id
        result.new_value = method_node_id

        if dry_run:
            try:
                inspection = await write_service.inspect_call(self._client, object_node_id, method_node_id, arguments)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(result, f"{method_node_id} could not be inspected: {type(exc).__name__}: {exc}")
                return
            if inspection.ok:
                result.status = STATUS_SKIPPED
                result.detail = f"Dry run: {inspection.detail}."
            else:
                self._fail(result, inspection.detail)
            return

        authorization = self._authority(result)
        try:
            outcome = await write_service.call_method(
                self._client, object_node_id, method_node_id, arguments, authorization=authorization
            )
        except asyncio.CancelledError as exc:
            sent = bool(getattr(exc, "sent", False))
            result.status = STATUS_OUTCOME_UNKNOWN if sent else STATUS_CANCELLED
            result.detail = (
                "Cancelled while the call was in flight; the server may have executed it."
                if sent
                else "Cancelled before the call was sent."
            )
            raise
        result.status = STATUS_FAILED if outcome.status == STATUS_SKIPPED else outcome.status
        result.detail = outcome.detail
        result.status_code = outcome.status_code

    # -- timing ----------------------------------------------------------------------

    def _raise_if_cancelled(self, cancel: Any) -> None:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            raise _SequenceCancelled

    async def _sleep_or_cancel(self, seconds: float, cancel: Any) -> None:
        deadline = self._clock() + max(0.0, float(seconds))
        while True:
            self._raise_if_cancelled(cancel)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            await self._sleep(min(0.1, remaining))
