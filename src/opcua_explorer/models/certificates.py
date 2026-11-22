"""Client-certificate creation: the plain request and what was written.

A secured endpoint needs a client certificate whose subjectAltName carries this
application's URI, and a first-time user usually has neither a certificate nor a
CA at hand. These dataclasses hold the plain data of that one *local* operation —
what to create and what was created — so the GUI, the CLI and their tests agree
without importing Qt, asyncua or the cryptography package.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..defaults import (
    CERTIFICATE_KEY_SIZES,
    DEFAULT_CERTIFICATE_VALIDITY_DAYS,
    MAX_CERTIFICATE_VALIDITY_DAYS,
    MIN_CERTIFICATE_KEY_SIZE,
)

#: The two things this client can create on this computer.
CERTIFICATE_MODES = ("self-signed", "csr")


@dataclass(slots=True)
class CertificateSpec:
    """What to create: one self-signed client certificate or one signing request.

    The application URI is not decoration: asyncua refuses a client certificate
    whose subjectAltName does not contain it, so the generator always writes it.
    """

    mode: str = "self-signed"
    common_name: str = ""
    application_uri: str = ""
    validity_days: int = DEFAULT_CERTIFICATE_VALIDITY_DAYS
    key_size: int = MIN_CERTIFICATE_KEY_SIZE
    output_dir: str = ""
    #: Replace files that already exist. Off by default: an existing private key
    #: must never be overwritten silently.
    overwrite: bool = False

    def normalised(self) -> CertificateSpec:
        """Clamp the range fields; the values left are what the UI promised."""
        key_size = min(max(int(self.key_size), MIN_CERTIFICATE_KEY_SIZE), CERTIFICATE_KEY_SIZES[-1])
        return replace(
            self,
            mode=self.mode if self.mode in CERTIFICATE_MODES else CERTIFICATE_MODES[0],
            common_name=" ".join(str(self.common_name).split()),
            application_uri=str(self.application_uri or "").strip(),
            validity_days=min(MAX_CERTIFICATE_VALIDITY_DAYS, max(1, int(self.validity_days))),
            key_size=key_size,
            output_dir=str(self.output_dir or "").strip(),
            overwrite=bool(self.overwrite),
        )

    @property
    def is_request(self) -> bool:
        return self.mode == "csr"

    def to_dict(self) -> dict[str, Any]:
        normalised = self.normalised()
        return {
            "mode": normalised.mode,
            "common_name": normalised.common_name,
            "application_uri": normalised.application_uri,
            "validity_days": normalised.validity_days,
            "key_size": normalised.key_size,
            "output_dir": normalised.output_dir,
            "overwrite": normalised.overwrite,
        }


@dataclass(slots=True)
class CertificateFiles:
    """What one creation wrote: the paths, the subject, the fingerprint and validity.

    ``fingerprint_sha256`` is the SHA-256 of the DER certificate for a self-signed
    run and of the DER request for a CSR, because that is the value an
    administrator can compare. Validity is only known for a self-signed run: a CSR
    has no lifetime until a CA issues from it.
    """

    mode: str = CERTIFICATE_MODES[0]
    common_name: str = ""
    subject: str = ""
    application_uri: str = ""
    key_size: int = MIN_CERTIFICATE_KEY_SIZE
    certificate_path: str | None = None
    request_path: str | None = None
    key_path: str = ""
    fingerprint_sha256: str = ""
    not_before: str | None = None
    not_after: str | None = None
    written: list[str] = field(default_factory=list)

    @property
    def is_request(self) -> bool:
        return self.mode == "csr"

    def summary_text(self) -> str:
        """One operator-facing paragraph: what exists now and what it is worth."""
        if self.is_request:
            head = (
                f"Certificate request for CN={self.common_name} written to {self.request_path}; "
                f"private key {self.key_path} (unencrypted, file mode 600)"
            )
            tail = (
                f"SHA-256 of the request: {self.fingerprint_sha256}. Send the request to the CA that issues "
                "certificates for this application; this client cannot issue one and nothing was installed."
            )
        else:
            head = (
                f"Self-signed client certificate for CN={self.common_name} written to "
                f"{self.certificate_path}; private key {self.key_path} (unencrypted, file mode 600)"
            )
            tail = (
                f"Valid {self.not_before} to {self.not_after}. SHA-256: {self.fingerprint_sha256}. "
                "Only the servers whose administrators trust this certificate will accept it; nothing was "
                "installed on a server and nothing was trusted."
            )
        return f"{head}. {tail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "common_name": self.common_name,
            "subject": self.subject,
            "application_uri": self.application_uri,
            "key_size": self.key_size,
            "certificate_path": self.certificate_path,
            "request_path": self.request_path,
            "key_path": self.key_path,
            "fingerprint_sha256": self.fingerprint_sha256,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "written": list(self.written),
        }


__all__ = ["CERTIFICATE_MODES", "CertificateFiles", "CertificateSpec"]
