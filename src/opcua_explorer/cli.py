"""Headless address-space export: the ``opcua-export`` command.

The GUI is not required to produce a NodeSet2 file. This module drives exactly the
same objects the *Export* tab uses — :class:`OpcUaService` (connect, discovery,
download) and :func:`opcua_explorer.opcua.nodeset2.export_nodeset2` — so the CLI
and the GUI cannot drift apart: one dependency collector, one writer, one
validator, one summary text.

Why a separate entry point instead of a flag on ``opcua-explorer``:
the GUI entry point owns a ``QApplication``; a headless export must not need a Qt
platform plugin. ``opcua-export`` is therefore Qt-free and asyncua is only
reached through the service layer, like every other front end.

Security behaviour follows the GUI invariants:

* no silent downgrade — the requested policy/mode must be advertised;
* a server certificate is only used after an explicit decision: an existing
  trust-store entry, ``--trust-server-certificate`` (approval recorded in the
  same trust store the GUI uses) or ``--server-certificate`` with a fingerprint
  that matches the advertised one;
* passwords never appear on the command line — ``--password-env`` names an
  environment variable, otherwise an interactive terminal is prompted with
  ``getpass``.

Exit codes: ``0`` success, ``1`` connection/export failure, ``2`` unusable
arguments or configuration, ``3`` file written but failed its own structural
checks (it may not import).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from . import __version__
from .config import NONE_POLICY_URI, ConnectionSettings, TrustStore, normalize_fingerprint
from .defaults import DEFAULT_BATCH_SIZE, DEFAULT_CONCURRENCY, DEFAULT_MAX_DEPENDENCIES, DEFAULT_MAX_NODES
from .errors import OpcUaExplorerError
from .models import TraversalConfig, TraversalProgress
from .opcua.client_service import OpcUaService
from .opcua.nodeset2 import NodeSet2Config, NodeSet2Result
from .opcua.security import (
    MODE_NAMES,
    all_policies,
    check_client_certificate,
    policy_from_label,
    policy_from_uri,
    summarize_certificate,
)

_logger = logging.getLogger(__name__)


def parse_iso_time(text: str | None, name: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; a bare value is taken as UTC.

    Shared by the history and event front ends, so ``--from``/``--to`` mean the
    same thing in both and neither accepts a local-time guess.
    """
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

#: Objects folder: the useful default root (Root itself is mostly the type system).
DEFAULT_ROOT = "i=85"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INVALID_OUTPUT = 3

PASSWORD_ENV = "OPCUA_EXPLORER_PASSWORD"
KEY_PASSWORD_ENV = "OPCUA_EXPLORER_KEY_PASSWORD"


