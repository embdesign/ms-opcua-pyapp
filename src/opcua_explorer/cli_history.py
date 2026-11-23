"""Headless history: the ``opcua-history`` command.

Reads one variable's raw/processed history through the same service the History
tab uses (one bounded ``HistoryRead``, continuation points released). With
``--update`` it prepares one ``HistoryUpdate`` (insert/replace values or delete a
range), sends it only with ``--confirm``, and prints the read-back evidence.
Output is text, a versioned JSON document or CSV rows.

Exit codes: ``0`` a Good HistoryRead or a passed update, ``1`` connection/session
failure, ``2`` unusable arguments/configuration or a refused preview, ``3`` a Bad
HistoryRead status or a failed/unknown update.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
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
from .cli import (
    parse_iso_time as _parse_time,
)
from .config import TrustStore
from .defaults import DEFAULT_HISTORY_MAX_VALUES, MAX_HISTORY_MAX_VALUES, MAX_HISTORY_UPDATE_ENTRIES
from .errors import OpcUaExplorerError
from .formatting import flatten_for_csv
from .models import (
    HISTORY_AGGREGATES,
    HISTORY_UPDATE_ACTIONS,
    STATUS_PASSED,
    HistoryConfig,
    HistoryResult,
    HistoryUpdateConfig,
    HistoryUpdateEntry,
)
from .opcua.client_service import OpcUaService
from .opcua.node_ids import normalise_node_id
from .trend import build_trend_series, sparkline

FORMAT = "opcua-history"
FORMAT_VERSION = 1
UPDATE_FORMAT = "opcua-history-update"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-history",
        description="Read the raw history of one OPC UA variable and print it as text, JSON or CSV.",
        epilog=(
            "Examples:\n"
            "  opcua-history --endpoint opc.tcp://192.168.1.100:4840 --node ns=2;s=Temperature\n"
            "  opcua-history --endpoint opc.tcp://plc:4840 --node ns=2;s=Speed "
            "--from 2026-11-21T00:00:00Z --csv\n"
            "  opcua-history --endpoint opc.tcp://plc:4840 --node ns=2;s=Temperature "
            "--update insert --entry 2026-11-21T10:00:00Z=21.5 --confirm\n"
            "\n"
            "Without --confirm a --update is only previewed and nothing is sent. A history update\n"
            "changes the server's historian, not the variable's live value, and is never retried.\n"
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
    target.add_argument(
        "--aggregate",
        choices=HISTORY_AGGREGATES,
        help="processed read (requires --from and --to); the server may not support it",
    )
    target.add_argument(
        "--interval",
        dest="processing_interval",
        type=float,
        default=0.0,
        metavar="MS",
        help="processing interval for an aggregate in ms (0 = one value over the range)",
    )
    update = parser.add_argument_group("history update")
    update.add_argument(
        "--update",
        choices=HISTORY_UPDATE_ACTIONS,
        metavar="ACTION",
        help="send one HistoryUpdate: " + ", ".join(HISTORY_UPDATE_ACTIONS),
    )
    update.add_argument(
        "--entry",
        action="append",
        metavar="ISO=VALUE",
        help="stored value for insert/replace, repeatable, e.g. 2026-11-21T10:00:00Z=21.5",
    )
    update.add_argument(
        "--confirm",
        action="store_true",
        help="required for --update: without it the request is only previewed and nothing is sent",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print a versioned JSON document")
    output.add_argument("--csv", action="store_true", help="print CSV rows (header included)")
    parser.add_argument(
        "--sparkline",
        action="store_true",
        help="also print the numeric values as a one-line text trend (text output only)",
    )

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


def _update_config(args: argparse.Namespace) -> HistoryUpdateConfig | None:
    """Validate the one optional HistoryUpdate before anything connects."""
    if not args.update:
        if args.entry or args.confirm:
            raise CliError("--entry and --confirm only apply together with --update; nothing was sent.")
        return None
    if args.csv or args.sparkline:
        raise CliError("--update prints the update result; --csv/--sparkline only apply to a history read.")
    if not args.confirm:
        raise CliError(
            f"--update {args.update} sends one HistoryUpdate; repeat the command with --confirm to allow it."
        )
    entries: list[HistoryUpdateEntry] = []
    for text in args.entry or []:
        stamp_text, separator, value_text = str(text).partition("=")
        if not separator:
            raise CliError(f"--entry needs ISO=VALUE, got {text!r}.")
        moment = _parse_time(stamp_text.strip(), "--entry")
        if moment is None:  # pragma: no cover - an empty stamp is rejected by _parse_time
            raise CliError(f"--entry needs a timestamp before '=', got {text!r}.")
        entries.append(HistoryUpdateEntry(source_timestamp=moment, value_text=value_text.strip()))
    start = _parse_time(args.start, "--from")
    end = _parse_time(args.end, "--to")
    if args.update == "delete":
        if args.entry:
            raise CliError("A history delete takes --from/--to, not --entry.")
        if start is None or end is None:
            raise CliError("A history delete needs --from and --to.")
    else:
        if not entries:
            raise CliError(f"--update {args.update} needs at least one --entry ISO=VALUE.")
        if len(entries) > MAX_HISTORY_UPDATE_ENTRIES:
            raise CliError(f"At most {MAX_HISTORY_UPDATE_ENTRIES} --entry values fit in one HistoryUpdate.")
    return HistoryUpdateConfig(
        action=args.update, node_id=normalise_node_id(args.node), entries=entries, start=start, end=end
    ).normalised()


def _print_update(outcome: Any, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {"format": UPDATE_FORMAT, "version": 1, "result": outcome.to_dict()},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return
    print(outcome.summary_text())
    for status in outcome.operation_results:
        print(f"  operation: {status}")
    if outcome.verification is not None:
        print(f"  read-back: {outcome.verification.summary_text()}")


def _print_result(
    result: HistoryResult, *, as_json: bool, as_csv: bool, as_sparkline: bool = False
) -> None:
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
    if as_sparkline:
        series = build_trend_series(result.values)
        trend = sparkline(series)
        if trend:
            print(f"trend  {trend}")
            print(f"       {series.summary_text()}")
        else:
            print("trend  no numeric values to plot")


async def _run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    update_config = _update_config(args)
    config = HistoryConfig(
        start=_parse_time(args.start, "--from"),
        end=_parse_time(args.end, "--to"),
        max_values=int(args.max_values),
        aggregate=args.aggregate,
        processing_interval_ms=float(args.processing_interval),
    ).normalised()
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    outcome = None
    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        if update_config is not None:
            inspection = await service.history_update_preview(update_config)
            if not inspection.ok:
                print(f"Refused: {inspection.detail}", file=sys.stderr)
                return EXIT_USAGE
            progress.note(f"Preview: {inspection.detail}")
            outcome = await service.write_history(update_config, reference="opcua-history --update")
        else:
            result = await service.read_history(normalise_node_id(args.node), config)
    finally:
        await service.shutdown()

    if outcome is not None:
        _print_update(outcome, as_json=args.json)
        return EXIT_OK if outcome.status == STATUS_PASSED else EXIT_INVALID_OUTPUT
    _print_result(result, as_json=args.json, as_csv=args.csv, as_sparkline=args.sparkline)
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
