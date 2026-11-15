"""Headless address-space comparison: the ``opcua-compare`` command.

Answers one question with evidence: *does the new device expose the same
information model as the old one?* Both address spaces are downloaded through
:class:`~opcua_explorer.opcua.client_service.OpcUaService` (the same code the
GUI *Export* tab uses) and compared by :func:`opcua_explorer.compare.compare_address_spaces`.
Nothing is written, called or subscribed; the command only browses and reads.

This is a second front end, not a second implementation: connection settings,
traversal configuration, certificate approval and progress output come from
:mod:`opcua_explorer.cli`. Security behaviour is therefore identical to
``opcua-export``:

* no silent downgrade — the requested policy/mode must be advertised;
* a server certificate is used only after an explicit decision (trust store,
  ``--trust-server-certificate`` for both endpoints, or a per-side
  ``--reference-server-certificate`` / ``--candidate-server-certificate`` whose
  SHA-256 matches what that server advertises);
* passwords come from an environment variable or an interactive prompt, never
  from the command line.

Both sessions deliberately use the same identity: ``UserAccessLevel`` can only be
compared when both servers are asked with the same user. Different security
settings are possible per side (``--candidate-policy``/``--candidate-mode``) when
the devices advertise different endpoints.

Exit codes: ``0`` no difference found, ``1`` connection or download failure,
``2`` unusable arguments or configuration, ``3`` the comparison completed and
found at least one difference (useful as a CI gate).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys

from . import __version__
from .cli import (
    DEFAULT_ROOT,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    CliError,
    ProgressPrinter,
    _configure_logging,
    _policy_uri,
    approve_server_certificate,
    settings_from_args,
    traversal_config_from_args,
)
from .compare import ComparisonConfig, compare_address_spaces
from .config import ConnectionSettings, TrustStore
from .defaults import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_COMPARISON_DIFFERENCES,
)
from .errors import OpcUaExplorerError
from .exporters import load_json_snapshot
from .models import AddressSpace, TraversalConfig, TraversalProgress
from .opcua.client_service import OpcUaService
from .reporting import write_comparison_html, write_comparison_json

_logger = logging.getLogger(__name__)

#: The comparison finished and found differences (CI signal, not a failure).
EXIT_DIFFERENCES = 3


class _SidePrinter:
    """Prefixes a progress stream with the side whose snapshot is being read."""

    def __init__(self, progress: ProgressPrinter, role: str) -> None:
        self._progress = progress
        self._role = role

    def __call__(self, update: TraversalProgress) -> None:
        self._progress(update)

    def note(self, message: str) -> None:
        self._progress.note(f"[{self._role}] {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-compare",
        description=(
            "Download the address space of two OPC UA servers and compare them structurally: node "
            "existence, node class, data type, value rank, access levels, type definitions, method "
            "signatures and (optionally) values. Read-only: nothing is written, called or subscribed."
        ),
        epilog=(
            "Examples:\n"
            "  opcua-compare --reference opc.tcp://old-device:4840 --candidate opc.tcp://new-device:4840\n"
            "  opcua-compare --reference opc.tcp://old:4840 --candidate opc.tcp://new:4840 \\\n"
            "      --node ns=2;s=MyDevice --json diff.json --report diff.html\n"
            "  opcua-compare --reference-file old-device.json --candidate opc.tcp://new:4840\n"
            "  opcua-compare --reference-file old.json --candidate-file new.json   # offline\n"
            "  opcua-compare --reference opc.tcp://old:4840 --candidate opc.tcp://new:4840 \\\n"
            "      --compare-reference-names --ignore i=2258 --compare-value ns=2;s=Setpoint\n"
            "\n"
            "Each side is either a live endpoint or a saved JSON export (--reference-file / "
            "--candidate-file, as written by the Export tab or opcua-export's JSON output). Two "
            "documents need no connection at all.\n"
            "Both sessions use the same identity on purpose: UserAccessLevel can only be compared "
            "when both servers are asked with the same user. Passwords are read from --password-env "
            "or prompted on a terminal. Exit code 3 means 'compared, differences found'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("targets")
    target.add_argument(
        "--reference",
        metavar="ENDPOINT",
        help="the existing device (baseline); use --reference-file for a saved JSON export instead",
    )
    target.add_argument(
        "--candidate",
        metavar="ENDPOINT",
        help="the replacement to verify; use --candidate-file for a saved JSON export instead",
    )
    target.add_argument(
        "--reference-file",
        metavar="FILE",
        help="JSON address-space export of the reference (instead of --reference); nothing is downloaded there",
    )
    target.add_argument(
        "--candidate-file",
        metavar="FILE",
        help="JSON address-space export of the candidate (instead of --candidate); nothing is downloaded there",
    )

    scope = parser.add_argument_group("download scope (identical for both sides)")
    scope.add_argument(
        "--node",
        action="append",
        metavar="NODEID",
        help=f"start NodeId; repeat for several roots (default: {DEFAULT_ROOT} = Objects folder)",
    )
    scope.add_argument(
        "--namespace",
        action="append",
        type=int,
        metavar="INDEX",
        help="follow only these namespace indexes; repeat for several (namespace 0 is always kept)",
    )
    scope.add_argument("--max-depth", type=int, default=0, metavar="N", help="download depth, 0 = unlimited")
    scope.add_argument(
        "--max-nodes",
        type=int,
        default=0,
        metavar="N",
        help="node budget per side, 0 = unlimited (default: unlimited — a truncated side cannot be compared)",
    )
    scope.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="concurrent Browse requests")
    scope.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="nodes per Browse request")
    scope.add_argument(
        "--values",
        action="store_true",
        help=(
            "read variable values during the download (implied by --compare-value); the comparison "
            "still compares values only for the nodes named with --compare-value"
        ),
    )

    comparison = parser.add_argument_group("comparison")
    comparison.add_argument(
        "--tolerance",
        choices=("functional", "strict"),
        default="functional",
        help=(
            "functional (default): node class, data type, value rank, array dimensions, access levels, "
            "type definition; strict: also display name and description"
        ),
    )
    comparison.add_argument(
        "--compare-value",
        action="append",
        metavar="NODEID",
        help="compare the value of this node (status, variant type, payload); repeat for several",
    )
    comparison.add_argument(
        "--ignore",
        action="append",
        metavar="NODEID",
        help="expected to differ (current time, counters, serial numbers); repeat for several",
    )
    comparison.add_argument(
        "--float-tolerance",
        type=float,
        default=0.0,
        metavar="X",
        help="absolute tolerance for compared numbers (default: 0 = exact)",
    )
    comparison.add_argument(
        "--compare-reference-names",
        action="store_true",
        help="also compare the reference graph (reference type names, sources, targets)",
    )
    comparison.add_argument(
        "--no-browse-path-match",
        action="store_true",
        help="do not match nodes by browse path when their NodeId changed (identifier-only matching)",
    )
    comparison.add_argument(
        "--max-differences",
        type=int,
        default=DEFAULT_MAX_COMPARISON_DIFFERENCES,
        metavar="N",
        help=f"rows kept per difference bucket, counts stay complete (default: {DEFAULT_MAX_COMPARISON_DIFFERENCES})",
    )

    connection = parser.add_argument_group("security and identity (shared by both sessions)")
    connection.add_argument("--policy", metavar="POLICY", help="security policy name or URI (default: None)")
    connection.add_argument(
        "--candidate-policy",
        metavar="POLICY",
        help="override the policy for the candidate only (when the devices advertise different endpoints)",
    )
    connection.add_argument(
        "--mode",
        choices=("None", "Sign", "SignAndEncrypt"),
        default="None",
        help="message security mode",
    )
    connection.add_argument(
        "--candidate-mode",
        choices=("None", "Sign", "SignAndEncrypt"),
        help="override the message security mode for the candidate only",
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
        "--reference-server-certificate",
        metavar="FILE",
        help="approved certificate of the reference server (.der/.pem); must match its advertised certificate",
    )
    connection.add_argument(
        "--candidate-server-certificate",
        metavar="FILE",
        help="approved certificate of the candidate server (.der/.pem); must match its advertised certificate",
    )
    connection.add_argument(
        "--trust-server-certificate",
        action="store_true",
        help="approve the advertised certificate of both endpoints and remember it in the trust store",
    )
    connection.add_argument("--trust-store", metavar="FILE", help="trust store file instead of the default")
    connection.add_argument("--timeout", type=float, default=10.0, help="request timeout in seconds")
    connection.add_argument("--connect-timeout", type=float, default=10.0, help="connect timeout in seconds")

    output = parser.add_argument_group("output")
    output.add_argument("--json", metavar="FILE", help="write the comparison as a JSON document")
    output.add_argument("--report", metavar="FILE", help="write the comparison as a self-contained HTML report")
    output.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    output.add_argument("--verbose", "-v", action="store_true", help="debug logging on stderr")
    return parser


def comparison_config_from_args(args: argparse.Namespace) -> ComparisonConfig:
    """Map parsed arguments onto the shared comparison configuration."""
    return ComparisonConfig(
        tolerance=args.tolerance,
        match_by_browse_path=not args.no_browse_path_match,
        compare_references=bool(args.compare_reference_names),
        value_node_ids=frozenset(args.compare_value or ()),
        compare_value_payload=bool(args.compare_value),
        float_tolerance=float(args.float_tolerance),
        ignore_node_ids=frozenset(args.ignore or ()),
        max_differences=int(args.max_differences),
    ).normalised()


def _side_args(args: argparse.Namespace, *, endpoint: str, server_certificate: str | None) -> argparse.Namespace:
    """View of the parsed arguments for one side (used by the shared helpers)."""
    values = vars(args).copy()
    values["endpoint"] = endpoint
    values["server_certificate"] = server_certificate
    return argparse.Namespace(**values)


def candidate_settings(reference_settings: ConnectionSettings, args: argparse.Namespace) -> ConnectionSettings:
    """Settings for the candidate: same identity, optionally other security settings."""
    policy_uri = _policy_uri(args.candidate_policy) if args.candidate_policy else reference_settings.security_policy_uri
    settings = dataclasses.replace(
        reference_settings,
        endpoint_url=args.candidate,
        security_policy_uri=policy_uri,
        security_mode=args.candidate_mode or reference_settings.security_mode,
        server_certificate_der=None,
    )
    problems = settings.validate(require_server_certificate=False)
    if problems:
        raise CliError(" ".join(problems))
    return settings


def _report_limits(snapshot: AddressSpace, role: str, progress: ProgressPrinter) -> None:
    if snapshot.limits_hit:
        progress.note(
            f"the {role} download stopped by limits ({', '.join(snapshot.limits_hit)}); "
            "the comparison will report the gap as a warning"
        )


def source_of(role: str, endpoint: str | None, document: str | None) -> tuple[str | None, str | None]:
    """Resolve one side: exactly one of a live endpoint or a saved JSON document."""
    if endpoint and document:
        raise CliError(f"--{role} and --{role}-file are mutually exclusive.")
    if not endpoint and not document:
        raise CliError(f"Give --{role} ENDPOINT or --{role}-file FILE.")
    return (endpoint, None) if endpoint else (None, document)


def _check_file_sides(
    args: argparse.Namespace,
    reference_document: str | None,
    candidate_document: str | None,
) -> None:
    """Flags that only make sense for a live session are refused for a document."""
    if reference_document and args.reference_server_certificate:
        raise CliError("--reference-server-certificate needs a live --reference; a saved document needs none.")
    if candidate_document and args.candidate_server_certificate:
        raise CliError("--candidate-server-certificate needs a live --candidate; a saved document needs none.")
    if reference_document and args.policy and not args.candidate:
        raise CliError("--policy is only used for live sessions; both sides are saved documents.")


async def _side_snapshot(
    service: OpcUaService | None,
    document: str | None,
    roots: list[str],
    traversal: TraversalConfig,
    progress: ProgressPrinter,
    role: str,
) -> AddressSpace:
    if document is not None:
        progress.note(f"[{role}] reading the saved snapshot {document}")
        snapshot = load_json_snapshot(document)
        progress.note(
            f"[{role}] {len(snapshot.nodes)} node(s), {len(snapshot.references)} reference(s) from the document"
        )
        return snapshot
    if service is None:  # pragma: no cover - guarded by source_of()
        raise CliError(f"The {role} side has neither an endpoint nor a document.")
    snapshot = await service.download(roots, traversal, progress=_SidePrinter(progress, role))
    _report_limits(snapshot, role, progress)
    return snapshot


async def run_compare(args: argparse.Namespace) -> int:
    roots = list(args.node) if args.node else [DEFAULT_ROOT]
    traversal = traversal_config_from_args(args)
    if args.compare_value and not traversal.include_values:
        # Comparing a payload requires reading it; the download flag is implied.
        traversal = dataclasses.replace(traversal, include_values=True)
    config = comparison_config_from_args(args)

    reference_endpoint, reference_document = source_of("reference", args.reference, args.reference_file)
    candidate_endpoint, candidate_document = source_of("candidate", args.candidate, args.candidate_file)
    _check_file_sides(args, reference_document, candidate_document)

    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)

    reference_settings: ConnectionSettings | None = None
    candidate: ConnectionSettings | None = None
    if reference_endpoint or candidate_endpoint:
        # Read credentials once and reuse them for both sessions.
        anchor_endpoint = reference_endpoint or candidate_endpoint
        anchor_certificate = (
            args.reference_server_certificate if reference_endpoint else args.candidate_server_certificate
        )
        anchor = settings_from_args(
            _side_args(args, endpoint=anchor_endpoint, server_certificate=anchor_certificate)
        )
        reference_settings = anchor if reference_endpoint else None
        candidate = candidate_settings(anchor, args) if candidate_endpoint else None

    reference_args = _side_args(
        args, endpoint=reference_endpoint or "", server_certificate=args.reference_server_certificate
    )
    candidate_args = _side_args(
        args, endpoint=candidate_endpoint or "", server_certificate=args.candidate_server_certificate
    )

    reference_service = OpcUaService(trust_store=trust_store)
    candidate_service = OpcUaService(trust_store=trust_store)
    try:
        if reference_settings is not None and reference_settings.is_secure:
            await approve_server_certificate(
                reference_service, reference_settings, reference_args, trust_store, progress
            )
        if candidate is not None and candidate.is_secure:
            await approve_server_certificate(candidate_service, candidate, candidate_args, trust_store, progress)

        if reference_settings is not None:
            await reference_service.connect(reference_settings)
        if candidate is not None:
            await candidate_service.connect(candidate)

        progress.note(f"reference: {reference_endpoint or 'saved document ' + str(reference_document)}")
        progress.note(f"candidate: {candidate_endpoint or 'saved document ' + str(candidate_document)}")
        if reference_settings is not None and candidate is not None:
            progress.note(f"one {reference_settings.auth_mode} identity for both sessions")

        reference_snapshot = await _side_snapshot(
            reference_service if reference_settings is not None else None,
            reference_document,
            roots,
            traversal,
            progress,
            "reference",
        )
        candidate_snapshot = await _side_snapshot(
            candidate_service if candidate is not None else None,
            candidate_document,
            roots,
            traversal,
            progress,
            "candidate",
        )
    finally:
        await reference_service.shutdown()
        await candidate_service.shutdown()

    result = compare_address_spaces(reference_snapshot, candidate_snapshot, config)
    print(result.summary_text())

    if args.json:
        report = write_comparison_json(result, args.json)
        print(f"JSON report:    {report.path}")
    if args.report:
        report = write_comparison_html(result, args.report)
        print(f"HTML report:    {report.path}")

    if result.has_differences:
        progress.note(f"{result.difference_count} difference(s) found; see the summary above.")
        return EXIT_DIFFERENCES
    progress.note("No difference found in the compared scope.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns a process exit code, never raises for expected errors."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)
    try:
        return asyncio.run(run_compare(args))
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