class CliError(Exception):
    """A problem the operator can fix; printed without a traceback."""


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the shared connection, security and identity flags."""
    policies = ", ".join(sorted({info.label for info in all_policies()}))
    connection = parser.add_argument_group("security and identity")
    connection.add_argument(
        "--policy",
        metavar="POLICY",
        help=f"security policy name or URI (default: None). Names: {policies}",
    )
    connection.add_argument("--mode", choices=MODE_NAMES, default="None", help="message security mode")
    connection.add_argument("--client-certificate", metavar="FILE", help="client certificate (.der or .pem)")
    connection.add_argument("--client-key", metavar="FILE", help="client private key (.pem)")
    connection.add_argument(
        "--key-password-env",
        metavar="VAR",
        help=f"environment variable holding the private-key password (default for the GUI: {KEY_PASSWORD_ENV})",
    )
    connection.add_argument(
        "--certificate-identity",
        action="store_true",
        help="authenticate with the client certificate instead of anonymously",
    )
    connection.add_argument("--user", metavar="NAME", help="user name for user-name authentication")
    connection.add_argument(
        "--password-env",
        metavar="VAR",
        help=f"environment variable holding the password (default: {PASSWORD_ENV}, else interactive prompt)",
    )
    connection.add_argument(
        "--server-certificate",
        metavar="FILE",
        help="approved server certificate (.der/.pem); its SHA-256 must match the advertised certificate",
    )
    connection.add_argument(
        "--trust-server-certificate",
        action="store_true",
        help="approve the server certificate advertised by this endpoint and remember it in the trust store",
    )
    connection.add_argument(
        "--trust-store",
        metavar="FILE",
        help="trust store file to use instead of the application default",
    )
    connection.add_argument("--timeout", type=float, default=10.0, help="request timeout in seconds")
    connection.add_argument("--connect-timeout", type=float, default=10.0, help="connect timeout in seconds")


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the shared quiet/verbose flags."""
    output = parser.add_argument_group("output")
    output.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    output.add_argument("--verbose", "-v", action="store_true", help="debug logging on stderr")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-export",
        description=(
            "Connect to an OPC UA server, download (part of) its address space and write a "
            "NodeSet2 XML file that modelling tools such as Siemens SiOME or Unified Automation "
            "UaModeler can import."
        ),
        epilog=(
            "Examples:\n"
            "  opcua-export --endpoint opc.tcp://192.168.1.100:4840 --output PSU8600.NodeSet2.xml\n"
            "  opcua-export export --endpoint opc.tcp://plc:4840 --node ns=2;s=MyDevice --manifest out.json\n"
            "  opcua-export --endpoint opc.tcp://plc:4840 --namespace 2 --policy Basic256Sha256 "
            "--mode SignAndEncrypt --client-certificate client.der --client-key client.pem\n"
            "\n"
            "Passwords are read from --password-env or prompted on a terminal; they are never "
            "accepted as command-line arguments. See README.md (section 'Headless export')."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("export",),
        default="export",
        help="command to run (default: export; accepted for symmetry with other tools)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("target")
    target.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    target.add_argument(
        "--output",
        help="NodeSet2 XML file to write (default: <host>-<port>.NodeSet2.xml in the current directory)",
    )
    target.add_argument(
        "--manifest",
        help="optional JSON file with the export result (counts, namespaces, missing dependencies, validation)",
    )

    scope = parser.add_argument_group("export scope")
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
    scope.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_MAX_NODES,
        metavar="N",
        help=f"node budget for the download, 0 = unlimited (default: {DEFAULT_MAX_NODES})",
    )
    scope.add_argument("--max-depth", type=int, default=0, metavar="N", help="download depth, 0 = unlimited")
    scope.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="concurrent Browse requests")
    scope.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="nodes per Browse request")
    scope.add_argument(
        "--values",
        action="store_true",
        help="also read variable values (off by default; makes the file larger and reads live process data)",
    )
    scope.add_argument(
        "--device-attributes",
        action="store_true",
        help=(
            "also read the attributes that are not part of the data model (write masks, historizing, "
            "sampling interval, type flags, access restrictions, role permissions) so opcua-compare "
            "--tolerance device can compare them"
        ),
    )

    nodeset = parser.add_argument_group("NodeSet2 output")
    nodeset.add_argument(
        "--no-dependencies",
        action="store_true",
        help="do not collect parents/type definitions (the file will likely not import)",
    )
    nodeset.add_argument(
        "--max-dependencies",
        type=int,
        default=DEFAULT_MAX_DEPENDENCIES,
        metavar="N",
        help=f"dependency node budget, 0 = none (default: {DEFAULT_MAX_DEPENDENCIES})",
    )
    nodeset.add_argument("--schema", metavar="XSD", help="NodeSet2 XSD file for optional schema validation")
    nodeset.add_argument("--no-pretty", action="store_true", help="write compact XML instead of indented XML")

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _policy_uri(value: str | None) -> str:
    """Map a policy label or URI to the canonical URI; unknown names are refused."""
    if not value:
        return NONE_POLICY_URI
    info = policy_from_label(value) or policy_from_uri(value)
    if info is None:
        known = ", ".join(info.label for info in all_policies())
        raise CliError(f"Unknown security policy {value!r}. Known policies: {known}.")
    return info.uri


def _read_secret(env_name: str | None, default_env: str, prompt: str) -> str:
    """Read a password from an environment variable or an interactive terminal.

    Command-line arguments are deliberately not supported: process argv is visible
    to other users on the machine.
    """
    name = env_name or default_env
    value = os.environ.get(name)
    if value:
        return value
    if env_name:
        raise CliError(f"Environment variable {name} is not set (or empty).")
    if sys.stdin.isatty():
        secret = getpass.getpass(prompt)
        if secret:
            return secret
        raise CliError("No password entered.")
    raise CliError(f"No password available: set {name} or run the command in an interactive terminal.")


