"""Headless guarded write: the ``opcua-write`` command.

One variable and one value, sent exactly once through
:mod:`opcua_explorer.opcua.write_service`. The command always previews first
(the server's DataType/ValueRank are read and the text is parsed), sends only
when ``--confirm`` is given, reads the value back and optionally restores the
previous value as one further verified write. There is no retry and no batch.

Exit codes: ``0`` the write (and restore, when requested) passed, ``1``
connection/session failure, ``2`` unusable arguments or a refused preview, ``3``
the write was sent but failed or its effect could not be verified.
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
from .models import STATUS_PASSED
from .opcua.client_service import OpcUaService
from .opcua.node_ids import normalise_node_id

FORMAT = "opcua-write"
FORMAT_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-write",
        description=(
            "Preview and then send exactly one OPC UA variable write, with a read-back and an optional restore."
        ),
        epilog=(
            "Examples:\n"
            "  opcua-write --endpoint opc.tcp://plc:4840 --node ns=2;s=Temperature --value 42.5\n"
            "  opcua-write --endpoint opc.tcp://plc:4840 --node ns=2;s=Temperature --value 42.5 --confirm\n"
            "  opcua-write --endpoint opc.tcp://plc:4840 --node ns=2;s=Setpoint --value 12 --confirm --restore\n"
            "\n"
            "Without --confirm the value is only validated and nothing is sent. The write is one frame, read back "
            "and never retried. Passwords are read from --password-env or prompted, never from argv."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("target")
    target.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    target.add_argument("--node", required=True, metavar="NODEID", help="NodeId of the variable to write")
    target.add_argument("--value", required=True, metavar="TEXT", help="new value, parsed with the server's DataType")
    target.add_argument(
        "--restore",
        action="store_true",
        help="after a successful write, write the previous value back (one further verified write)",
    )
    target.add_argument(
        "--confirm",
        action="store_true",
        help="required to send; without it the command previews the write and sends nothing",
    )
    parser.add_argument("--json", action="store_true", help="print a versioned JSON document")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _write_document(result: Any) -> dict[str, Any]:
    write = result.write
    restore = result.restore
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "status": result.status,
        "detail": result.summary_text(),
        "write": {
            "node_id": write.node_id,
            "status": write.status,
            "detail": write.detail,
            "sent": write.sent,
            "write_status_code": write.write_status_code,
            "read_back_value": write.read_back_value,
            "read_back_status_code": write.read_back_status_code,
            "previous_value": write.previous_value,
        },
        "restore": None
        if restore is None
        else {
            "node_id": restore.node_id,
            "status": restore.status,
            "detail": restore.detail,
            "sent": restore.sent,
            "written_value": restore.written_value,
            "read_back_value": restore.read_back_value,
            "read_back_status_code": restore.read_back_status_code,
        },
    }


def _print_inspection(inspection: Any) -> None:
    print(f"Preview: {inspection.detail}")
    if inspection.preview:
        print(f"Parsed:  {inspection.preview}")
    if inspection.data_type_label:
        print(f"DataType: {inspection.data_type_label} (ValueRank {inspection.value_rank})")


def _print_result(document: dict[str, Any]) -> None:
    print(f"Write: {document['write']['status']} — {document['write']['detail']}")
    print(f"  sent:      {document['write']['sent']}")
    print(f"  write:     {document['write']['write_status_code'] or '-'}")
    print(f"  read back: {document['write']['read_back_value']} ({document['write']['read_back_status_code'] or '-'})")
    print(f"  previous:  {document['write']['previous_value'] or '-'}")
    if document["restore"] is not None:
        restore = document["restore"]
        print(f"Restore: {restore['status']} — {restore['detail']}")
        print(f"  read back: {restore['read_back_value']} ({restore['read_back_status_code'] or '-'})")


async def _run(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    node_id = normalise_node_id(args.node)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        inspection = await service.write_preview(node_id, args.value)
        if not inspection.ok:
            print(f"Refused: {inspection.detail}", file=sys.stderr)
            if args.json:
                print(
                    json.dumps(
                        {"format": FORMAT, "version": FORMAT_VERSION, "sent": False, "detail": inspection.detail},
                        indent=2,
                    )
                )
            return EXIT_USAGE
        if not args.confirm:
            _print_inspection(inspection)
            print("Nothing was sent. Re-run with --confirm to send this exact write.", file=sys.stderr)
            return EXIT_USAGE

        result = await service.write_node_value(
            node_id,
            args.value,
            restore_previous=args.restore,
            reference="opcua-write",
        )
    finally:
        await service.shutdown()

    document = _write_document(result)
    if args.json:
        print(json.dumps(document, indent=2, ensure_ascii=False, default=str))
    else:
        _print_inspection(inspection)
        _print_result(document)
    return EXIT_OK if document["status"] == STATUS_PASSED else EXIT_INVALID_OUTPUT


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
