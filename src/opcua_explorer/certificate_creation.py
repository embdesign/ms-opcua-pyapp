"""Create the client certificate this application needs (pure, no Qt, no asyncua).

A first-time user of a secured endpoint usually has no certificate and no CA at
hand, and asyncua rejects a client certificate whose subjectAltName does not carry
the application URI. This module writes exactly the files the Connection tab asks
for:

* a **self-signed** client certificate (PEM) with the application URI as a
  subjectAltName URI, `clientAuth` extended key usage and no CA flag, together
  with its private key, or
* a **PKCS#10 certificate signing request** (PEM) plus the same kind of key, for a
  CA or GDS that issues the certificate.

The private key is written unencrypted as PKCS#8 PEM with file mode ``0600`` and
is reported as such: the caller shows that consequence before anything is written.
Nothing here connects, signs on behalf of anybody else, installs a certificate on
a server or touches the trust store, and an existing file is never replaced unless
the caller asked for it.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .models import CertificateFiles, CertificateSpec, iso_utc

#: Suffixes of the files one run writes; the stem comes from the common name.
CERTIFICATE_SUFFIX = "-client-cert.pem"
REQUEST_SUFFIX = "-client.csr.pem"
KEY_SUFFIX = "-client-key.pem"
#: Mode of the written private key: owner read/write only.
KEY_FILE_MODE = 0o600
#: Clock-skew allowance: a freshly written certificate must be usable at once.
_NOT_BEFORE_SKEW = timedelta(days=1)


class CertificateCreationError(ValueError):
    """The request cannot be honoured as asked; nothing was written."""


def _safe_stem(common_name: str) -> str:
    """A file-name stem derived from the common name, never empty."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", common_name).strip("-.")
    return stem or "opcua-client"


def _require_uri(application_uri: str) -> str:
    uri = application_uri.strip()
    if not uri:
        raise CertificateCreationError(
            "An application URI is required: a client certificate without it as subjectAltName URI is "
            "rejected by asyncua before connecting."
        )
    if any(char.isspace() for char in uri) or ":" not in uri:
        raise CertificateCreationError(
            f"{uri!r} is not a usable application URI; expected a URN or URL such as "
            "urn:example:opcua:client."
        )
    return uri


def planned_paths(spec: CertificateSpec) -> tuple[Path, Path]:
    """``(main_file, key_file)`` this spec would write.

    The main file is the certificate for a self-signed run and the signing request
    for a CSR run. Used for the confirmation text and the overwrite check, so what
    the dialog promises is exactly what the generator writes.
    """
    normalised = spec.normalised()
    if not normalised.common_name:
        raise CertificateCreationError("A common name (CN) is required.")
    _require_uri(normalised.application_uri)
    if not normalised.output_dir:
        raise CertificateCreationError("Choose the folder the certificate and key are written to.")
    folder = Path(normalised.output_dir).expanduser()
    stem = _safe_stem(normalised.common_name)
    suffix = REQUEST_SUFFIX if normalised.is_request else CERTIFICATE_SUFFIX
    return folder / f"{stem}{suffix}", folder / f"{stem}{KEY_SUFFIX}"


def create_certificate(spec: CertificateSpec, *, now: datetime | None = None) -> CertificateFiles:
    """Write one client certificate (or signing request) and its private key.

    ``now`` exists for tests only; a real run uses the wall clock. A file that
    already exists aborts the whole request before anything is written unless
    ``spec.overwrite`` is set, so a wrong folder can never destroy an existing key.
    """
    normalised = spec.normalised()
    target, key_path = planned_paths(normalised)
    targets = [target, key_path]
    existing = [str(path) for path in targets if path.exists()]
    if existing and not normalised.overwrite:
        raise CertificateCreationError(
            "These files already exist and would be replaced: "
            + ", ".join(existing)
            + ". Choose another common name or folder, or allow replacing them explicitly."
        )

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, normalised.common_name)])
    san = x509.SubjectAlternativeName(
        [x509.UniformResourceIdentifier(normalised.application_uri)]
    )
    key = rsa.generate_private_key(
        public_exponent=65537, key_size=normalised.key_size
    )

    folder = key_path.parent
    created_folder = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    if created_folder:
        # Windows has no POSIX modes; the key itself is protected by its own mode.
        with suppress(OSError):
            os.chmod(folder, 0o700)

    written: list[Path] = []
    if normalised.is_request:
        builder = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(subject)
            .add_extension(san, critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
            )
        )
        request = builder.sign(key, hashes.SHA256())
        target.write_bytes(request.public_bytes(serialization.Encoding.PEM))
        written.append(target)
        fingerprint = hashlib.sha256(
            request.public_bytes(serialization.Encoding.DER)
        ).hexdigest()
        not_before = not_after = None
    else:
        not_before_dt = moment - _NOT_BEFORE_SKEW
        not_after_dt = moment + timedelta(days=normalised.validity_days)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before_dt)
            .not_valid_after(not_after_dt)
            .add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=True,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        target.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        written.append(target)
        fingerprint = hashlib.sha256(
            certificate.public_bytes(serialization.Encoding.DER)
        ).hexdigest()
        not_before = iso_utc(not_before_dt)
        not_after = iso_utc(not_after_dt)

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with suppress(OSError):  # pragma: no cover - Windows has no POSIX modes
        os.chmod(key_path, KEY_FILE_MODE)
    written.append(key_path)

    return CertificateFiles(
        mode=normalised.mode,
        common_name=normalised.common_name,
        subject=subject.rfc4514_string(),
        application_uri=normalised.application_uri,
        key_size=normalised.key_size,
        certificate_path=None if normalised.is_request else str(target),
        request_path=str(target) if normalised.is_request else None,
        key_path=str(key_path),
        fingerprint_sha256=fingerprint,
        not_before=not_before,
        not_after=not_after,
        written=[str(path) for path in written],
    )


__all__ = [
    "CERTIFICATE_SUFFIX",
    "KEY_FILE_MODE",
    "KEY_SUFFIX",
    "REQUEST_SUFFIX",
    "CertificateCreationError",
    "create_certificate",
    "planned_paths",
]
