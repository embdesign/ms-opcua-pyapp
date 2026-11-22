"""Security-policy mapping, certificate inspection and anti-downgrade checks.

Everything in this module is pure Python (no session required) so it can be unit
tested without a server. The policy URIs and classes were verified against
asyncua 2.1.0 (``asyncua.crypto.security_policies``).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asyncua import ua
from asyncua.crypto import security_policies as sp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from ..errors import EndpointNotAdvertisedError
from ..models import EndpointInfo, iso_utc

_logger = logging.getLogger(__name__)

POLICY_NONE_URI = sp.SecurityPolicyNone.URI


@dataclass(frozen=True, slots=True)
class PolicyInfo:
    """A selectable MessageSecurityPolicy."""

    uri: str
    label: str
    policy_class: type
    secure: bool
    deprecated: bool = False
    note: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.label


_POLICIES: tuple[PolicyInfo, ...] = (
    PolicyInfo(
        uri=sp.SecurityPolicyNone.URI,
        label="None",
        policy_class=sp.SecurityPolicyNone,
        secure=False,
        note="No signing and no encryption. Every message is readable on the wire.",
    ),
    PolicyInfo(
        uri=sp.SecurityPolicyBasic256Sha256.URI,
        label="Basic256Sha256",
        policy_class=sp.SecurityPolicyBasic256Sha256,
        secure=True,
        note="Recommended baseline: SignAndEncrypt with SHA-256 and AES-256.",
    ),
    PolicyInfo(
        uri=sp.SecurityPolicyAes128Sha256RsaOaep.URI,
        label="Aes128_Sha256_RsaOaep",
        policy_class=sp.SecurityPolicyAes128Sha256RsaOaep,
        secure=True,
    ),
    PolicyInfo(
        uri=sp.SecurityPolicyAes256Sha256RsaPss.URI,
        label="Aes256_Sha256_RsaPss",
        policy_class=sp.SecurityPolicyAes256Sha256RsaPss,
        secure=True,
    ),
    PolicyInfo(
        uri=sp.SecurityPolicyBasic256.URI,
        label="Basic256",
        policy_class=sp.SecurityPolicyBasic256,
        secure=True,
        deprecated=True,
        note="Deprecated by the OPC Foundation; prefer Basic256Sha256.",
    ),
    PolicyInfo(
        uri=sp.SecurityPolicyBasic128Rsa15.URI,
        label="Basic128Rsa15",
        policy_class=sp.SecurityPolicyBasic128Rsa15,
        secure=True,
        deprecated=True,
        note="Deprecated and considered weak; avoid on new installations.",
    ),
)

_BY_URI = {info.uri: info for info in _POLICIES}
_BY_LABEL = {info.label: info for info in _POLICIES}

MODE_NAMES: tuple[str, ...] = ("None", "Sign", "SignAndEncrypt")


def all_policies() -> tuple[PolicyInfo, ...]:
    """Selectable policies in GUI display order."""
    return _POLICIES


def policy_from_uri(uri: str | None) -> PolicyInfo | None:
    if not uri:
        return None
    return _BY_URI.get(uri)


def policy_from_label(label: str) -> PolicyInfo | None:
    return _BY_LABEL.get(label)


def policy_label(uri: str | None) -> str:
    info = policy_from_uri(uri)
    if info is not None:
        return info.label
    if not uri:
        return "unknown"
    # Show the fragment of an unknown policy URI, never the whole string.
    return uri.rsplit("#", 1)[-1]


def policy_class(uri: str) -> type | None:
    info = policy_from_uri(uri)
    return None if info is None else info.policy_class


def mode_name(mode: ua.MessageSecurityMode | int | str | None) -> str:
    """Canonical display name of a MessageSecurityMode."""
    if mode is None:
        return "None"
    if isinstance(mode, str):
        return mode
    try:
        value = ua.MessageSecurityMode(int(mode))
    except ValueError:
        return "Invalid"
    if value is ua.MessageSecurityMode.None_:
        return "None"
    return value.name


def mode_from_name(name: str) -> ua.MessageSecurityMode:
    if name == "None":
        return ua.MessageSecurityMode.None_
    try:
        return ua.MessageSecurityMode[name]
    except KeyError as exc:
        raise ValueError(f"Unknown message security mode {name!r}") from exc


def is_secure(policy_uri: str, security_mode: str) -> bool:
    """True when traffic is signed or encrypted.

    Deliberately strict: anything we cannot positively identify as secure is
    treated as not secure (used for warnings, never to grant permissions).
    """
    info = policy_from_uri(policy_uri)
    if info is None or not info.secure:
        return False
    return security_mode in ("Sign", "SignAndEncrypt")


def security_warnings(policy_uri: str, security_mode: str) -> list[str]:
    """Warnings to show in the GUI for the selected combination."""
    warnings: list[str] = []
    info = policy_from_uri(policy_uri)
    if info is None:
        warnings.append(f"Unknown security policy {policy_uri!r}; the connection is likely to fail.")
        return warnings
    if not info.secure:
        warnings.append("SecurityPolicy None: credentials and values cross the network unencrypted.")
    if info.deprecated:
        warnings.append(f"{info.label} is deprecated and should not be used for new installations.")
    if info.secure and security_mode == "Sign":
        warnings.append("Sign only: messages are authenticated but not encrypted.")
    return warnings


def default_application_uri(hostname: str | None = None) -> str:
    """Application URI used by this client.

    A client certificate must contain this URI in its subjectAltName, otherwise
    asyncua rejects the certificate during ``set_security``.
    """
    return f"urn:{hostname or socket.gethostname()}:OPCUAExplorer:Client"


def token_type_names(endpoint: ua.EndpointDescription) -> list[str]:
    names: list[str] = []
    for policy in endpoint.UserIdentityTokens or []:
        try:
            names.append(ua.UserTokenType(int(policy.TokenType)).name)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            names.append(f"Unknown({policy.TokenType})")
    # Anonymous first, then stable ordering.
    return sorted(set(names), key=lambda name: (name != "Anonymous", name))


def format_fingerprint(hex_digest: str) -> str:
    """Format a bare hex digest as colon-separated uppercase SHA-256."""
    clean = hex_digest.replace(":", "").replace(" ", "").upper()
    return ":".join(clean[i : i + 2] for i in range(0, len(clean), 2))


@dataclass(frozen=True, slots=True)
class CertificateSummary:
    """Locally parsed view of an application instance certificate."""

    sha256: str = ""
    subject: str = ""
    issuer: str = ""
    serial_number: str = ""
    not_before: str | None = None
    not_after: str | None = None
    dns_names: tuple[str, ...] = ()
    parse_error: str | None = None

    @property
    def fingerprint_colon(self) -> str:
        return format_fingerprint(self.sha256)

    @property
    def is_expired(self) -> bool:
        # Both values are ISO strings in UTC; comparing them lexicographically is
        # valid for the fixed format produced by iso_utc().
        if not self.not_after:
            return False
        return self.not_after < iso_utc(_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "sha256_colon": self.fingerprint_colon,
            "subject": self.subject,
            "issuer": self.issuer,
            "serial_number": self.serial_number,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "dns_names": list(self.dns_names),
            "parse_error": self.parse_error,
        }


def _now():  # small indirection to keep tests deterministic if ever needed
    from datetime import datetime

    return datetime.now(UTC)


def summarize_certificate(der: bytes | None) -> CertificateSummary:
    """Parse a DER certificate into a display summary.

    Never raises: an unparseable certificate still yields its SHA-256
    fingerprint, because that is what the operator must compare.
    """
    if not der:
        return CertificateSummary(parse_error="The endpoint did not provide a server certificate.")
    try:
        digest = hashes.Hash(hashes.SHA256())
        digest.update(der)
        sha256 = digest.finalize().hex()
    except Exception as exc:  # pragma: no cover - hashing a bytes object cannot fail
        return CertificateSummary(parse_error=f"Could not hash certificate: {exc}")

    try:
        cert = x509.load_der_x509_certificate(der)
        not_before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(tzinfo=UTC)
        not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=UTC)
        dns_names: tuple[str, ...] = ()
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            dns_names = tuple(san.value.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            dns_names = ()
        return CertificateSummary(
            sha256=sha256,
            subject=cert.subject.rfc4514_string(),
            issuer=cert.issuer.rfc4514_string(),
            serial_number=format(cert.serial_number, "x"),
            not_before=iso_utc(not_before),
            not_after=iso_utc(not_after),
            dns_names=dns_names,
        )
    except Exception as exc:
        return CertificateSummary(sha256=sha256, parse_error=f"Certificate could not be parsed: {exc}")


@dataclass(slots=True)
class ClientCertificateCheck:
    """What can be decided locally about the client certificate and key before connecting.

    Nothing here talks to a server: the files are parsed on this computer, so an
    expired certificate, a missing application URI or a key that does not belong to
    the certificate is reported *before* the connection attempt, where it would
    otherwise only appear as the server's ``BadSecurityChecksFailed``.
    """

    certificate_path: str = ""
    key_path: str = ""
    ok: bool = True
    subject: str = ""
    issuer: str = ""
    fingerprint_sha256: str = ""
    not_before: str | None = None
    not_after: str | None = None
    days_left: int | None = None
    #: ``None`` when no application URI was supplied or the certificate could not be read.
    application_uri_present: bool | None = None
    #: ``None`` when no key was given or it could not be read.
    key_matches: bool | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary_text(self) -> str:
        """One operator-facing line for the Connection page."""
        if not self.certificate_path:
            return "No client certificate selected."
        if self.problems:
            return " ".join(self.problems)
        parts = [f"Client certificate {self.subject or '(unparsed)'}"]
        if self.not_after:
            validity = f"valid until {self.not_after}"
            if self.days_left is not None:
                validity = f"{validity} ({self.days_left} day(s) left)"
            parts.append(validity)
        if self.application_uri_present is True:
            parts.append("application URI is in subjectAltName")
        if self.key_matches is True:
            parts.append("private key matches")
        text = "; ".join(parts) + "."
        if self.warnings:
            text = f"{text} {' '.join(self.warnings)}"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "certificate_path": self.certificate_path,
            "key_path": self.key_path,
            "ok": self.ok,
            "subject": self.subject,
            "issuer": self.issuer,
            "fingerprint_sha256": self.fingerprint_sha256,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "days_left": self.days_left,
            "application_uri_present": self.application_uri_present,
            "key_matches": self.key_matches,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }


def _load_certificate_file(path: Path) -> x509.Certificate:
    """Read a client certificate as PEM or DER, like the connection settings do."""
    data = path.read_bytes()
    if b"-----BEGIN" in data:
        return x509.load_pem_x509_certificate(data)
    return x509.load_der_x509_certificate(data)


def _public_key_bytes(public_key: Any) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def check_client_certificate(
    certificate_path: str | None,
    private_key_path: str | None = None,
    *,
    password: str = "",
    application_uri: str = "",
    now: datetime | None = None,
    expiring_days: int = 30,
) -> ClientCertificateCheck:
    """Parse the configured client certificate/key on this computer; never raises.

    A missing file, an unreadable key, an expired or not-yet-valid certificate, a
    missing application URI in ``subjectAltName`` and a key that does not belong to
    the certificate are reported as ``problems``; a certificate that expires within
    ``expiring_days`` is only a ``warning``.
    """
    check = ClientCertificateCheck(
        certificate_path=str(certificate_path or ""), key_path=str(private_key_path or "")
    )
    if not certificate_path:
        return check
    moment = (now or _now()).astimezone(UTC)

    try:
        certificate = _load_certificate_file(Path(certificate_path))
    except FileNotFoundError:
        check.problems.append(f"The client certificate {certificate_path} does not exist.")
        check.ok = False
        return check
    except Exception as exc:
        check.problems.append(f"The client certificate {certificate_path} could not be read: {exc}")
        check.ok = False
        return check

    der = certificate.public_bytes(serialization.Encoding.DER)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    check.fingerprint_sha256 = digest.finalize().hex()
    check.subject = certificate.subject.rfc4514_string()
    check.issuer = certificate.issuer.rfc4514_string()
    not_before = getattr(certificate, "not_valid_before_utc", None) or certificate.not_valid_before.replace(
        tzinfo=UTC
    )
    not_after = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after.replace(
        tzinfo=UTC
    )
    check.not_before = iso_utc(not_before)
    check.not_after = iso_utc(not_after)
    check.days_left = max(0, (not_after - moment).days)

    if not_after <= moment:
        check.problems.append(
            f"The client certificate expired on {check.not_after}; a server will reject it."
        )
    elif not_before > moment:
        check.problems.append(
            f"The client certificate is not valid before {check.not_before}; a server will reject it."
        )
    elif check.days_left <= expiring_days:
        check.warnings.append(f"It expires in {check.days_left} day(s) — renew it before then.")

    if application_uri:
        try:
            san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            uris = tuple(san.get_values_for_type(x509.UniformResourceIdentifier))
        except x509.ExtensionNotFound:
            uris = ()
        check.application_uri_present = application_uri in uris
        if not check.application_uri_present:
            check.problems.append(
                f"The client certificate does not contain {application_uri} as a subjectAltName URI; "
                "asyncua rejects such a certificate before connecting."
            )

    if private_key_path:
        try:
            key_data = Path(private_key_path).read_bytes()
            private_key = serialization.load_pem_private_key(
                key_data, password=password.encode("utf-8") if password else None
            )
        except FileNotFoundError:
            check.problems.append(f"The private key {private_key_path} does not exist.")
        except TypeError:
            check.problems.append(
                f"The private key {private_key_path} is encrypted; enter its password or choose another key."
            )
        except ValueError:
            check.problems.append(
                f"The private key {private_key_path} could not be read; the password may be wrong."
            )
        except Exception as exc:
            check.problems.append(f"The private key {private_key_path} could not be read: {exc}")
        else:
            check.key_matches = _public_key_bytes(private_key.public_key()) == _public_key_bytes(
                certificate.public_key()
            )
            if not check.key_matches:
                check.problems.append(
                    f"The private key {private_key_path} does not belong to this certificate."
                )

    check.ok = not check.problems
    return check


def endpoint_to_info(endpoint: ua.EndpointDescription) -> EndpointInfo:
    """Convert an asyncua EndpointDescription into the application model."""
    certificate = bytes(endpoint.ServerCertificate) if endpoint.ServerCertificate else b""
    summary = summarize_certificate(certificate or None)
    return EndpointInfo(
        endpoint_url=endpoint.EndpointUrl,
        security_policy_uri=endpoint.SecurityPolicyUri,
        security_policy_label=policy_label(endpoint.SecurityPolicyUri),
        security_mode=mode_name(endpoint.SecurityMode),
        security_level=int(endpoint.SecurityLevel or 0),
        token_types=token_type_names(endpoint),
        certificate_sha256=summary.sha256 or None,
        certificate_subject=summary.subject or None,
        certificate_not_after=summary.not_after,
        certificate_der=certificate or None,
        transport_profile_uri=endpoint.TransportProfileUri,
    )


def select_endpoint(endpoints: list[EndpointInfo], policy_uri: str, security_mode: str) -> EndpointInfo:
    """Return the advertised endpoint for an exact policy/mode pair.

    Raises :class:`EndpointNotAdvertisedError` instead of falling back to any
    other endpoint - this is the "never silently downgrade security" rule.
    """
    candidates = [
        endpoint
        for endpoint in endpoints
        if endpoint.security_policy_uri == policy_uri and endpoint.security_mode == security_mode
    ]
    if not candidates:
        advertised = ", ".join(sorted({endpoint.label() for endpoint in endpoints})) or "none"
        raise EndpointNotAdvertisedError(
            f"The server does not advertise {policy_label(policy_uri)} / {security_mode}. "
            f"Advertised endpoints: {advertised}. "
            "No automatic fallback to a weaker configuration was performed."
        )
    # Highest security level first; stable for equal levels.
    return max(candidates, key=lambda endpoint: endpoint.security_level)
