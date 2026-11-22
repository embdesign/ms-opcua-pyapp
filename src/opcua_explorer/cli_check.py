"""Headless functional checks: the ``opcua-check`` command.

Runs the same bounded suite as the **Tests** tab through the same
:class:`~opcua_explorer.opcua.client_service.OpcUaService` call
(`run_functional_tests`), so a service engineer and a CI job get the same verdict
and the same report files.

Default is read-only. ``--include-writes --confirm`` adds the guarded checks for
explicitly configured targets; without ``--confirm`` nothing is written and the
command refuses before it connects. The only disruptive read-only check
(connection interruption) stays opt-in through ``--include-recovery``.

Exit codes: ``0`` no check failed, ``1`` connection/session failure, ``2``
unusable arguments/configuration, ``3`` at least one check failed.
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
from .defaults import (
    DEFAULT_LATENCY_SAMPLES,
    DEFAULT_NOTIFICATION_WAIT_SECONDS,
    DEFAULT_PUBLISHING_INTERVAL_MS,
    DEFAULT_SAMPLING_INTERVAL_MS,
)
from .errors import InvalidNodeIdError, OpcUaExplorerError
from .models import STATUS_FAILED, MethodCallTarget, TestConfig, TestProgress, TestRun, WriteTarget
from .opcua.browse import parse_node_id
from .opcua.client_service import OpcUaService
from .opcua.node_ids import normalise_node_id
from .operator_text import guarded_operation_confirmation

# The suite is the Tests tab's, so its JSON output is that report's canonical document.
from .reporting import run_to_document, write_html_report, write_json_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-check",
        description=(
            "Run the bounded functional and interoperability checks against one OPC UA server and "
            "print or write the report."
        ),
        epilog=(
            "Examples:\n"
            "  opcua-check --endpoint opc.tcp://192.168.1.100:4840\n"
            "  opcua-check --endpoint opc.tcp://plc:4840 --variable ns=2;s=Temperature --json\n"
            "  opcua-check --endpoint opc.tcp://plc:4840 --report checks.html --quiet\n"
            "  opcua-check --endpoint opc.tcp://plc:4840 --include-writes --confirm "
            "--write ns=2;s=Temperature=21.5\n"
            "\n"
            "The default checks are read-only. Passwords are read from --password-env or prompted on "
            "a terminal; they are never accepted as command-line arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("target")
    target.add_argument("--endpoint", required=True, help="OPC UA endpoint URL, e.g. opc.tcp://192.168.1.100:4840")
    target.add_argument("--root", default="i=85", metavar="NODEID", help="browse root for the checks (default: i=85)")
    target.add_argument(
        "--variable",
        metavar="NODEID",
        help="variable the value, subscription and latency checks use (default: auto-detected near the root)",
    )

    timing = parser.add_argument_group("timing")
    timing.add_argument(
        "--publishing-interval",
        type=float,
        default=DEFAULT_PUBLISHING_INTERVAL_MS,
        metavar="MS",
        help=f"requested publishing interval in ms (default: {DEFAULT_PUBLISHING_INTERVAL_MS:.0f})",
    )
    timing.add_argument(
        "--sampling-interval",
        type=float,
        default=DEFAULT_SAMPLING_INTERVAL_MS,
        metavar="MS",
        help=f"requested sampling interval in ms (default: {DEFAULT_SAMPLING_INTERVAL_MS:.0f})",
    )
    timing.add_argument(
        "--notification-wait",
        type=float,
        default=DEFAULT_NOTIFICATION_WAIT_SECONDS,
        metavar="SECONDS",
        help=f"how long the subscription check waits for a notification (default: {DEFAULT_NOTIFICATION_WAIT_SECONDS:g})",
    )
    timing.add_argument(
        "--latency-samples",
        type=int,
        default=DEFAULT_LATENCY_SAMPLES,
        metavar="N",
        help=f"read samples for the latency check (default: {DEFAULT_LATENCY_SAMPLES})",
    )
    timing.add_argument(
        "--include-recovery",
        action="store_true",
        help="also run the opt-in check that closes the secure channel and reconnects (interrupts subscriptions)",
    )

    guarded = parser.add_argument_group("guarded checks (opt-in)")
    guarded.add_argument(
        "--include-writes",
        action="store_true",
        help="add the guarded write/method-call checks for the targets given below",
    )
    guarded.add_argument(
        "--write",
        action="append",
        metavar="NODEID=VALUE",
        help="write target for the guarded check; repeatable",
    )
    guarded.add_argument(
        "--restore",
        action="store_true",
        help="restore each write target's previous value afterwards (one further verified write)",
    )
    guarded.add_argument(
        "--call",
        action="append",
        metavar="OBJECT|METHOD|ARGS",
        help="method call for the guarded check, e.g. 'ns=2;s=Motor|ns=2;s=Start|1450'; repeatable",
    )
    guarded.add_argument(
        "--confirm",
        action="store_true",
        help="required with --include-writes: without it nothing is written",
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print the run as one versioned JSON document")
    output.add_argument(
        "--report",
        metavar="PATH",
        help="write a report: .json or .html (the same writers the Tests tab uses)",
    )

    add_connection_arguments(parser)
    add_output_arguments(parser)
    return parser


def _write_target(text: str) -> WriteTarget:
    """Parse ``NODEID=VALUE``, splitting at the last ``=`` that leaves a real NodeId.

    A NodeId contains ``=`` itself (``ns=2;s=Temperature``), so a plain ``partition``
    would cut inside it. Splitting from the right and validating the left side keeps
    ``ns=2;s=Recipe=x=y`` (value ``y``) working and refuses a target with no value.
    """
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "=":
            continue
        node_text, value_text = text[:index].strip(), text[index + 1 :].strip()
        if not value_text:
            continue
        try:
            node_id = parse_node_id(node_text)
        except InvalidNodeIdError:
            continue
        if not node_id.Identifier and node_id.Identifier != 0:
            continue
        return WriteTarget(node_id=node_id.to_string(), value_text=value_text)
    raise CliError(f"--write expects NODEID=VALUE, not {text!r}.")


def _method_call(text: str) -> MethodCallTarget:
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise CliError(f"--call expects OBJECT|METHOD|ARGS, not {text!r}.")
    arguments = [argument.strip() for argument in parts[2].split(",")] if len(parts) > 2 and parts[2] else []
    return MethodCallTarget(
        object_node_id=parts[0], method_node_id=parts[1], arguments=[arg for arg in arguments if arg]
    )


def _config_from_args(args: argparse.Namespace) -> TestConfig:
    """Build the suite configuration and refuse an unconfirmed guarded run."""
    writes = [_write_target(text) for text in (args.write or [])]
    calls = [_method_call(text) for text in (args.call or [])]
    if (writes or calls) and not args.include_writes:
        raise CliError("--write and --call only apply with --include-writes.")
    if args.include_writes and not (writes or calls):
        raise CliError("--include-writes needs at least one --write or --call target.")
    if args.include_writes and not args.confirm:
        raise CliError(
            "--include-writes sends real writes; repeat the command with --confirm to allow them. "
            "Nothing was sent."
        )
    if args.restore and not writes:
        raise CliError("--restore only applies to --write targets.")
    if args.confirm and not args.include_writes:
        raise CliError("--confirm only applies with --include-writes; nothing was sent.")

    return TestConfig(
        root_node_id=normalise_node_id(args.root),
        variable_node_id=normalise_node_id(args.variable) if args.variable else None,
        publishing_interval_ms=float(args.publishing_interval),
        sampling_interval_ms=float(args.sampling_interval),
        notification_wait_seconds=float(args.notification_wait),
        latency_samples=int(args.latency_samples),
        include_channel_recovery=bool(args.include_recovery),
        allow_writes=bool(args.include_writes),
        write_targets=[WriteTarget(t.node_id, t.value_text, args.restore) for t in writes],
        method_calls=calls,
    ).normalised()


def _print_run(run: TestRun, *, as_json: bool) -> None:
    if as_json:
        # The canonical run document, so the CLI and the Tests tab's JSON report agree.
        print(json.dumps(run_to_document(run), indent=2, ensure_ascii=False, default=str))
        return
    print(run.summary_text())
    for check in run.checks:
        marker = {"passed": "ok  ", "failed": "FAIL", "skipped": "skip"}.get(check.status, check.status)
        detail = f" — {check.detail}" if check.detail else ""
        print(f"  {marker}  {check.id:<28} {check.name}{detail}")


def _write_report(run: TestRun, path: str) -> None:
    target = path.lower()
    if target.endswith(".json"):
        result = write_json_report(run, path)
    elif target.endswith((".html", ".htm")):
        result = write_html_report(run, path)
    else:
        raise CliError("--report needs a .json or .html path.")
    print(f"report: {result.path} ({result.bytes_written} bytes)", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    settings = settings_from_args(args)
    trust_store = TrustStore(args.trust_store) if args.trust_store else TrustStore()
    service = OpcUaService(trust_store=trust_store)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)

    if config.allow_writes:
        # The operator sees the same equipment warning the GUI confirmation shows before
        # the single confirmed operations run.
        print(
            guarded_operation_confirmation(endpoint_url=args.endpoint, config=config),
            file=sys.stderr,
            flush=True,
        )

    def report_progress(update: TestProgress) -> None:
        prefix = f"{update.current}/{update.total}".strip() if update.total else update.phase
        progress.note(f"{prefix} {update.message}".strip())

    if settings.is_secure:
        await approve_server_certificate(service, settings, args, trust_store, progress)
    await service.connect(settings)
    try:
        run = await service.run_functional_tests(config, progress=report_progress)
    finally:
        await service.shutdown()

    _print_run(run, as_json=args.json)
    if args.report:
        _write_report(run, args.report)
    failed = [check for check in run.checks if check.status == STATUS_FAILED]
    progress.note(f"{len(run.checks)} check(s), {len(failed)} failed")
    return EXIT_INVALID_OUTPUT if failed else EXIT_OK


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
