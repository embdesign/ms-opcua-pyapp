"""Headless live monitor: the ``opcua-monitor`` command.

Creates one data-change subscription through the same
:class:`~opcua_explorer.opcua.client_service.OpcUaService` the GUI uses and
prints notifications until ``--count`` is reached or ``--seconds`` elapse. It
never writes and never retries a notification; stopping or exiting deletes the
subscription.

Exit codes: ``0`` at least one notification was received, ``1`` connection or
subscription failure, ``2`` unusable arguments/configuration, ``3`` the run ended
without any notification.
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
from .defaults import DEFAULT_PUBLISHING_INTERVAL_MS, DEFAULT_SAMPLING_INTERVAL_MS
from .errors import OpcUaExplorerError
from .formatting import flatten_for_csv
from .models import MonitorConfig, MonitorNotification, MonitorStatusEvent
from .opcua.client_service import OpcUaService

_VALUE_CELL_CHARS = 300


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-monitor",
        description="Subscribe to data changes of one or more OPC UA nodes and print each notification.",
        epilog=(
            "Examples:\n"
            "  opcua-monitor --endpoint opc.tcp://192.168.1.100:4840 --node ns=2;s=Temperature --seconds 30\n"
            "  opcua-monitor --endpoint opc.tcp://plc:4840 --node ns=2;s=Speed --count 5 --json\n"
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
        "--node",
        action="append",
        required=True,
        metavar="NODEID",
        help="NodeId to monitor; repeat for several nodes",
    )
    parser.add_argument(
        "--publishing-interval",
        type=float,
        default=DEFAULT_PUBLISHING_INTERVAL_MS,
        metavar="MS",
        help=f"requested publishing interval in ms (default: {DEFAULT_PUBLISHING_INTERVAL_MS:.0f})",
    )
    parser.add_argument(
        "--sampling-interval",
        type=float,
        default=DEFAULT_SAMPLING_INTERVAL_MS,
        metavar="MS",
        help=f"requested sampling interval in ms (default: {DEFAULT_SAMPLING_INTERVAL_MS:.0f})",
    )
    parser.add_argument("--queue-size", type=int, default=0, metavar="N", help="server queue size per item (0 = default)")
    parser.add_argument("--seconds", type=float, default=10.0, help="run duration in seconds (default: 10)")
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        metavar="N",
        help="stop after N notifications; 0 = run for --seconds (default: 0)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object per notification")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _notification_line(notification: MonitorNotification) -> str:
    value = flatten_for_csv(notification.value)
    if len(value) > _VALUE_CELL_CHARS:
        value = value[: _VALUE_CELL_CHARS - 1] + "…"
    stamp = notification.source_timestamp or notification.server_timestamp or notification.received_at.isoformat()
    return f"{notification.node_id}  {notification.status}  {value}  {stamp}"


async def _run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    config = MonitorConfig(
        publishing_interval_ms=float(args.publishing_interval),
        sampling_interval_ms=float(args.sampling_interval),
        queue_size=int(args.queue_size),
    ).normalised()

    received = 0

    def emit(payload: Any) -> None:
        nonlocal received
        if isinstance(payload, MonitorNotification):
            received += 1
            if args.json:
                print(json.dumps(payload.to_dict(), ensure_ascii=False, default=str), flush=True)
            else:
                print(_notification_line(payload), flush=True)
        elif isinstance(payload, MonitorStatusEvent):
            detail = f" {payload.detail}" if payload.detail else ""
            print(f"subscription: {payload.status}{detail}", file=sys.stderr, flush=True)

    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        start = await service.start_monitor(args.node, config, emit=emit)
        accepted = sum(1 for item in start.items if item.ok)
        progress.note(f"Monitoring {accepted} of {len(start.items)} item(s) for at most {args.seconds:.0f} s")
        for item in start.items:
            if not item.ok:
                progress.note(f"{item.node_id} was rejected: {item.status} {item.detail}".rstrip())
        if accepted == 0:
            return EXIT_FAILED

        deadline = time.monotonic() + max(0.0, float(args.seconds))
        while time.monotonic() < deadline:
            if args.count > 0 and received >= args.count:
                break
            await asyncio.sleep(0.05)
    finally:
        try:
            await service.monitor_stop()
        finally:
            await service.shutdown()

    progress.note(f"Received {received} notification(s)")
    return EXIT_OK if received > 0 else EXIT_INVALID_OUTPUT


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
