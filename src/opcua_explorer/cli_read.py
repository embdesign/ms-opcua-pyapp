"""Headless read: the ``opcua-read`` command.

Reads one attribute (Value by default) from one or more nodes through the same
:class:`~opcua_explorer.opcua.client_service.OpcUaService` the GUI uses. It is a
front end, not a second implementation: connection settings, certificate
approval and output flags come from :mod:`opcua_explorer.cli`.

Exit codes: ``0`` every requested attribute was read with a Good status, ``1``
connection or session failure, ``2`` unusable arguments/configuration, ``3`` at
least one read returned a Bad status or an unusable attribute.
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
from .formatting import flatten_for_csv
from .opcua.client_service import OpcUaService
from .opcua.node_ids import normalise_node_id

#: Attributes this command can read. ``Value`` uses the value path; everything
#: else comes from the node-details read (the same attributes the browser shows).
ATTRIBUTES = (
    "Value",
    "NodeId",
    "NodeClass",
    "BrowseName",
    "DisplayName",
    "Description",
    "DataType",
    "ValueRank",
    "AccessLevel",
    "UserAccessLevel",
    "TypeDefinition",
)

FORMAT = "opcua-read"
FORMAT_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-read",
        description="Read one attribute (Value by default) from one or more OPC UA nodes and print it.",
        epilog=(
            "Examples:\n"
            "  opcua-read --endpoint opc.tcp://192.168.1.100:4840 --node ns=2;s=Temperature\n"
            "  opcua-read --endpoint opc.tcp://plc:4840 --node i=85 --attribute DisplayName --json\n"
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
        help="NodeId to read; repeat for several nodes",
    )
    target.add_argument(
        "--attribute",
        choices=ATTRIBUTES,
        default="Value",
        help="attribute to read (default: Value)",
    )
    parser.add_argument("--json", action="store_true", help="print a versioned JSON document instead of lines")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


async def _read_one(service: OpcUaService, node_id: str, attribute: str) -> dict[str, Any]:
    canonical = normalise_node_id(node_id)
    if attribute == "Value":
        snapshot = await service.read_value(canonical)
        return {
            "node_id": canonical,
            "attribute": attribute,
            "status": snapshot.status,
            "value": snapshot.value,
            "detail": snapshot.to_dict(),
        }

    details = await service.read_node_details(canonical)
    match = next(
        (item for item in details.attributes if item.attribute.lower() == attribute.lower()),
        None,
    )
    if match is None:
        return {
            "node_id": canonical,
            "attribute": attribute,
            "status": "BadAttributeIdInvalid",
            "value": None,
            "detail": f"{attribute} is not available in the node details of {canonical}.",
        }
    value = match.display if match.display is not None else match.value
    return {
        "node_id": canonical,
        "attribute": attribute,
        "status": match.status,
        "value": value,
        "detail": match.value,
    }


def _print_reads(reads: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {"format": FORMAT, "version": FORMAT_VERSION, "reads": reads},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return
    for read in reads:
        value = flatten_for_csv(read["value"])
        print(f"{read['node_id']}  {read['attribute']}: {value}  [{read['status']}]")


async def _run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    reads: list[dict[str, Any]] = []
    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        for node_id in args.node:
            try:
                reads.append(await _read_one(service, node_id, args.attribute))
            except OpcUaExplorerError as exc:
                reads.append(
                    {
                        "node_id": node_id,
                        "attribute": args.attribute,
                        "status": "Error",
                        "value": None,
                        "detail": str(exc),
                    }
                )
    finally:
        await service.shutdown()

    _print_reads(reads, as_json=args.json)
    return EXIT_OK if all(read["status"] == "Good" for read in reads) else EXIT_INVALID_OUTPUT


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
