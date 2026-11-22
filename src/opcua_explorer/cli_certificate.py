"""Headless client-certificate creation: the ``opcua-certificate`` command.

The Connection page's *Create certificate…* without a window: one self-signed
client certificate or one certificate signing request, written to a folder. It is
a **local** operation — no endpoint, no session, no trust decision, nothing sent
to a server — so it needs no ``--confirm``: the operator names the target folder
and the files are never replaced unless ``--overwrite`` says so.

Exit codes: ``0`` the files were written, ``1`` a file could not be written,
``2`` unusable arguments, ``3`` the requested files already exist.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .certificate_creation import CertificateCreationError, create_certificate
from .cli import (
    EXIT_FAILED,
    EXIT_INVALID_OUTPUT,
    EXIT_OK,
    EXIT_USAGE,
    ProgressPrinter,
    _configure_logging,
    add_output_arguments,
)
from .defaults import (
    CERTIFICATE_KEY_SIZES,
    DEFAULT_CERTIFICATE_VALIDITY_DAYS,
    MAX_CERTIFICATE_VALIDITY_DAYS,
)
from .models import CERTIFICATE_MODES, CertificateSpec

FORMAT = "opcua-certificate"
FORMAT_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opcua-certificate",
        description=(
            "Create a client certificate (self-signed) or a certificate signing request for one "
            "application URI, plus its private key. Local only: nothing is installed or trusted."
        ),
        epilog=(
            "Examples:\n"
            "  opcua-certificate --application-uri urn:example:opcua:client --common-name "
            "line1-client --output-dir ~/certs\n"
            "  opcua-certificate --mode csr --application-uri urn:example:opcua:client "
            "--common-name line1-client --output-dir ~/certs --json\n"
            "\n"
            "The application URI is written as a subjectAltName URI; asyncua rejects a client "
            "certificate without it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--application-uri",
        required=True,
        metavar="URI",
        help="application URI of this client, e.g. urn:example:opcua:client (subjectAltName URI)",
    )
    parser.add_argument("--common-name", required=True, metavar="CN", help="subject common name")
    parser.add_argument(
        "--mode",
        choices=list(CERTIFICATE_MODES),
        default=CERTIFICATE_MODES[0],
        help="self-signed certificate or a signing request for a CA (default: self-signed)",
    )
    parser.add_argument(
        "--validity-days",
        type=int,
        default=DEFAULT_CERTIFICATE_VALIDITY_DAYS,
        metavar="N",
        help=f"lifetime of a self-signed certificate (1-{MAX_CERTIFICATE_VALIDITY_DAYS})",
    )
    parser.add_argument(
        "--key-size",
        type=int,
        choices=list(CERTIFICATE_KEY_SIZES),
        default=CERTIFICATE_KEY_SIZES[0],
        metavar="BITS",
        help="RSA key size: " + ", ".join(str(size) for size in CERTIFICATE_KEY_SIZES),
    )
    parser.add_argument(
        "--output-dir", required=True, metavar="DIR", help="folder the certificate/CSR and key are written to"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace files that already exist (off: an existing private key is never replaced)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as one JSON document")
    add_output_arguments(parser)
    return parser


def _spec(args: argparse.Namespace) -> CertificateSpec:
    return CertificateSpec(
        mode=str(args.mode),
        common_name=str(args.common_name),
        application_uri=str(args.application_uri),
        validity_days=int(args.validity_days),
        key_size=int(args.key_size),
        output_dir=str(args.output_dir),
        overwrite=bool(args.overwrite),
    ).normalised()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args)
    progress = ProgressPrinter(sys.stderr, quiet=args.quiet)
    try:
        files = create_certificate(_spec(args))
    except CertificateCreationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        # Existing files are a refusal, not a usage mistake the operator has to guess.
        return EXIT_USAGE if "already exist" not in str(exc) else EXIT_INVALID_OUTPUT
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.json:
        print(
            json.dumps(
                {"format": FORMAT, "format_version": FORMAT_VERSION, **files.to_dict()},
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
    else:
        print(files.summary_text(), flush=True)
    progress.note("Nothing was sent to a server; only local files were written.")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
