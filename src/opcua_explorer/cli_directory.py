"""Headless server directory: the ``opcua-directory`` command.

One `FindServers` request against a discovery endpoint, printed as an
:class:`~opcua_explorer.models.DiscoveryResult` table — the same call the
**Connection** page's *Find servers…* dialog makes. It is a lookup only: no
session, no identity, no trust decision (the request uses a temporary
SecurityPolicyNone channel), no multicast discovery, and it never connects to a
found application.

Exit codes: ``0`` the directory answered (with or without entries), ``1`` the
request failed, ``2`` unusable arguments/configuration.
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
    add_output_arguments,
)
from .models import ServerDiscoveryResult
from .opcua.client_service import OpcUaService

FORMAT = "opcua-directory"
FORMAT_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-directory",
        description="Ask an OPC UA discovery endpoint (LDS/GDS or any server) which servers it knows.",
        epilog=(
            "Examples:\n"
            "  opcua-directory --discovery-url opc.tcp://192.168.1.10:4840\n"
            "  opcua-directory --discovery-url opc.tcp://gds:4840 --uri urn:vendor --json\n"
            "\n"
            "A lookup only: no session is opened and no found server is contacted. There is no "
            "multicast/FindServersOnNetwork discovery."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--discovery-url",
        "--endpoint",
        dest="discovery_url",
        required=True,
        help="discovery endpoint URL, e.g. opc.tcp://192.168.1.10:4840",
    )
    parser.add_argument(
        "--uri",
        action="append",
        metavar="APPLICATIONURI",
        help="only applications whose URI starts with this prefix; repeatable",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="request timeout (default: 10)",
    )
    parser.add_argument("--json", action="store_true", help="print a versioned JSON document")
    add_output_arguments(parser)
    return parser


def _print(result: ServerDiscoveryResult, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {"format": FORMAT, "version": FORMAT_VERSION, "directory": result.to_dict()},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return
    print(result.summary_text())
    for server in result.servers:
        print(f"  {server.application_name or '(unnamed)'}  [{server.application_type}]")
        print(f"    application URI: {server.application_uri}")
        if server.product_uri:
            print(f"    product URI:     {server.product_uri}")
        if server.gateway_server_uri:
            print(f"    gateway:         {server.gateway_server_uri}")
        for url in server.discovery_urls:
            print(f"    endpoint:        {url}")


async def _run(args: argparse.Namespace) -> int:
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    service = OpcUaService()
    try:
        result = await service.discover_servers(
            args.discovery_url,
            server_uris=args.uri or (),
            request_timeout=float(args.timeout),
        )
    finally:
        await service.shutdown()

    _print(result, as_json=args.json)
    progress.note(
        f"{len(result.servers)} application(s) in {result.duration_ms:.0f} ms; "
        "nothing was connected, only the directory was asked."
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args)
    try:
        return asyncio.run(_run(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - one clear line for a refused endpoint
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
