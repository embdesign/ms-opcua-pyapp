"""Unit tests for the local client-certificate check (no server).

The check runs before connecting, so these tests pin down what is a *problem*
(the server would refuse the certificate) and what is only a *warning*.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from opcua_explorer.certificate_creation import create_certificate
from opcua_explorer.models import CertificateSpec
from opcua_explorer.opcua.security import check_client_certificate

_URI = "urn:opcua-explorer:test:client"
_NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _pair(tmp_path, *, uri: str = _URI, now: datetime = _NOW, days: int = 365):
    spec = CertificateSpec(
        mode="self-signed",
        common_name="line1-client",
        application_uri=uri,
        validity_days=days,
        output_dir=str(tmp_path),
    )
    return create_certificate(spec, now=now)


def test_a_fresh_certificate_and_its_key_pass(tmp_path) -> None:
    files = _pair(tmp_path)

    check = check_client_certificate(
        files.certificate_path, files.key_path, application_uri=_URI, now=_NOW
    )

    assert check.ok is True
    assert check.problems == []
    assert check.subject.endswith("CN=line1-client")
    assert check.application_uri_present is True
    assert check.key_matches is True
    assert check.days_left == 365
    assert "private key matches" in check.summary_text()


def test_an_expired_certificate_is_a_problem(tmp_path) -> None:
    files = _pair(tmp_path, days=30)

    check = check_client_certificate(
        files.certificate_path, None, application_uri=_URI, now=_NOW + timedelta(days=60)
    )

    assert check.ok is False
    assert any("expired on" in problem for problem in check.problems)
    assert check.summary_text().startswith("The client certificate expired")


def test_a_certificate_from_the_future_is_a_problem(tmp_path) -> None:
    files = _pair(tmp_path, now=_NOW + timedelta(days=10))

    check = check_client_certificate(files.certificate_path, None, application_uri=_URI, now=_NOW)

    assert check.ok is False
    assert any("not valid before" in problem for problem in check.problems)


def test_a_certificate_that_expires_soon_is_only_a_warning(tmp_path) -> None:
    files = _pair(tmp_path, days=10)

    check = check_client_certificate(files.certificate_path, None, application_uri=_URI, now=_NOW)

    assert check.ok is True
    assert check.problems == []
    assert any("expires in 10 day(s)" in warning for warning in check.warnings)
    assert "expires in 10 day(s)" in check.summary_text()


def test_a_missing_application_uri_in_the_certificate_is_a_problem(tmp_path) -> None:
    files = _pair(tmp_path, uri="urn:someone-else:client")

    check = check_client_certificate(
        files.certificate_path, None, application_uri=_URI, now=_NOW
    )

    assert check.ok is False
    assert check.application_uri_present is False
    assert any("subjectAltName" in problem for problem in check.problems)


def test_the_key_must_belong_to_the_certificate(tmp_path) -> None:
    first = _pair(tmp_path / "a")
    second = _pair(tmp_path / "b")

    check = check_client_certificate(
        first.certificate_path, second.key_path, application_uri=_URI, now=_NOW
    )

    assert check.ok is False
    assert check.key_matches is False
    assert any("does not belong" in problem for problem in check.problems)


def test_an_encrypted_key_needs_its_password(tmp_path) -> None:
    files = _pair(tmp_path)
    key = serialization.load_pem_private_key(Path(files.key_path).read_bytes(), password=None)
    encrypted = tmp_path / "encrypted-key.pem"
    encrypted.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"secret"),
        )
    )

    without = check_client_certificate(
        files.certificate_path, str(encrypted), application_uri=_URI, now=_NOW
    )
    with_password = check_client_certificate(
        files.certificate_path, str(encrypted), password="secret", application_uri=_URI, now=_NOW
    )

    assert without.ok is False
    assert any("encrypted" in problem for problem in without.problems)
    assert with_password.ok is True
    assert with_password.key_matches is True


def test_missing_files_are_reported_without_raising(tmp_path) -> None:
    no_certificate = check_client_certificate(tmp_path / "nope.pem", application_uri=_URI, now=_NOW)
    assert no_certificate.ok is False
    assert any("does not exist" in problem for problem in no_certificate.problems)

    files = _pair(tmp_path)
    no_key = check_client_certificate(
        files.certificate_path, str(tmp_path / "nope-key.pem"), application_uri=_URI, now=_NOW
    )
    assert any("does not exist" in problem for problem in no_key.problems)


def test_an_unconfigured_certificate_is_not_a_finding() -> None:
    check = check_client_certificate(None)

    assert check.ok is True
    assert check.problems == []
    assert check.summary_text() == "No client certificate selected."
    assert check.to_dict()["certificate_path"] == ""
