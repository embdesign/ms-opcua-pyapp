"""Security mapping, certificate summaries and the no-downgrade endpoint rule."""

from __future__ import annotations

import pytest
from asyncua import ua
from asyncua.crypto import security_policies as sp

from opcua_explorer.errors import EndpointNotAdvertisedError
from opcua_explorer.models import EndpointInfo
from opcua_explorer.opcua.security import (
    all_policies,
    endpoint_to_info,
    format_fingerprint,
    is_secure,
    mode_from_name,
    mode_name,
    policy_from_label,
    policy_from_uri,
    policy_label,
    security_warnings,
    select_endpoint,
    summarize_certificate,
    token_type_names,
)
from tests.support import make_certificate_der, sha256_of

NONE_URI = sp.SecurityPolicyNone.URI
BASIC256SHA256_URI = sp.SecurityPolicyBasic256Sha256.URI


def test_policy_table_matches_asyncua_uris() -> None:
    by_label = {info.label: info for info in all_policies()}
    assert by_label["None"].uri == NONE_URI
    assert by_label["Basic256Sha256"].uri == BASIC256SHA256_URI
    assert by_label["Basic256Sha256"].policy_class is sp.SecurityPolicyBasic256Sha256
    assert by_label["Basic128Rsa15"].deprecated is True
    assert by_label["None"].secure is False


def test_policy_lookup_helpers() -> None:
    assert policy_from_uri(BASIC256SHA256_URI).label == "Basic256Sha256"
    assert policy_from_label("Basic256").uri == sp.SecurityPolicyBasic256.URI
    assert policy_from_uri("http://example.org/unknown") is None
    assert policy_label("http://example.org/security#Custom") == "Custom"
    assert policy_label(None) == "unknown"


def test_mode_name_and_roundtrip() -> None:
    assert mode_name(ua.MessageSecurityMode.None_) == "None"
    assert mode_name(ua.MessageSecurityMode.Sign) == "Sign"
    assert mode_name(ua.MessageSecurityMode.SignAndEncrypt) == "SignAndEncrypt"
    assert mode_name(None) == "None"
    for name in ("None", "Sign", "SignAndEncrypt"):
        assert mode_name(mode_from_name(name)) == name


def test_is_secure_is_strict() -> None:
    assert is_secure(BASIC256SHA256_URI, "SignAndEncrypt") is True
    assert is_secure(BASIC256SHA256_URI, "Sign") is True
    assert is_secure(BASIC256SHA256_URI, "None") is False
    assert is_secure(NONE_URI, "SignAndEncrypt") is False
    assert is_secure("http://unknown/policy", "SignAndEncrypt") is False


def test_security_warnings() -> None:
    assert any("unencrypted" in warning for warning in security_warnings(NONE_URI, "None"))
    assert any("deprecated" in warning for warning in security_warnings(sp.SecurityPolicyBasic256.URI, "Sign"))
    assert any("not encrypted" in warning for warning in security_warnings(BASIC256SHA256_URI, "Sign"))
    assert security_warnings(BASIC256SHA256_URI, "SignAndEncrypt") == []


def test_summarize_certificate_extracts_fields() -> None:
    der = make_certificate_der(common_name="plant-server", dns_names=("plant.local", "10.0.0.5"))
    summary = summarize_certificate(der)
    assert summary.parse_error is None
    assert summary.sha256 == sha256_of(der)
    assert summary.fingerprint_colon.startswith(summary.sha256[:2].upper())
    assert "plant-server" in summary.subject
    assert summary.dns_names == ("plant.local", "10.0.0.5")
    assert summary.is_expired is False
    assert summary.not_before and summary.not_after


def test_summarize_expired_certificate() -> None:
    summary = summarize_certificate(make_certificate_der(expired=True))
    assert summary.parse_error is None
    assert summary.is_expired is True


def test_summarize_missing_or_invalid_certificate_never_raises() -> None:
    missing = summarize_certificate(None)
    assert missing.parse_error is not None
    assert missing.sha256 == ""

    invalid = summarize_certificate(b"not a certificate")
    assert invalid.parse_error is not None
    assert invalid.sha256 == sha256_of(b"not a certificate")


def test_format_fingerprint_groups_bytes() -> None:
    assert format_fingerprint("aabbccdd") == "AA:BB:CC:DD"


def test_token_type_names_are_unique_and_anonymous_first() -> None:
    endpoint = ua.EndpointDescription()
    endpoint.UserIdentityTokens = [
        ua.UserTokenPolicy(TokenType=ua.UserTokenType.UserName),
        ua.UserTokenPolicy(TokenType=ua.UserTokenType.Anonymous),
        ua.UserTokenPolicy(TokenType=ua.UserTokenType.Anonymous),
    ]
    assert token_type_names(endpoint) == ["Anonymous", "UserName"]


def test_endpoint_to_info_parses_certificate() -> None:
    der = make_certificate_der()
    endpoint = ua.EndpointDescription()
    endpoint.EndpointUrl = "opc.tcp://demo:4840"
    endpoint.SecurityPolicyUri = BASIC256SHA256_URI
    endpoint.SecurityMode = ua.MessageSecurityMode.SignAndEncrypt
    endpoint.SecurityLevel = 70
    endpoint.ServerCertificate = der
    endpoint.UserIdentityTokens = [ua.UserTokenPolicy(TokenType=ua.UserTokenType.Anonymous)]

    info = endpoint_to_info(endpoint)
    assert info.security_policy_label == "Basic256Sha256"
    assert info.security_mode == "SignAndEncrypt"
    assert info.is_secure is True
    assert info.certificate_sha256 == sha256_of(der)
    assert info.certificate_der == der
    assert info.token_types == ["Anonymous"]
    assert info.label() == "Basic256Sha256 / SignAndEncrypt"


def _endpoint(uri: str, mode: str, level: int) -> EndpointInfo:
    return EndpointInfo(
        endpoint_url="opc.tcp://demo:4840",
        security_policy_uri=uri,
        security_mode=mode,
        security_policy_label=policy_label(uri),
        security_level=level,
    )


def test_select_endpoint_requires_exact_pair() -> None:
    endpoints = [
        _endpoint(NONE_URI, "None", 0),
        _endpoint(BASIC256SHA256_URI, "SignAndEncrypt", 70),
        _endpoint(BASIC256SHA256_URI, "Sign", 60),
    ]
    assert select_endpoint(endpoints, BASIC256SHA256_URI, "SignAndEncrypt").security_level == 70
    assert select_endpoint(endpoints, BASIC256SHA256_URI, "Sign").security_level == 60


def test_select_endpoint_never_downgrades() -> None:
    endpoints = [_endpoint(NONE_URI, "None", 0), _endpoint(BASIC256SHA256_URI, "SignAndEncrypt", 70)]
    with pytest.raises(EndpointNotAdvertisedError) as excinfo:
        select_endpoint(endpoints, BASIC256SHA256_URI, "Sign")
    message = str(excinfo.value)
    assert "does not advertise" in message
    assert "None / None" in message  # advertises what is available
    assert "No automatic fallback" in message


def test_select_endpoint_prefers_highest_security_level() -> None:
    endpoints = [
        _endpoint(BASIC256SHA256_URI, "SignAndEncrypt", 60),
        _endpoint(BASIC256SHA256_URI, "SignAndEncrypt", 80),
    ]
    assert select_endpoint(endpoints, BASIC256SHA256_URI, "SignAndEncrypt").security_level == 80
