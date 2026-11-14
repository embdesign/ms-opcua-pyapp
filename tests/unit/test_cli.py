"""Unit tests for the headless export CLI (argument handling only, no network)."""

from __future__ import annotations

import io
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from opcua_explorer import cli
from opcua_explorer.config import NONE_POLICY_URI, TrustStore
from opcua_explorer.defaults import DEFAULT_MAX_DEPENDENCIES, DEFAULT_MAX_NODES
from opcua_explorer.models import EndpointInfo

from ..support import make_certificate_der, sha256_of

ENDPOINT = "opc.tcp://127.0.0.1:4840"
SECURE_POLICY = "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"


def _args(*extra: str):
    return cli.build_parser().parse_args(["export", "--endpoint", ENDPOINT, *extra])


def test_the_command_word_is_optional() -> None:
    with_command = _args("--quiet")
    without_command = cli.build_parser().parse_args(["--endpoint", ENDPOINT, "--quiet"])

    assert with_command.command == "export"
    assert without_command.command == "export"
    assert without_command.endpoint == ENDPOINT


def test_connection_defaults_are_the_safe_ones() -> None:
    settings = cli.settings_from_args(_args())

    assert settings.security_policy_uri == NONE_POLICY_URI
    assert settings.security_mode == "None"
    assert settings.auth_mode == "anonymous"
    # A one-shot export must not keep retrying a dead link in the background.
    assert settings.auto_reconnect is False
    assert settings.password is None


@pytest.mark.parametrize(
    ("value", "expected_fragment"),
    [("Basic256Sha256", "Basic256Sha256"), ("http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256", "Basic256Sha256")],
)
def test_policy_accepts_a_label_or_a_uri(value: str, expected_fragment: str) -> None:
    settings = cli.settings_from_args(
        _args(
            "--policy",
            value,
            "--mode",
            "SignAndEncrypt",
            "--client-certificate",
            "client.der",
            "--client-key",
            "client.pem",
        )
    )

    assert settings.security_policy_uri.endswith("#" + expected_fragment)
    assert settings.is_secure is True
    assert settings.client_certificate_path == "client.der"
    assert settings.client_private_key_path == "client.pem"


def test_unknown_policy_is_refused_before_connecting() -> None:
    with pytest.raises(cli.CliError, match="Unknown security policy"):
        cli.settings_from_args(_args("--policy", "NotAPolicy"))


def test_max_nodes_zero_means_unlimited_and_defaults_are_shared() -> None:
    assert cli.traversal_config_from_args(_args()).max_nodes == DEFAULT_MAX_NODES
    assert cli.traversal_config_from_args(_args("--max-nodes", "0")).max_nodes is None
    assert cli.traversal_config_from_args(_args("--max-depth", "0")).max_depth is None


def test_namespace_scope_becomes_a_filter() -> None:
    config = cli.traversal_config_from_args(_args("--namespace", "2", "--namespace", "3"))

    assert config.namespace_filter == frozenset({2, 3})


def test_values_flag_reaches_download_and_nodeset_config() -> None:
    args = _args("--values")

    assert cli.traversal_config_from_args(args).include_values is True
    assert cli.nodeset_config_from_args(args).include_values is True


def test_nodeset_flags_map_to_the_shared_config() -> None:
    args = _args("--no-dependencies", "--max-dependencies", "0", "--schema", " ua.xsd ", "--no-pretty")
    config = cli.nodeset_config_from_args(args)

    assert config.include_dependencies is False
    assert config.max_dependency_nodes == 0
    assert config.schema_path == "ua.xsd"
    assert config.pretty is False
    assert cli.nodeset_config_from_args(_args()).max_dependency_nodes == DEFAULT_MAX_DEPENDENCIES


