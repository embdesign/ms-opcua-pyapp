"""Headless alarm summary: the ``opcua-conditions`` command.

Reads the state variables of condition objects on demand - ``AckedState``,
``ConfirmedState``, ``EnabledState`` and ``Retain`` - through the same service
the Events tab's alarm summary uses. Nothing is inferred from an event, no state
is cached, and no method is called.

Conditions are named explicitly with ``--condition``, or discovered by listening
to a condition event type for a bounded number of seconds and collecting the
condition NodeIds those events carry. A state the condition does not expose is
reported as "not exposed", never as ``False``.

Exit codes: ``0`` at least one condition was read, ``1`` connection/session
failure, ``2`` unusable arguments, ``3`` no condition was found or the read was
refused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
from .errors import OpcUaExplorerError
from .models import CONDITION_TYPE_NODE_ID, ConditionStateSnapshot, EventConfig, EventNotification
from .opcua.client_service import OpcUaService
from .opcua.event_service import SERVER_OBJECT_NODE_ID
from .opcua.node_ids import normalise_node_id

FORMAT = "opcua-alarm-summary"
FORMAT_VERSION = 1
#: One alarm summary reads at most this many conditions in one command.
MAX_CONDITIONS = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-conditions",
        description=(
            "Read the state of OPC UA condition objects (AckedState, ConfirmedState, EnabledState, Retain)."
        ),
        epilog=(
            "Examples:\n"
            "  opcua-conditions --endpoint opc.tcp://plc:4840 --condition ns=2;s=PumpCondition\n"
            "  opcua-conditions --endpoint opc.tcp://plc:4840 --seconds 5 --json\n"
            "\n"
            "Without --condition the command listens to the condition event type for --seconds and reads the\n"
            "state of every condition those events name. It is read-only: no method is called, nothing is\n"
            "acknowledged and no state is inferred from an event.\n"
            "\n"
            "Passwords are read from --password-env or prompted on a terminal; they are never accepted as\n"
            "command-line arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("target")
    target.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    target.add_argument(
        "--condition",
        action="append",
        metavar="NODEID",
        help="condition object to read; repeatable (omit to discover conditions from events)",
    )
    target.add_argument(
        "--source",
        action="append",
        metavar="NODEID",
        help=f"event source used for discovery (default: the Server object {SERVER_OBJECT_NODE_ID})",
    )
    target.add_argument(
        "--event-type",
        metavar="NODEID",
        default=CONDITION_TYPE_NODE_ID,
        help=f"event type used for discovery (default: ConditionType {CONDITION_TYPE_NODE_ID})",
    )
    target.add_argument(
        "--seconds",
        type=float,
        default=5.0,
        help="discovery window in seconds when --condition is not given (default: 5)",
    )
    parser.add_argument("--json", action="store_true", help="print a versioned JSON document")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _print_text(snapshot: ConditionStateSnapshot) -> None:
    print(snapshot.summary_text())
    for condition in snapshot.conditions:
        print(
            f"{condition.node_id}  {condition.condition_name or '-'}  "
            f"acked={condition.acked_text()}  confirmed={condition.confirmed_text()}  "
            f"enabled={condition.enabled_text()}  retain={condition.retain_text()}  "
            f"severity={condition.last_severity if condition.last_severity is not None else '-'}"
        )
        if condition.missing:
            print(f"  not exposed: {', '.join(condition.missing)}")
        if condition.detail:
            print(f"  {condition.detail}")


def _print_json(snapshot: ConditionStateSnapshot) -> None:
    print(
        json.dumps(
            {"format": FORMAT, "version": FORMAT_VERSION, "snapshot": snapshot.to_dict()},
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


async def _discover_conditions(
    service: OpcUaService, sources: list[str], event_type: str, seconds: float
) -> list[str]:
    """Listen to condition events for a bounded time and collect their condition NodeIds."""
    found: list[str] = []
    seen: set[str] = set()

    def emit(payload: Any) -> None:
        if not isinstance(payload, EventNotification):
            return
        node_id = payload.condition_node_id
        if node_id and node_id not in seen and len(found) < MAX_CONDITIONS:
            seen.add(node_id)
            found.append(node_id)

    start = await service.start_event_monitor(
        sources,
        EventConfig(
            publishing_interval_ms=200.0,
            event_type_node_id=event_type,
            # The standard A&C identity field; without an explicit selection a
            # server may publish only BaseEventType's fields and name no condition.
            select_fields=["ConditionId"],
        ),
        emit=emit,
    )
    try:
        accepted = sum(1 for info in start.sources if info.ok)
        if accepted == 0:
            raise CliError(
                "The server refused every event source for condition discovery: "
                + ", ".join(f"{info.node_id} ({info.status})" for info in start.sources)
            )
        deadline = asyncio.get_running_loop().time() + max(0.0, float(seconds))
        while asyncio.get_running_loop().time() < deadline and len(found) < MAX_CONDITIONS:
            await asyncio.sleep(0.05)
    finally:
        await service.event_monitor_stop()
    return found


async def _run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        node_ids = list(dict.fromkeys(normalise_node_id(text) for text in (args.condition or [])))
        if len(node_ids) > MAX_CONDITIONS:
            raise CliError(f"At most {MAX_CONDITIONS} conditions can be read in one run.")
        if not node_ids:
            sources = [normalise_node_id(text) for text in (args.source or [SERVER_OBJECT_NODE_ID])]
            node_ids = await _discover_conditions(
                service, sources, str(args.event_type or CONDITION_TYPE_NODE_ID), float(args.seconds)
            )
            if not node_ids:
                print(
                    "No condition event named a condition object in the observation window; "
                    "pass --condition NODEID to read one explicitly.",
                    file=sys.stderr,
                )
                return EXIT_INVALID_OUTPUT
        snapshot = await service.read_condition_states(node_ids)
    finally:
        await service.shutdown()

    if args.json:
        _print_json(snapshot)
    else:
        _print_text(snapshot)
    return EXIT_OK if snapshot.conditions else EXIT_INVALID_OUTPUT


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