def settings_from_args(args: argparse.Namespace) -> ConnectionSettings:
    """Build the connection settings; validated, but without the certificate yet."""
    policy_uri = _policy_uri(args.policy)
    settings = ConnectionSettings(
        endpoint_url=args.endpoint,
        security_policy_uri=policy_uri,
        security_mode=args.mode,
        connect_timeout=float(args.connect_timeout),
        request_timeout=float(args.timeout),
        # A one-shot export must not silently retry a dropped link into the void.
        auto_reconnect=False,
    )
    if args.client_certificate or args.client_key:
        if not (args.client_certificate and args.client_key):
            raise CliError("--client-certificate and --client-key must be given together.")
        settings.client_certificate_path = args.client_certificate
        settings.client_private_key_path = args.client_key
    if args.key_password_env:
        settings.private_key_password = _read_secret(args.key_password_env, KEY_PASSWORD_ENV, "Private key password: ")
    if args.user and args.certificate_identity:
        raise CliError("--user and --certificate-identity are mutually exclusive.")
    if args.user:
        settings.auth_mode = "username"
        settings.username = args.user
        settings.password = _read_secret(args.password_env, PASSWORD_ENV, f"Password for {args.user}: ")
    elif args.certificate_identity:
        settings.auth_mode = "certificate"
        settings.use_certificate_identity = True

    problems = settings.validate(require_server_certificate=False)
    if problems:
        raise CliError(" ".join(problems))
    return settings


def traversal_config_from_args(args: argparse.Namespace) -> TraversalConfig:
    return TraversalConfig(
        max_depth=None if args.max_depth <= 0 else int(args.max_depth),
        max_nodes=None if args.max_nodes <= 0 else int(args.max_nodes),
        concurrency=int(args.concurrency),
        batch_size=int(args.batch_size),
        namespace_filter=frozenset(args.namespace) if args.namespace else None,
        include_values=bool(args.values),
        include_device_attributes=bool(getattr(args, "device_attributes", False)),
    ).normalised()


def nodeset_config_from_args(args: argparse.Namespace) -> NodeSet2Config:
    return NodeSet2Config(
        include_values=bool(args.values),
        include_dependencies=not args.no_dependencies,
        max_dependency_nodes=int(args.max_dependencies),
        pretty=not args.no_pretty,
        schema_path=(args.schema or "").strip() or None,
    ).normalised()


def default_output_path(endpoint: str) -> Path:
    """``opc.tcp://host:port`` -> ``host-port.NodeSet2.xml`` in the working directory."""
    url = endpoint.strip()
    parts = urlsplit(url if "://" in url else "opc.tcp://" + url)
    host = (parts.hostname or "server").replace(":", "-")
    return Path(f"{host}-{parts.port or 4840}.NodeSet2.xml")


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


class ProgressPrinter:
    """Throttled ``phase``/count progress on stderr.

    Progress is diagnostic output, never the result: it goes to stderr so that
    stdout stays parseable.
    """

    STEP = 500

    def __init__(self, stream: TextIO, *, quiet: bool = False) -> None:
        self._stream = stream
        self._quiet = quiet
        self._phase = ""
        self._last_count = 0

    def __call__(self, update: TraversalProgress) -> None:
        if self._quiet:
            return
        count = int(update.nodes_collected or 0)
        phase_changed = update.phase != self._phase
        if phase_changed:
            self._phase = update.phase
            self._print(f"[{update.phase}] {update.message or 'working'}")
            self._last_count = count
            return
        if count - self._last_count >= self.STEP:
            self._last_count = count
            self._print(f"[{update.phase}] {count} node(s)")

    def note(self, message: str) -> None:
        """Print a one-off line (used when the same phase reports a new step)."""
        if not self._quiet:
            self._print(message)

    def _print(self, message: str) -> None:
        print(message, file=self._stream, flush=True)


# ---------------------------------------------------------------------------
# Certificate approval
# ---------------------------------------------------------------------------


