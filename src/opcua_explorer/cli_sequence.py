"""``opcua-run-sequence``: run one operator-written sequence document headlessly.

The command is an adapter, not a second implementation: it loads a
``*.opcua-sequence.json`` document with the same parser the GUI uses, connects through
``OpcUaService`` (same certificate approval rules as ``opcua-export``) and calls
``OpcUaService.run_sequence()``. Every step is printed with its StatusCode and read-back
result; nothing is retried and no device is hard-coded.

Exit codes: ``0`` sequence passed, ``1`` a step failed or the gate release failed,
``2`` usage/document error. A dry run sends nothing and uses the same codes: a document that
cannot be resolved fails with ``1``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__
from .cli import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    CliError,
    ProgressPrinter,
    _configure_logging,
    approve_server_certificate,
    settings_from_args,
)
from .config import TrustStore
from .errors import OpcUaExplorerError
from .models import STATUS_PASSED, SequenceRun, SequenceStepResult
from .opcua.client_service import OpcUaService
from .sequence_document import SequenceDocumentError, load_sequence_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-run-sequence",
        description=(
            "Run an operator-written OPC UA sequence document (preconditions, optional gate, body). "
            "Each step is sent once, read back where the document asks for it, and reported."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("sequence and target")
    target.add_argument("sequence", metavar="SEQUENCE", help="path to a *.opcua-sequence.json document")
    target.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    target.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="value for a ${name} placeholder (repeatable; overrides the document's own default)",
    )

    run = parser.add_argument_group("run")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve every node, DataType and method signature and send nothing",
    )

    connection = parser.add_argument_group("security and identity")
    connection.add_argument("--policy", metavar="POLICY", help="security policy name or URI (default: None)")
    connection.add_argument(
        "--mode", choices=("None", "Sign", "SignAndEncrypt"), default="None", help="message security mode"
    )
    connection.add_argument("--client-certificate", metavar="FILE", help="client certificate (.der or .pem)")
    connection.add_argument("--client-key", metavar="FILE", help="client private key (.pem)")
    connection.add_argument("--key-password-env", metavar="VAR", help="environment variable holding the key password")
    connection.add_argument(
        "--certificate-identity",
        action="store_true",
        help="authenticate with the client certificate instead of anonymously",
    )
    connection.add_argument("--user", metavar="NAME", help="user name for user-name authentication")
    connection.add_argument(
        "--password-env",
        metavar="VAR",
        help="environment variable holding the password (else an interactive prompt)",
    )
    connection.add_argument(
        "--server-certificate",
        metavar="FILE",
        help="approved server certificate (.der/.pem); must match what the server advertises",
    )
    connection.add_argument(
        "--trust-server-certificate",
        action="store_true",
        help="approve the advertised certificate for this run and remember it in the trust store",
    )
    connection.add_argument("--trust-store", metavar="FILE", help="trust store file instead of the default")
    connection.add_argument("--timeout", type=float, default=10.0, help="request timeout in seconds")
    connection.add_argument("--connect-timeout", type=float, default=10.0, help="connect timeout in seconds")

    output = parser.add_argument_group("output")
    output.add_argument("--json", metavar="FILE", help="write the run (steps, StatusCodes, gate state) as JSON")
    output.add_argument("--quiet", action="store_true", help="print only the summary")
    output.add_argument("--verbose", action="store_true", help="debug logging on stderr")
    return parser


def parse_inputs(values: list[str]) -> dict[str, str]:
    """``--input NAME=VALUE`` pairs; a missing name or ``=`` is a usage error."""
    inputs: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            raise CliError(f"--input expects NAME=VALUE, got {item!r}")
        inputs[name.strip()] = value
    return inputs


def _step_printer(quiet: bool):
    if quiet:
        return None

    def report(result: SequenceStepResult) -> None:
        line = f"  {result.status:<16} {result.phase:<12} {result.step_id:<24} {result.detail}"
        print(line.rstrip(), file=sys.stderr)

    return report


def _print_run(run: SequenceRun, *, quiet: bool) -> None:
    if not quiet:
        print()
        print(f"Gate: {run.gate_state or 'not involved'}")
    print(run.summary_text())
    if run.dry_run:
        print("Dry run: nothing was sent.")


async def run(args: argparse.Namespace) -> int:
    try:
        sequence = load_sequence_file(args.sequence)
    except SequenceDocumentError as exc:
        raise CliError(str(exc)) from exc
    inputs = parse_inputs(args.input)
    settings = settings_from_args(args)

    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)
        await service.connect(settings)
        progress.note(
            f"Connected to {service.endpoint_url} (session {settings.auth_mode}, policy {args.policy or 'None'})"
        )
        run_result = await service.run_sequence(
            sequence,
            inputs=inputs,
            dry_run=args.dry_run,
            on_step=_step_printer(args.quiet),
        )
    finally:
        await service.shutdown()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as stream:
            json.dump(run_result.to_dict(), stream, indent=2)
            stream.write("\n")
        if not args.quiet:
            print(f"JSON: {args.json}")

    _print_run(run_result, quiet=args.quiet)
    return EXIT_OK if run_result.status == STATUS_PASSED else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns a process exit code, never raises for expected errors."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OpcUaExplorerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - module execution path
    raise SystemExit(main())
