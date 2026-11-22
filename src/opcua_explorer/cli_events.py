"""Headless events: the ``opcua-events`` command.

Creates one event subscription through the same
:class:`~opcua_explorer.opcua.client_service.OpcUaService` the Events tab uses,
prints every event until ``--count`` is reached or ``--seconds`` elapse, and
stops. It is the headless twin of that page, including its condition actions:

* ``--refresh`` asks the server once to re-publish the conditions it already
  retains (``ConditionRefresh``) and reports whether ``RefreshStart``/
  ``RefreshEnd`` arrived;
* ``--acknowledge EVENTID`` is the shortcut for one ``Acknowledge`` call, and
  ``--condition-action ACTION --eventid HEX`` reaches the other condition methods
  (``confirm``, ``enable``, ``disable``, ``comment``); every one of them needs
  ``--condition`` and ``--confirm``.

Neither action is retried, one action per run is sent, and the JSON event objects
carry the EventId and condition NodeId needed to act on a condition found by an
earlier run.

Exit codes: ``0`` at least one event was received, ``1`` connection or
subscription failure, ``2`` unusable arguments/configuration, ``3`` the run ended
without any event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from . import __version__
from .cli import (
    EXIT_FAILED,
    EXIT_INVALID_OUTPUT,
    EXIT_OK,
    EXIT_USAGE,
    CliError,
    ProgressPrinter,
    _configure_logging,
    add_connection_arguments,
    add_output_arguments,
    approve_server_certificate,
    settings_from_args,
)
from .config import TrustStore
from .defaults import DEFAULT_EVENT_PUBLISHING_INTERVAL_MS
from .errors import OpcUaExplorerError
from .exporters.observation_log import write_observation_log
from .models import (
    CONDITION_ACTIONS,
    STATUS_PASSED,
    ConditionRefreshResult,
    EventConfig,
    EventNotification,
    EventStart,
    condition_action_label,
)
from .opcua.client_service import OpcUaService
from .opcua.event_service import SERVER_OBJECT_NODE_ID
from .opcua.node_ids import normalise_node_id
from .operator_text import condition_action_result_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-events",
        description="Subscribe to the events of one or more OPC UA event sources and print them.",
        epilog=(
            "Examples:\n"
            "  opcua-events --endpoint opc.tcp://192.168.1.100:4840 --seconds 30\n"
            "  opcua-events --endpoint opc.tcp://plc:4840 --event-type i=2782 --select Severity "
            "--min-severity 500 --refresh --json\n"
            "  opcua-events --endpoint opc.tcp://plc:4840 --acknowledge 0102ff "
            "--condition ns=2;s=PumpCondition --comment checked --confirm\n"
            "\n"
            "Passwords are read from --password-env or prompted on a terminal; they are never "
            "accepted as command-line arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("target")
    target.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    target.add_argument(
        "--source",
        action="append",
        metavar="NODEID",
        help=f"event source NodeId; repeat for several sources (default: the Server object {SERVER_OBJECT_NODE_ID})",
    )
    target.add_argument(
        "--event-type",
        metavar="NODEID",
        default="",
        help="event type whose fields are requested (default: BaseEventType, i.e. every event)",
    )

    parser.add_argument(
        "--publishing-interval",
        type=float,
        default=DEFAULT_EVENT_PUBLISHING_INTERVAL_MS,
        metavar="MS",
        help=f"requested publishing interval in ms (default: {DEFAULT_EVENT_PUBLISHING_INTERVAL_MS:.0f})",
    )
    parser.add_argument("--queue-size", type=int, default=0, metavar="N", help="server queue size per item (0 = default)")
    parser.add_argument(
        "--select",
        action="append",
        metavar="NAME",
        help="extra EventType property to request, e.g. ConditionClassId or 2:Temperature; repeatable",
    )
    parser.add_argument(
        "--min-severity",
        type=int,
        default=None,
        metavar="N",
        help="server-side filter: only events with at least this Severity",
    )
    parser.add_argument("--seconds", type=float, default=10.0, help="run duration in seconds (default: 10)")
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        metavar="N",
        help="stop after N events; 0 = run for --seconds (default: 0)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object per event")
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="also write the received events to FILE when the run ends (.csv or versioned JSON)",
    )

    condition = parser.add_argument_group("condition actions")
    condition.add_argument(
        "--refresh",
        action="store_true",
        help="send one ConditionRefresh after the subscription starts and report its refresh events",
    )
    condition.add_argument("--acknowledge", metavar="EVENTID", help="EventId (hex) to acknowledge, sent once")
    condition.add_argument(
        "--condition-action",
        choices=list(CONDITION_ACTIONS),
        metavar="ACTION",
        help="one condition method to send once: " + ", ".join(CONDITION_ACTIONS),
    )
    condition.add_argument(
        "--eventid", metavar="HEX", help="EventId (hex) for --condition-action"
    )
    condition.add_argument("--condition", metavar="NODEID", help="condition node the call targets")
    condition.add_argument("--comment", default="", metavar="TEXT", help="comment sent with the condition call")
    condition.add_argument(
        "--confirm",
        action="store_true",
        help="required for a condition call: without it nothing is sent",
    )

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _event_line(notification: EventNotification) -> str:
    severity = "" if notification.severity is None else str(notification.severity)
    source = notification.source_name or notification.source_node
    return (
        f"{notification.time or notification.received_at.isoformat()}  {severity:>5}  "
        f"{notification.event_type}  {source}  {notification.message}"
    ).rstrip()


def _print_refresh(result: ConditionRefreshResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"condition_refresh": result.to_dict()}, ensure_ascii=False, default=str), flush=True)
        return
    print(f"condition-refresh: {result.summary_text()}", file=sys.stderr, flush=True)


def _condition_plan(args: argparse.Namespace) -> tuple[str, str, str] | None:
    """The one condition call this invocation asks for: ``(action, event_id, condition)``.

    Everything is validated *before* the command connects, so an incomplete or
    unconfirmed request sends nothing. ``--acknowledge EVENTID`` stays the shortcut
    for the acknowledge action; ``--condition-action`` covers the other four.
    """
    action = args.condition_action
    if args.acknowledge and action:
        raise CliError(
            "--acknowledge and --condition-action both name a condition call; use one of them."
        )
    if action and action not in CONDITION_ACTIONS:
        raise CliError(f"{action!r} is not a condition action; use one of {', '.join(CONDITION_ACTIONS)}.")
    if not args.acknowledge and not action:
        if args.condition or args.comment or args.eventid:
            raise CliError(
                "--condition, --comment and --eventid only apply together with --acknowledge "
                "or --condition-action."
            )
        if args.confirm:
            raise CliError(
                "--confirm only applies together with a condition call; nothing was sent."
            )
        return None

    label = "--acknowledge" if args.acknowledge else f"--condition-action {action}"
    if not args.condition:
        raise CliError(f"{label} needs --condition with the NodeId of the condition object.")
    if args.acknowledge and args.eventid:
        raise CliError("--eventid only applies to --condition-action; --acknowledge carries its own EventId.")
    event_id = str(args.acknowledge or args.eventid or "")
    if not event_id:
        raise CliError(f"{label} needs --eventid with the EventId (hex) of the event to act on.")
    if not args.confirm:
        raise CliError(
            f"{label} sends one condition method call; repeat the command with --confirm to allow it."
        )
    return str(action or "acknowledge"), event_id, str(args.condition)


def _check_condition_arguments(args: argparse.Namespace) -> None:
    """Refuse an incomplete or unconfirmed condition call before anything connects."""
    _condition_plan(args)


async def _condition_call(service: OpcUaService, args: argparse.Namespace) -> int:
    """Send the one confirmed condition method call and return the exit code."""
    plan = _condition_plan(args)
    if plan is None:  # pragma: no cover - _run only calls this with a plan
        return EXIT_OK
    action, event_id, condition_node_id = plan
    label = condition_action_label(action)
    outcome = await service.condition_action(
        condition_node_id,
        event_id,
        str(args.comment or ""),
        action=action,
        reference=f"opcua-events {label.lower()} {condition_node_id}",
    )
    if args.json:
        print(
            json.dumps({action: outcome.__dict__}, ensure_ascii=False, default=str),
            flush=True,
        )
    else:
        print(f"{label.lower()}: {outcome.detail or outcome.status}", file=sys.stderr, flush=True)
    # Safety-relevant, so it is printed even with --quiet: --quiet silences progress,
    # not what was done.
    print(
        condition_action_result_text(
            action=action, condition_node_id=condition_node_id, event_id=event_id
        ),
        file=sys.stderr,
        flush=True,
    )
    return EXIT_OK if outcome.status == STATUS_PASSED else EXIT_INVALID_OUTPUT


async def _run(args: argparse.Namespace) -> int:
    _check_condition_arguments(args)
    settings = settings_from_args(args)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    sources = [normalise_node_id(source) for source in (args.source or [SERVER_OBJECT_NODE_ID])]
    config = EventConfig(
        publishing_interval_ms=float(args.publishing_interval),
        queue_size=int(args.queue_size),
        event_type_node_id=str(args.event_type or ""),
        select_fields=[str(text) for text in (args.select or [])],
        min_severity=None if args.min_severity is None else int(args.min_severity),
    ).normalised()

    received = 0
    collected: list[EventNotification] = []
    refresh_result: ConditionRefreshResult | None = None
    condition_plan = _condition_plan(args)
    condition_code = EXIT_OK

    def emit(payload: Any) -> None:
        nonlocal received
        if not isinstance(payload, EventNotification):
            detail = getattr(payload, "detail", "")
            print(f"subscription: {payload.status} {detail}".rstrip(), file=sys.stderr, flush=True)
            return
        received += 1
        collected.append(payload)
        if args.json:
            print(json.dumps(payload.to_dict(), ensure_ascii=False, default=str), flush=True)
        else:
            print(_event_line(payload), flush=True)

    start: EventStart | None = None
    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        start = await service.start_event_monitor(sources, config, emit=emit)
        accepted = sum(1 for info in start.sources if info.ok)
        progress.note(f"Event subscription {start.subscription_id}: {accepted} of {len(start.sources)} source(s) active")
        for info in start.sources:
            if not info.ok:
                progress.note(f"{info.node_id} was rejected: {info.status} {info.detail}".rstrip())
        if accepted == 0:
            return EXIT_FAILED

        if args.refresh:
            refresh_result = await service.refresh_event_conditions(reference="opcua-events --refresh")
            _print_refresh(refresh_result, as_json=args.json)
        if condition_plan is not None:
            condition_code = await _condition_call(service, args)

        deadline = time.monotonic() + max(0.0, float(args.seconds))
        while time.monotonic() < deadline:
            if args.count > 0 and received >= args.count:
                break
            await asyncio.sleep(0.05)
    finally:
        try:
            await service.event_monitor_stop()
        finally:
            await service.shutdown()

    progress.note(f"Received {received} event(s)")
    if args.output:
        written = write_observation_log(
            args.output,
            kind="events",
            payloads=collected,
            endpoint_url=settings.endpoint_url,
        )
        progress.note(f"{len(collected)} event(s) written to {written}")
    # A run that asked for a condition action is judged by that action as well: the
    # events themselves are the subscription's own output and may legitimately be
    # empty (a server publishes on its own interval).
    codes: list[int] = []
    if refresh_result is not None:
        codes.append(EXIT_OK if refresh_result.status == STATUS_PASSED else EXIT_INVALID_OUTPUT)
    if condition_plan is not None:
        codes.append(condition_code)
    if not codes:
        codes.append(EXIT_OK if received > 0 else EXIT_INVALID_OUTPUT)
    for code in codes:
        if code != EXIT_OK:
            return code
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args)
    try:
        return asyncio.run(_run(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OpcUaExplorerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
