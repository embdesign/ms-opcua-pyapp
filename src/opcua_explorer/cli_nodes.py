"""Headless node management: the ``opcua-nodes`` command.

One structural change per invocation — exact
:meth:`~opcua_explorer.opcua.client_service.OpcUaService.create_node` /
`delete_node` calls the **Write & call** page makes:

* ``--create`` sends one ``AddNodes``; the server assigns the NodeId and the new
  node is read back;
* ``--delete`` sends one ``DeleteNodes``; the node is read again to verify it is
  gone.

Both are guarded operations: nothing is sent without ``--confirm``, there is no
batch, no undo, no retry, and the target is never discovered — it is named here.
``--attribute`` writes ``DisplayName``/``Description`` through the one
``write_attribute_value`` path instead (also ``--confirm``).

Exit codes: ``0`` the change was sent and verified, ``1`` connection/session
failure, ``2`` unusable arguments or a missing ``--confirm``, ``3`` the server
refused it or the verification failed.
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
from .models import (
    CREATABLE_NODE_CLASSES,
    STATUS_PASSED,
    WRITABLE_TEXT_ATTRIBUTES,
    NodeCreateRequest,
)
from .opcua.client_service import OpcUaService
from .opcua.node_ids import normalise_node_id

FORMAT = "opcua-nodes"
FORMAT_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-nodes",
        description="Create, delete or re-label one OPC UA node, with one confirmed request.",
        epilog=(
            "Examples:\n"
            "  opcua-nodes --endpoint opc.tcp://plc:4840 --create Object --parent i=85 "
            "--name Line3 --confirm\n"
            "  opcua-nodes --endpoint opc.tcp://plc:4840 --create Variable --parent ns=2;s=Line3 "
            "--name Setpoint --data-type i=11 --value 21.5 --confirm\n"
            "  opcua-nodes --endpoint opc.tcp://plc:4840 --delete ns=2;s=Line3 --confirm\n"
            "  opcua-nodes --endpoint opc.tcp://plc:4840 --node ns=2;s=Line3 "
            "--attribute Description --text 'Line 3' --confirm\n"
            "\n"
            "One node per invocation, never a batch, and never without --confirm. Passwords are "
            "read from --password-env or prompted on a terminal."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    parser.add_argument("--json", action="store_true", help="print a versioned JSON document")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required: without it nothing is created, deleted or written",
    )

    create = parser.add_argument_group("create one node")
    create.add_argument("--create", choices=CREATABLE_NODE_CLASSES, help="node class to create")
    create.add_argument("--parent", metavar="NODEID", help="parent node the new node is added below")
    create.add_argument("--name", metavar="BROWSENAME", help="BrowseName of the new node")
    create.add_argument("--display", metavar="TEXT", help="DisplayName (default: the BrowseName)")
    create.add_argument("--data-type", metavar="NODEID", help="DataType for a Variable (default: i=11, Double)")
    create.add_argument("--value", metavar="TEXT", help="initial value for a Variable (parsed with the DataType)")
    create.add_argument(
        "--namespace",
        type=int,
        default=-1,
        metavar="N",
        help="namespace for the new node (default: -1 = the parent's namespace)",
    )

    delete = parser.add_argument_group("delete one node")
    delete.add_argument("--delete", metavar="NODEID", help="node to delete (references go with it)")

    attribute = parser.add_argument_group("re-label one node")
    attribute.add_argument("--node", metavar="NODEID", help="node whose metadata attribute is written")
    attribute.add_argument("--attribute", choices=WRITABLE_TEXT_ATTRIBUTES, help="metadata attribute to write")
    attribute.add_argument("--text", metavar="TEXT", help="new text for the attribute")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _validate(args: argparse.Namespace) -> str:
    """Check the argument combination; returns the single action to perform."""
    actions = [bool(args.create), bool(args.delete), bool(args.attribute)]
    if sum(actions) != 1:
        raise CliError("choose exactly one action: --create, --delete or --attribute.")
    if args.create:
        if not args.parent or not args.name:
            raise CliError("--create needs --parent and --name.")
        if args.node or args.text:
            raise CliError("--node and --text only apply with --attribute.")
        normalise_node_id(args.parent)
        return "create"
    if args.delete:
        if args.parent or args.name or args.data_type or args.value or args.display:
            raise CliError("--delete takes only the NodeId to remove.")
        if args.node or args.text:
            raise CliError("--node and --text only apply with --attribute.")
        normalise_node_id(args.delete)
        return "delete"
    if not args.node or not args.text:
        raise CliError("--attribute needs --node and --text.")
    if args.parent or args.name or args.data_type or args.value or args.display:
        raise CliError("--parent/--name/--data-type/--value only apply with --create.")
    normalise_node_id(args.node)
    return "attribute"


def _print(document: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, indent=2, ensure_ascii=False, default=str))
        return
    print(document["detail"])


async def _run(args: argparse.Namespace) -> int:
    action = _validate(args)
    if not args.confirm:
        raise CliError(
            f"--{action} changes the server; repeat the command with --confirm to allow this one request. "
            "Nothing was sent."
        )

    settings = settings_from_args(args)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    outcome = None
    document: dict = {"format": FORMAT, "version": FORMAT_VERSION, "action": action}
    detail = ""
    passed = False

    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        try:
            if action == "create":
                request = NodeCreateRequest(
                    parent_node_id=normalise_node_id(args.parent),
                    browse_name=args.name,
                    node_class=args.create,
                    display_name=args.display or args.name,
                    data_type=normalise_node_id(args.data_type) if args.data_type else "i=11",
                    value_text=args.value if args.value is not None else "0",
                    namespace_index=int(args.namespace),
                )
                outcome = await service.create_node(request, reference="opcua-nodes --create")
                document["create"] = outcome.to_dict()
                passed = outcome.status == STATUS_PASSED
            elif action == "delete":
                outcome = await service.delete_node(
                    normalise_node_id(args.delete), reference="opcua-nodes --delete"
                )
                document["delete"] = outcome.to_dict()
                passed = outcome.status == STATUS_PASSED
            else:
                outcome = await service.write_attribute_value(
                    normalise_node_id(args.node),
                    args.attribute,
                    args.text,
                    reference="opcua-nodes --attribute",
                )
                document["attribute"] = outcome.to_dict()
                passed = outcome.status == STATUS_PASSED
        finally:
            await service.shutdown()
    except OpcUaExplorerError as exc:  # a refused NodeId never reaches the server
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    detail = outcome.summary_text() if outcome is not None else "No outcome was reported."
    document["status"] = "passed" if passed else "failed"
    document["detail"] = detail
    _print(document, as_json=args.json)
    progress.note("One request was sent; there is no retry, no batch and no undo.")
    return EXIT_OK if passed else EXIT_INVALID_OUTPUT


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
