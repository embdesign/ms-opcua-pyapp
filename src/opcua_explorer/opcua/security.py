"""Security-policy mapping, certificate inspection and anti-downgrade checks.

Everything in this module is pure Python (no session required) so it can be unit
tested without a server. The policy URIs and classes were verified against
asyncua 2.1.0 (``asyncua.crypto.security_policies``).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from asyncua import ua
from asyncua.crypto import security_policies as sp
from cryptography import x509
from cryptography.hazmat.primitives import hashes

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
