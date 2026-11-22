"""Headless server status: the ``opcua-status`` command.

Reads the same one-shot ServerStatus/ServerDiagnostics snapshot the **Server** tab
shows (`read_server_status()`), so an operations script can check that a server is
reachable, see its state and uptime, and read the capability limits this client
depends on — without opening the GUI.

Read-only: one batched `Read`, no write, no method call (`GetMonitoredItems` is
never invoked). A Good StatusCode with a null value is reported as *not provided*
and a Bad StatusCode per value; neither is an error.

Exit codes: ``0`` the snapshot was read with a Good status, ``1`` connection or
session failure, ``2`` unusable arguments/configuration, ``3`` the snapshot was read
but the status variables were not usable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

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
from .formatting import format_duration
from .models import ServerStatusSnapshot
from .opcua.client_service import OpcUaService

FORMAT = "opcua-status"
FORMAT_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-status",
        description="Read ServerStatus, the diagnostics summary and the capability limits of one OPC UA server.",
        epilog=(
            "Examples:\n"
            "  opcua-status --endpoint opc.tcp://192.168.1.100:4840\n"
            "  opcua-status --endpoint opc.tcp://plc:4840 --json\n"
            "  opcua-status --endpoint opc.tcp://plc:4840 --limits-only\n"
            "\n"
            "Read-only: one batched Read, no write and no method call. Passwords are read from "
            "--password-env or prompted on a terminal; they are never accepted as command-line "
            "arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    parser.add_argument(
        "--limits-only",
        action="store_true",
        help="print only the ServerCapabilities limits this client depends on",
    )
    parser.add_argument("--json", action="store_true", help="print a versioned JSON document")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _text(snapshot: ServerStatusSnapshot, *, limits_only: bool) -> str:
    lines: list[str] = []
    if not limits_only:
        lines.append(snapshot.summary_text())
        lines.append(f"{'State':<24}{snapshot.state}")
        if snapshot.current_time:
            lines.append(f"{'Current time':<24}{snapshot.current_time}")
        if snapshot.start_time:
            lines.append(f"{'Start time':<24}{snapshot.start_time}")
        if snapshot.uptime_seconds is not None:
            lines.append(f"{'Uptime':<24}{format_duration(snapshot.uptime_seconds)}")
        if snapshot.seconds_till_shutdown is not None:
            lines.append(f"{'Seconds till shutdown':<24}{snapshot.seconds_till_shutdown}")
        if snapshot.shutdown_reason:
            lines.append(f"{'Shutdown reason':<24}{snapshot.shutdown_reason}")
        if snapshot.diagnostics_enabled is not None:
            lines.append(f"{'Diagnostics enabled':<24}{'yes' if snapshot.diagnostics_enabled else 'no'}")
        if snapshot.counters:
            lines.append("")
            lines.append("Diagnostics counters")
            for name, value in snapshot.counters.items():
                lines.append(f"  {name:<26}{value}")
        if snapshot.not_provided:
            lines.append("")
            lines.append(f"Not provided by the server ({len(snapshot.not_provided)}): {', '.join(snapshot.not_provided)}")
        if snapshot.unavailable:
            lines.append("")
            lines.append("Unavailable (Bad StatusCode)")
            for name, status in snapshot.unavailable.items():
                lines.append(f"  {name:<26}{status}")
    if snapshot.limits:
        if lines:
            lines.append("")
        lines.append("Capability limits")
        for name, value in snapshot.limits.items():
            lines.append(f"  {name:<26}{value}")
    return "\n".join(lines) if lines else "The server returned no status variables."


async def _run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    if settings.is_secure:
        await approve_server_certificate(service, settings, args, trust_store, progress)
    await service.connect(settings)
    try:
        snapshot = await service.server_status()
    finally:
        await service.shutdown()

    if args.json:
        print(
            json.dumps(
                {"format": FORMAT, "version": FORMAT_VERSION, "status": snapshot.to_dict()},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
    else:
        print(_text(snapshot, limits_only=args.limits_only))
    # A state the server never reported is not a failure; an unusable state is.
    return EXIT_OK if snapshot.state not in ("Unknown", "") else EXIT_INVALID_OUTPUT


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