def _read_certificate_der(path: Path) -> bytes:
    """Read a PEM or DER certificate file and return the DER bytes."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CliError(f"{path} could not be read: {exc}") from exc
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
    except ImportError as exc:  # pragma: no cover - cryptography ships with asyncua
        raise CliError("Reading certificate files requires the 'cryptography' package.") from exc
    try:
        certificate = (
            x509.load_pem_x509_certificate(data)
            if b"-----BEGIN CERTIFICATE-----" in data
            else x509.load_der_x509_certificate(data)
        )
    except Exception as exc:
        raise CliError(f"{path} is not a readable X.509 certificate: {exc}") from exc
    return certificate.public_bytes(Encoding.DER)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _note_client_certificate(settings: ConnectionSettings, progress: ProgressPrinter) -> None:
    """Report local problems with ``--client-certificate``/``--client-key`` before connecting.

    A warning, not a refusal: the operator may know that the server accepts the
    certificate anyway, and the server's own StatusCode is the verdict.
    """
    if not settings.client_certificate_path:
        return
    check = check_client_certificate(
        settings.client_certificate_path,
        settings.client_private_key_path,
        password=settings.private_key_password or "",
    )
    for problem in check.problems:
        progress.note(f"client certificate: {problem}")
    for warning in check.warnings:
        progress.note(f"client certificate: {warning}")


async def approve_server_certificate(
    service: OpcUaService,
    settings: ConnectionSettings,
    args: argparse.Namespace,
    trust_store: TrustStore,
    progress: ProgressPrinter,
) -> None:
    """Decide which DER certificate may be used, or refuse to connect.

    The decision is never implicit: the certificate must already be in the trust
    store, be approved for this run with ``--trust-server-certificate``, or be
    supplied with ``--server-certificate`` and match what the server advertises.
    """
    _note_client_certificate(settings, progress)
    discovery = await service.discover(
        settings.endpoint_url,
        request_timeout=settings.request_timeout,
        connect_timeout=settings.connect_timeout,
    )
    endpoint = discovery.find(settings.security_policy_uri, settings.security_mode)
    if endpoint is None:
        advertised = ", ".join(discovery.advertised_combinations()) or "none"
        raise CliError(
            f"{settings.security_policy_uri} / {settings.security_mode} is not advertised by the server. "
            f"Advertised: {advertised}."
        )

    if args.server_certificate:
        der = _read_certificate_der(Path(args.server_certificate))
        approved = _sha256_hex(der)
        if endpoint.certificate_sha256 and normalize_fingerprint(endpoint.certificate_sha256) != approved:
            raise CliError(
                f"The certificate in {args.server_certificate} does not match the certificate the server "
                f"advertises (file {approved[:16]}..., server {normalize_fingerprint(endpoint.certificate_sha256)[:16]}...)."
            )
        settings.server_certificate_der = der
        return

    der = endpoint.certificate_der
    if not der:
        raise CliError("The endpoint did not provide a server certificate, so no secure session is possible.")
    summary = summarize_certificate(der)

    if not trust_store.is_trusted(summary.sha256):
        if not args.trust_server_certificate:
            raise CliError(
                "The server certificate is not approved. Verify this fingerprint out-of-band, then re-run "
                "with --trust-server-certificate (or pass the approved file with --server-certificate):\n"
                f"  subject: {summary.subject}\n"
                f"  sha256 : {summary.fingerprint_colon}"
            )
        trust_store.add(
            summary.sha256,
            subject=summary.subject,
            endpoint_url=settings.endpoint_url,
            not_after=summary.not_after,
        )
        progress.note(f"Approved server certificate {summary.subject or 'unknown subject'} ({summary.fingerprint_colon})")
    settings.server_certificate_der = der


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def run_export(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    traversal = traversal_config_from_args(args)
    nodeset_config = nodeset_config_from_args(args)
    target = Path(args.output) if args.output else default_output_path(args.endpoint)
    roots = list(args.node) if args.node else [DEFAULT_ROOT]

    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)

    try:
        if settings.is_secure:
            await approve_server_certificate(service, settings, args, trust_store, progress)

        await service.connect(settings)
        progress.note(f"Connected to {service.endpoint_url} (session {settings.auth_mode}, policy {args.policy or 'None'})")

        snapshot = await service.download(roots, traversal, progress=progress)
        if snapshot.limits_hit:
            progress.note(f"Download stopped by limits: {', '.join(snapshot.limits_hit)}")

        result = await service.export_nodeset2(target, snapshot, nodeset_config, progress=progress)
    finally:
        await service.shutdown()

    _print_result(result, args, progress)
    return EXIT_INVALID_OUTPUT if result.validation.structural_errors else EXIT_OK


def _print_result(result: NodeSet2Result, args: argparse.Namespace, progress: ProgressPrinter) -> None:
    print(result.summary_text())
    for entry in result.namespaces[1:]:
        print(f"  ns={entry['index']}  {entry['uri']}")

    if args.manifest:
        manifest_path = Path(args.manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"Manifest:       {manifest_path}")

    if result.validation.structural_errors:
        progress.note(
            "The file failed its own structural checks and may not import; see the errors above."
        )


def _configure_logging(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # asyncua is extremely chatty while opening a channel.
    logging.getLogger("asyncua").setLevel(logging.DEBUG if args.verbose else logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns a process exit code, never raises for expected errors."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)
    try:
        return asyncio.run(run_export(args))
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
