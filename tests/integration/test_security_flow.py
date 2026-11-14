"""Secure connection, certificate approval and anti-downgrade rules against a real server."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
from asyncua import Client
from cryptography.x509 import ExtendedKeyUsageOID

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.errors import (
    CertificateRejectedError,
    ConnectionFailedError,
    EndpointNotAdvertisedError,
)
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.opcua.security import default_application_uri

BASIC256SHA256 = "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"


async def _make_certificate(uri: str, key: Path, cert: Path, *, server: bool) -> tuple[Path, Path]:
    helper = Client("opc.tcp://127.0.0.1")
    helper.application_uri = uri
    extended = [ExtendedKeyUsageOID.CLIENT_AUTH]
    if server:
        extended.append(ExtendedKeyUsageOID.SERVER_AUTH)
    certificate, key_file = await helper.setup_self_signed_certificate(
        key, cert, host_name=socket.gethostname(), extended=extended
    )
    return certificate, key_file


@dataclass
class SecureEnv:
    """A secure demo server plus the client credentials needed to talk to it."""

    model: object
    client_cert: str
    client_key: str

    @property
    def endpoint(self) -> str:
        return str(self.model.endpoint)  # type: ignore[attr-defined]


@pytest.fixture
async def secure_server(tmp_path):
    server_cert, server_key = await _make_certificate(
        "urn:opcua-explorer:demo:server", tmp_path / "server.pem", tmp_path / "server.der", server=True
    )
    client_cert, client_key = await _make_certificate(
        default_application_uri(), tmp_path / "client.pem", tmp_path / "client.der", server=False
    )
    model = await start_demo_server(
        certificate=str(server_cert), private_key=str(server_key), update_values=False
    )
    try:
        yield SecureEnv(model=model, client_cert=str(client_cert), client_key=str(client_key))
    finally:
        await stop_demo_server(model)


def _settings(env: SecureEnv, certificate_der: bytes | None) -> ConnectionSettings:
    return ConnectionSettings(
        endpoint_url=env.endpoint,
        security_policy_uri=BASIC256SHA256,
        security_mode="SignAndEncrypt",
        client_certificate_path=env.client_cert,
        client_private_key_path=env.client_key,
        server_certificate_der=certificate_der,
        connect_timeout=20,
        request_timeout=20,
    )


async def test_secure_server_advertises_only_secure_endpoint(secure_server) -> None:
    service = OpcUaService()
    try:
        discovery = await service.discover(secure_server.endpoint)
    finally:
        await service.disconnect()

    labels = {endpoint.label() for endpoint in discovery.endpoints}
    assert labels == {"Basic256Sha256 / SignAndEncrypt"}
    endpoint = discovery.endpoints[0]
    assert endpoint.is_secure is True
    assert endpoint.certificate_sha256
    assert endpoint.certificate_der


async def test_secure_connect_requires_approval_and_then_works(secure_server, tmp_path) -> None:
    trust_path = tmp_path / "trust.json"
    service = OpcUaService(trust_store=TrustStore(trust_path))
    try:
        discovery = await service.discover(secure_server.endpoint)
        endpoint = discovery.endpoints[0]

        # 1. No approved certificate -> the request is refused before connecting.
        with pytest.raises(ConnectionFailedError) as excinfo:
            await service.connect(_settings(secure_server, None))
        assert "approved" in str(excinfo.value).lower()

        # 2. Trust store starts empty, then records the explicit approval.
        assert service.certificate_is_trusted(endpoint.certificate_der) is False
        service.approve_certificate(
            endpoint.certificate_der,
            subject=endpoint.certificate_subject or "",
            endpoint_url=endpoint.endpoint_url,
            not_after=endpoint.certificate_not_after,
        )
        assert service.certificate_is_trusted(endpoint.certificate_der) is True
        assert trust_path.exists()
        assert TrustStore(trust_path).is_trusted(endpoint.certificate_sha256)

        # 3. With the approved DER bytes the secure session works.
        info = await service.connect(_settings(secure_server, endpoint.certificate_der))
        assert info.security_mode == "SignAndEncrypt"
        assert info.security_policy_label == "Basic256Sha256"
        assert info.server_certificate_sha256 == endpoint.certificate_sha256

        children = await service.browse_children("i=85")
        assert children, "browsing must work over the encrypted channel"
    finally:
        await service.shutdown()


async def test_changed_certificate_is_rejected(secure_server, tmp_path) -> None:
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    try:
        discovery = await service.discover(secure_server.endpoint)
        endpoint = discovery.endpoints[0]
        other_der = (await _make_certificate(
            "urn:opcua-explorer:demo:server", tmp_path / "other.pem", tmp_path / "other.der", server=True
        ))[0]
        wrong_der = Path(other_der).read_bytes()

        with pytest.raises(CertificateRejectedError) as excinfo:
            await service.connect(_settings(secure_server, wrong_der))
        assert "changed" in str(excinfo.value).lower()
        assert endpoint.certificate_der != wrong_der
    finally:
        await service.shutdown()


async def test_unadvertised_security_mode_is_not_downgraded(secure_server, tmp_path) -> None:
    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    try:
        discovery = await service.discover(secure_server.endpoint)
        endpoint = discovery.endpoints[0]
        settings = _settings(secure_server, endpoint.certificate_der)
        settings.security_mode = "Sign"  # the demo server only offers SignAndEncrypt

        with pytest.raises(EndpointNotAdvertisedError) as excinfo:
            await service.connect(settings)
        assert "SignAndEncrypt" in str(excinfo.value)
    finally:
        await service.shutdown()