def test_user_name_requires_a_password_source(monkeypatch, capsys) -> None:
    monkeypatch.delenv(cli.PASSWORD_ENV, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(cli.CliError, match=cli.PASSWORD_ENV):
        cli.settings_from_args(_args("--user", "operator"))


def test_password_can_come_from_an_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("CLI_TEST_PASSWORD", "secret-value")

    settings = cli.settings_from_args(_args("--user", "operator", "--password-env", "CLI_TEST_PASSWORD"))

    assert settings.auth_mode == "username"
    assert settings.username == "operator"
    assert settings.password == "secret-value"
    # The secret must not leak into a log-safe description.
    assert "secret-value" not in str(settings.redacted())


def test_client_certificate_and_key_must_be_given_together() -> None:
    with pytest.raises(cli.CliError, match="must be given together"):
        cli.settings_from_args(_args("--client-certificate", "client.der"))


def test_certificate_identity_needs_a_certificate() -> None:
    with pytest.raises(cli.CliError, match="Certificate authentication requires"):
        cli.settings_from_args(_args("--certificate-identity"))


def test_user_and_certificate_identity_are_mutually_exclusive() -> None:
    with pytest.raises(cli.CliError, match="mutually exclusive"):
        cli.settings_from_args(_args("--user", "operator", "--certificate-identity"))


def test_default_output_name_is_derived_from_the_endpoint() -> None:
    assert cli.default_output_path("opc.tcp://192.168.1.100:4840").name == "192.168.1.100-4840.NodeSet2.xml"
    assert cli.default_output_path("plc").name == "plc-4840.NodeSet2.xml"


def test_main_reports_usage_errors_with_exit_code_2(capsys) -> None:
    code = cli.main(["export", "--endpoint", ENDPOINT, "--policy", "NotAPolicy"])

    assert code == cli.EXIT_USAGE
    assert "Unknown security policy" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Server-certificate approval (fakes, no server)
# ---------------------------------------------------------------------------


class _FakeDiscovery:
    def __init__(self, endpoint: EndpointInfo | None) -> None:
        self._endpoint = endpoint

    def find(self, policy_uri: str, security_mode: str) -> EndpointInfo | None:
        endpoint = self._endpoint
        if endpoint is None:
            return None
        if endpoint.security_policy_uri == policy_uri and endpoint.security_mode == security_mode:
            return endpoint
        return None

    def advertised_combinations(self) -> list[str]:
        return [] if self._endpoint is None else [self._endpoint.label()]


class _FakeService:
    def __init__(self, endpoint: EndpointInfo | None) -> None:
        self.discovery = _FakeDiscovery(endpoint)

    async def discover(self, url: str, *, request_timeout: float = 10.0, connect_timeout: float | None = None):
        return self.discovery


def _secure_endpoint(der: bytes, *, policy_uri: str = SECURE_POLICY, mode: str = "SignAndEncrypt") -> EndpointInfo:
    return EndpointInfo(
        endpoint_url=ENDPOINT,
        security_policy_uri=policy_uri,
        security_mode=mode,
        security_policy_label="Basic256Sha256",
        certificate_sha256=sha256_of(der),
        certificate_der=der,
    )


async def test_an_unapproved_server_certificate_is_refused(tmp_path) -> None:
    der = make_certificate_der()
    settings = cli.settings_from_args(
        _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt", "--client-certificate", "c.der", "--client-key", "c.pem")
    )

    with pytest.raises(cli.CliError, match="not approved"):
        await cli.approve_server_certificate(
            _FakeService(_secure_endpoint(der)),
            settings,
            _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt"),
            TrustStore(tmp_path / "trust.json"),
            cli.ProgressPrinter(io.StringIO(), quiet=True),
        )
    assert settings.server_certificate_der is None


async def test_explicit_approval_remembers_the_fingerprint(tmp_path) -> None:
    der = make_certificate_der()
    settings = cli.settings_from_args(
        _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt", "--client-certificate", "c.der", "--client-key", "c.pem")
    )
    trust_store = TrustStore(tmp_path / "trust.json")
    args = _args(
        "--policy",
        SECURE_POLICY,
        "--mode",
        "SignAndEncrypt",
        "--client-certificate",
        "c.der",
        "--client-key",
        "c.pem",
        "--trust-server-certificate",
    )

    await cli.approve_server_certificate(
        _FakeService(_secure_endpoint(der)), settings, args, trust_store, cli.ProgressPrinter(io.StringIO(), quiet=True)
    )

    assert settings.server_certificate_der == der
    assert trust_store.is_trusted(sha256_of(der)) is True


async def test_an_already_trusted_certificate_needs_no_flag(tmp_path) -> None:
    der = make_certificate_der()
    trust_store = TrustStore(tmp_path / "trust.json")
    trust_store.add(sha256_of(der), subject="test-server", endpoint_url=ENDPOINT)
    settings = cli.settings_from_args(
        _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt", "--client-certificate", "c.der", "--client-key", "c.pem")
    )

    await cli.approve_server_certificate(
        _FakeService(_secure_endpoint(der)), settings, _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt"), trust_store, cli.ProgressPrinter(io.StringIO(), quiet=True)
    )

    assert settings.server_certificate_der == der


@pytest.mark.parametrize("encoding", [Encoding.DER, Encoding.PEM])
async def test_a_certificate_file_must_match_the_advertised_certificate(tmp_path, encoding) -> None:
    advertised = make_certificate_der(common_name="server")
    other = make_certificate_der(common_name="someone-else")
    certificate_file = tmp_path / "server.cer"
    certificate_file.write_bytes(x509.load_der_x509_certificate(other).public_bytes(encoding))
    settings = cli.settings_from_args(
        _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt", "--client-certificate", "c.der", "--client-key", "c.pem")
    )

    with pytest.raises(cli.CliError, match="does not match"):
        await cli.approve_server_certificate(
            _FakeService(_secure_endpoint(advertised)),
            settings,
            _args("--server-certificate", str(certificate_file)),
            TrustStore(tmp_path / "trust.json"),
            cli.ProgressPrinter(io.StringIO(), quiet=True),
        )


@pytest.mark.parametrize("encoding", [Encoding.DER, Encoding.PEM])
async def test_a_matching_certificate_file_is_used_as_is(tmp_path, encoding) -> None:
    der = make_certificate_der(common_name="server")
    certificate_file = tmp_path / "server.cer"
    certificate_file.write_bytes(x509.load_der_x509_certificate(der).public_bytes(encoding))
    settings = cli.settings_from_args(
        _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt", "--client-certificate", "c.der", "--client-key", "c.pem")
    )

    await cli.approve_server_certificate(
        _FakeService(_secure_endpoint(der)),
        settings,
        _args("--server-certificate", str(certificate_file)),
        TrustStore(tmp_path / "trust.json"),
        cli.ProgressPrinter(io.StringIO(), quiet=True),
    )

    assert settings.server_certificate_der == der


async def test_a_policy_the_server_does_not_advertise_is_refused(tmp_path) -> None:
    der = make_certificate_der()
    settings = cli.settings_from_args(
        _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt", "--client-certificate", "c.der", "--client-key", "c.pem")
    )

    with pytest.raises(cli.CliError, match="not advertised"):
        await cli.approve_server_certificate(
            _FakeService(_secure_endpoint(der, mode="Sign")),
            settings,
            _args("--policy", SECURE_POLICY, "--mode", "SignAndEncrypt"),
            TrustStore(tmp_path / "trust.json"),
            cli.ProgressPrinter(io.StringIO(), quiet=True),
        )
