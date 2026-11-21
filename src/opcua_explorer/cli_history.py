"""Headless raw history: the ``opcua-history`` command.

Reads one variable's raw history through the same service the History tab uses
(one bounded ``HistoryRead``, continuation points released, never a write). Output
is text, a versioned JSON document or CSV rows.

Exit codes: ``0`` a Good HistoryRead, ``1`` connection/session failure, ``2``
unusable arguments/configuration, ``3`` a Bad HistoryRead status.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import UTC, datetime

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
from .defaults import DEFAULT_HISTORY_MAX_VALUES, MAX_HISTORY_MAX_VALUES
from .errors import OpcUaExplorerError
from .formatting import flatten_for_csv
from .models import HistoryConfig, HistoryResult
from .opcua.client_service import OpcUaService
from .opcua.node_ids import normalise_node_id

FORMAT = "opcua-history"
FORMAT_VERSION = 1


def _parse_time(text: str | None, name: str) -> datetime | None:
    if not text:
        return None
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CliError(f"{name} must be an ISO-8601 timestamp, for example 2026-11-21T10:00:00Z.") from exc
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-history",
        description="Read the raw history of one OPC UA variable and print it as text, JSON or CSV.",
        epilog=(
            "Examples:\n"
            "  opcua-history --endpoint opc.tcp://192.168.1.100:4840 --node ns=2;s=Temperature\n"
            "  opcua-history --endpoint opc.tcp://plc:4840 --node ns=2;s=Speed "
            "--from 2026-11-21T00:00:00Z --csv\n"
            "\n"
            "Passwords are read from --password-env or prompted on a terminal; they are never "
            "accepted as command-line arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("target")
    target.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    target.add_argument("--node", required=True, metavar="NODEID", help="NodeId of the historizing variable")
    target.add_argument("--from", dest="start", metavar="ISO", help="start of the range (default: open)")
    target.add_argument("--to", dest="end", metavar="ISO", help="end of the range (default: open)")
    target.add_argument(
        "--max-values",
        type=int,
        default=DEFAULT_HISTORY_MAX_VALUES,
        metavar="N",
        help=f"client-side value cap, at most {MAX_HISTORY_MAX_VALUES} (default: {DEFAULT_HISTORY_MAX_VALUES})",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print a versioned JSON document")
    output.add_argument("--csv", action="store_true", help="print CSV rows (header included)")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _write_csv(result: HistoryResult) -> None:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["source_timestamp", "server_timestamp", "status", "variant_type", "value"])
    for snapshot in result.values:
        writer.writerow(
            [
                snapshot.source_timestamp or "",
                snapshot.server_timestamp or "",
                snapshot.status,
                snapshot.variant_type or "",
                flatten_for_csv(snapshot.value),
            ]
        )


def _print_result(result: HistoryResult, *, as_json: bool, as_csv: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {"format": FORMAT, "version": FORMAT_VERSION, "result": result.to_dict()},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return
    if as_csv:
        _write_csv(result)
        return
    print(result.summary_text())
    for snapshot in result.values:
        stamp = snapshot.source_timestamp or snapshot.server_timestamp or ""
        print(f"{stamp}  {snapshot.status}  {flatten_for_csv(snapshot.value)}")


async def _run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    config = HistoryConfig(
        start=_parse_time(args.start, "--from"),
        end=_parse_time(args.end, "--to"),
        max_values=int(args.max_values),
    ).normalised()
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        result = await service.read_history(normalise_node_id(args.node), config)
    finally:
        await service.shutdown()

    _print_result(result, as_json=args.json, as_csv=args.csv)
    return EXIT_OK if result.status == "Good" else EXIT_INVALID_OUTPUT


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
