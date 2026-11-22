"""Unit tests for local client-certificate creation (pure, no Qt, no server).

The generator is the only place this project creates a key or signs a certificate,
so these tests pin down what the files contain, what a second run refuses and what
the operator is told.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from opcua_explorer.certificate_creation import (
    KEY_FILE_MODE,
    CertificateCreationError,
    create_certificate,
    planned_paths,
)
from opcua_explorer.models import CertificateSpec

_URI = "urn:opcua-explorer:test:client"


def _spec(tmp_path, **overrides) -> CertificateSpec:
    values = {
        "mode": "self-signed",
        "common_name": "line1-client",
        "application_uri": _URI,
        "validity_days": 30,
        "key_size": 2048,
        "output_dir": str(tmp_path),
    }
    values.update(overrides)
    return CertificateSpec(**values)


def _load_certificate(path: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def test_self_signed_certificate_carries_the_application_uri(tmp_path) -> None:
    files = create_certificate(_spec(tmp_path))

    assert files.mode == "self-signed"
    certificate = _load_certificate(files.certificate_path)
    assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "line1-client"
    # asyncua rejects a client certificate whose subjectAltName lacks this URI.
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert _URI in san.get_values_for_type(x509.UniformResourceIdentifier)
    eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert constraints.ca is False
    # Self-signed: subject == issuer, and the fingerprint is the SHA-256 of the DER.
    assert certificate.subject == certificate.issuer
    der = certificate.public_bytes(serialization.Encoding.DER)
    assert files.fingerprint_sha256 == hashlib.sha256(der).hexdigest()
    assert len(files.fingerprint_sha256) == 64


def test_certificate_validity_follows_the_request_and_the_injected_clock(tmp_path) -> None:
    moment = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

    files = create_certificate(_spec(tmp_path, validity_days=10), now=moment)

    assert files.not_after is not None and files.not_before is not None
    certificate = _load_certificate(files.certificate_path)
    assert certificate.not_valid_after_utc - certificate.not_valid_before_utc == timedelta(days=11)
    assert files.not_after.startswith("2026-12-02")


def test_the_private_key_is_a_matching_unencrypted_pkcs8_file(tmp_path) -> None:
    files = create_certificate(_spec(tmp_path))

    key = serialization.load_pem_private_key(Path(files.key_path).read_bytes(), password=None)
    certificate = _load_certificate(files.certificate_path)
    assert key.public_key().public_numbers() == certificate.public_key().public_numbers()
    assert files.key_size == 2048
    assert sorted(files.written) == sorted([files.certificate_path, files.key_path])
    if sys.platform != "win32":
        mode = os.stat(files.key_path).st_mode
        assert stat.S_IMODE(mode) == KEY_FILE_MODE


def test_a_request_is_a_csr_and_not_a_certificate(tmp_path) -> None:
    files = create_certificate(_spec(tmp_path, mode="csr"))

    assert files.mode == "csr"
    assert files.certificate_path is None
    assert files.request_path is not None
    assert files.not_before is None and files.not_after is None
    request = x509.load_pem_x509_csr(Path(files.request_path).read_bytes())
    assert request.is_signature_valid
    assert request.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "line1-client"
    san = request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert _URI in san.get_values_for_type(x509.UniformResourceIdentifier)
    der = request.public_bytes(serialization.Encoding.DER)
    assert files.fingerprint_sha256 == hashlib.sha256(der).hexdigest()
    assert "cannot issue" in files.summary_text()


def test_existing_files_are_never_replaced_silently(tmp_path) -> None:
    files = create_certificate(_spec(tmp_path))
    before = Path(files.key_path).read_bytes()

    with pytest.raises(CertificateCreationError, match="already exist"):
        create_certificate(_spec(tmp_path))

    # The refused run wrote nothing at all.
    assert Path(files.key_path).read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 2


def test_overwrite_replaces_the_files_and_changes_the_fingerprint(tmp_path) -> None:
    first = create_certificate(_spec(tmp_path))

    second = create_certificate(_spec(tmp_path, overwrite=True))

    assert second.certificate_path == first.certificate_path
    assert second.fingerprint_sha256 != first.fingerprint_sha256


def test_the_request_refuses_what_it_cannot_honest_about(tmp_path) -> None:
    with pytest.raises(CertificateCreationError, match="common name"):
        create_certificate(_spec(tmp_path, common_name="   "))
    with pytest.raises(CertificateCreationError, match="application URI"):
        create_certificate(_spec(tmp_path, application_uri=""))
    with pytest.raises(CertificateCreationError, match="application URI"):
        create_certificate(_spec(tmp_path, application_uri="not a uri with spaces"))
    with pytest.raises(CertificateCreationError, match="folder"):
        create_certificate(_spec(tmp_path, output_dir=""))


def test_planned_paths_are_what_the_generator_writes(tmp_path) -> None:
    main, key = planned_paths(_spec(tmp_path))

    assert main.name == "line1-client-client-cert.pem"
    assert key.name == "line1-client-client-key.pem"
    main_request, key_request = planned_paths(_spec(tmp_path, mode="csr"))
    assert main_request.name == "line1-client-client.csr.pem"
    assert key_request == key


def test_a_missing_folder_is_created(tmp_path) -> None:
    target = tmp_path / "nested" / "certs"

    files = create_certificate(_spec(str(target), output_dir=str(target)))

    assert target.is_dir()
    assert files.certificate_path is not None and os.path.exists(files.certificate_path)
