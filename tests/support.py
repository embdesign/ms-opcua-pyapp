"""Test helpers (no fixtures here, so tests can import them directly)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from opcua_explorer import __version__


def make_certificate_der(
    *,
    common_name: str = "test-server",
    valid_days: int = 30,
    dns_names: tuple[str, ...] = ("localhost",),
    expired: bool = False,
) -> bytes:
    """Create a throw-away self-signed DER certificate for tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    not_before = now - timedelta(days=2)
    not_after = now - timedelta(days=1) if expired else now + timedelta(days=valid_days)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
    )
    if dns_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]), critical=False
        )
    certificate = builder.sign(key, hashes.SHA256())
    return certificate.public_bytes(serialization.Encoding.DER)


def sha256_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def make_test_run(*, failing: bool = False, cancelled: bool = False):
    """Build a representative :class:`TestRun` for reporting tests."""
    from opcua_explorer.models import (
        STATUS_FAILED,
        STATUS_PASSED,
        STATUS_SKIPPED,
        ConnectionInfo,
        TestCheck,
        TestConfig,
        TestRun,
    )

    connection = ConnectionInfo(
        endpoint_url="opc.tcp://demo:4840",
        security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
        security_policy_label="Basic256Sha256",
        security_mode="SignAndEncrypt",
        auth_mode="username",
        server_application_name="Demo Server",
        manufacturer_name="opcua-explorer",
        software_version=__version__,
        session_timeout_ms=600_000,
        server_certificate_sha256="ab" * 32,
    )
    run = TestRun(
        endpoint_url="opc.tcp://demo:4840",
        config=TestConfig(variable_node_id="ns=2;s=Temperature", notification_wait_seconds=1.0),
        connection=connection,
        environment={"application": f"opcua-explorer {__version__}", "python": "3.11.2", "asyncua": "2.1.0"},
        coverage_limitations=["Static limitation note."],
        inaccessible_nodes={"ns=2;s=Denied": "BadUserAccessDenied"},
        notes=["A run note."],
        cancelled=cancelled,
    )
    run.checks = [
        TestCheck(
            id="endpoint_discovery",
            name="Endpoint discovery",
            category="connection",
            status=STATUS_PASSED,
            detail="2 endpoint(s); active configuration is advertised.",
            duration_ms=3.2,
            status_codes=[{"operation": "GetEndpoints", "status": "Good"}],
            data={"endpoint_count": 2},
        ),
        TestCheck(
            id="subscription_data_change",
            name="Monitored-item <script>alert('x')</script>",
            category="subscription",
            status=STATUS_SKIPPED,
            detail='No change observed within 1.0 s for "ns=2;s=<Temperature>"',
            duration_ms=1005.0,
            status_codes=[
                {"operation": "DataChange", "status": "Good", "node_id": "ns=2;s=<Temperature>"}
            ],
            notes=["This is not a failure."],
            data={"notifications_received": 1, "value_changes_observed": 0},
        ),
        TestCheck(
            id="latency",
            name="Bounded read latency",
            category="latency",
            status=STATUS_FAILED if failing else STATUS_PASSED,
            detail="median 0.8 ms",
            duration_ms=9.5,
            error="RuntimeError: boom <bad>" if failing else None,
        ),
    ]
    run.finished_at = run.started_at + timedelta(seconds=1.5)
    return run

