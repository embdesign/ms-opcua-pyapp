"""Configuration, trust store and the "no credentials on disk" guarantee."""

from __future__ import annotations

import json

import pytest

from opcua_explorer.config import (
    NONE_POLICY_URI,
    AppSettings,
    ConnectionProfile,
    ConnectionSettings,
    TrustStore,
    assert_no_secrets,
    normalize_fingerprint,
)


def test_normalize_fingerprint() -> None:
    assert normalize_fingerprint("AA:BB:CC") == "aabbcc"
    assert normalize_fingerprint(" aa bb cc ") == "aabbcc"


def test_trust_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "trust.json"
    store = TrustStore(path)
    assert store.is_trusted("aa:bb") is False

    entry = store.add("AA:BB", subject="CN=server", endpoint_url="opc.tcp://x:4840", not_after="2027-01-01T00:00:00Z")
    assert entry.sha256 == "aabb"
    assert store.is_trusted("aa:bb") is True
    assert store.get("AA:BB").subject == "CN=server"

    reloaded = TrustStore(path)
    assert reloaded.is_trusted("aabb") is True
    assert reloaded.entries[0].endpoint_url == "opc.tcp://x:4840"

    assert store.remove("aa:bb") is True
    assert store.remove("aa:bb") is False
    assert TrustStore(path).entries == []


def test_trust_store_tolerates_corrupt_file(tmp_path) -> None:
    path = tmp_path / "trust.json"
    path.write_text("{not json", encoding="utf-8")
    assert TrustStore(path).entries == []


def test_settings_roundtrip_without_credentials(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = AppSettings(last_endpoint="opc.tcp://plant:4840")
    settings.profiles.append(
        ConnectionProfile(
            name="Plant",
            endpoint_url="opc.tcp://plant:4840",
            security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
            security_mode="SignAndEncrypt",
            auth_mode="username",
            username="operator",
        )
    )
    settings.save(path)

    text = path.read_text(encoding="utf-8")
    # The note may *mention* passwords; no password may be stored as a field.
    assert '"password"' not in text
    assert "private_key_password" not in text

    reloaded = AppSettings.load(path)
    assert reloaded.last_endpoint == "opc.tcp://plant:4840"
    assert reloaded.profiles[0].username == "operator"
    assert reloaded.credentials_in_profiles is False


def test_settings_roundtrip_keeps_auto_reconnect(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = AppSettings(auto_reconnect=False)
    settings.profiles.append(
        ConnectionProfile(name="Manual", endpoint_url="opc.tcp://x:4840", auto_reconnect=False)
    )
    settings.save(path)

    reloaded = AppSettings.load(path)
    assert reloaded.auto_reconnect is False
    assert reloaded.profiles[0].auto_reconnect is False

    # Settings written by an older build (no key) default to reconnecting.
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"last_endpoint": "opc.tcp://old:4840"}), encoding="utf-8")
    assert AppSettings.load(legacy).auto_reconnect is True


def test_auto_reconnect_reaches_the_profile_and_redacted_view() -> None:
    settings = ConnectionSettings(endpoint_url="opc.tcp://x:4840", auto_reconnect=False)
    assert settings.redacted()["auto_reconnect"] is False
    assert settings.to_profile("Manual").auto_reconnect is False
    assert ConnectionSettings(endpoint_url="opc.tcp://x:4840").to_profile("Default").auto_reconnect is True


def test_settings_tolerates_missing_and_broken_file(tmp_path) -> None:
    assert AppSettings.load(tmp_path / "missing.json").last_endpoint.startswith("opc.tcp://")
    broken = tmp_path / "settings.json"
    broken.write_text("nonsense", encoding="utf-8")
    assert AppSettings.load(broken).profiles == []


def test_assert_no_secrets_detects_nested_keys() -> None:
    assert_no_secrets({"a": [{"username": "x"}]}, context="ok")
    with pytest.raises(ValueError) as excinfo:
        assert_no_secrets({"a": [{"password": "hunter2"}]}, context="test")
    assert "password" in str(excinfo.value)


def test_profile_rejects_secret_keys_on_load() -> None:
    # from_dict filters unknown keys, so even a tampered file cannot inject one.
    profile = ConnectionProfile.from_dict(
        {"name": "x", "endpoint_url": "opc.tcp://x", "password": "leaked"}
    )
    assert not hasattr(profile, "password")
    assert "leaked" not in json.dumps(profile.to_dict())


def test_connection_settings_repr_hides_secrets() -> None:
    settings = ConnectionSettings(
        endpoint_url="opc.tcp://x:4840",
        password="hunter2",
        private_key_password="keystore-secret",
    )
    assert "hunter2" not in repr(settings)
    assert "keystore-secret" not in repr(settings)
    redacted = settings.redacted()
    assert redacted["has_password"] is True
    assert "hunter2" not in json.dumps(redacted)


def test_profile_never_carries_the_password() -> None:
    settings = ConnectionSettings(
        endpoint_url="opc.tcp://x:4840",
        auth_mode="username",
        username="operator",
        password="hunter2",
    )
    profile = settings.to_profile("Plant")
    payload = json.dumps(profile.to_dict())
    assert "hunter2" not in payload
    assert profile.username == "operator"


def test_validate_requires_certificate_for_secure_mode() -> None:
    settings = ConnectionSettings(
        endpoint_url="opc.tcp://x:4840",
        security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
        security_mode="SignAndEncrypt",
    )
    problems = settings.validate()
    assert any("client certificate" in problem for problem in problems)
    assert any("approved" in problem for problem in problems)

    discovery_problems = settings.validate(require_server_certificate=False)
    assert not any("approved" in problem for problem in discovery_problems)


def test_validate_rejects_inconsistent_security_combinations() -> None:
    assert any(
        "requires MessageSecurityMode None" in problem
        for problem in ConnectionSettings(endpoint_url="opc.tcp://x", security_mode="Sign").validate()
    )
    assert any(
        "cannot be combined" in problem
        for problem in ConnectionSettings(
            endpoint_url="opc.tcp://x",
            security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
            security_mode="None",
        ).validate()
    )


def test_validate_identity_rules() -> None:
    username = ConnectionSettings(endpoint_url="opc.tcp://x", auth_mode="username")
    assert any("user name" in problem for problem in username.validate())

    certificate = ConnectionSettings(endpoint_url="opc.tcp://x", auth_mode="certificate")
    assert any("certificate authentication" in problem.lower() for problem in certificate.validate())


def test_validate_timeouts_and_url() -> None:
    problems = ConnectionSettings(endpoint_url="http://x", connect_timeout=0).validate()
    assert any("Endpoint URL" in problem for problem in problems)
    assert any("Timeouts" in problem for problem in problems)


def test_secure_flag() -> None:
    assert ConnectionSettings(endpoint_url="opc.tcp://x").is_secure is False
    assert (
        ConnectionSettings(
            endpoint_url="opc.tcp://x",
            security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
            security_mode="Sign",
        ).is_secure
        is True
    )
    assert ConnectionSettings(endpoint_url="opc.tcp://x", security_policy_uri=NONE_POLICY_URI).is_secure is False


def test_profile_to_settings_maps_every_runtime_field() -> None:
    profile = ConnectionProfile(
        name="Plant",
        endpoint_url="opc.tcp://plant:4840",
        security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
        security_mode="SignAndEncrypt",
        auth_mode="certificate",
        username="operator",
        client_certificate_path="/certs/client.der",
        client_private_key_path="/certs/client.pem",
        connect_timeout=12.0,
        request_timeout=7.0,
        session_timeout_ms=300_000,
        secure_channel_timeout_ms=1_800_000,
        auto_reconnect=False,
    )

    settings = profile.to_settings()

    assert settings.endpoint_url == profile.endpoint_url
    assert settings.security_policy_uri == profile.security_policy_uri
    assert settings.security_mode == profile.security_mode
    assert settings.auth_mode == profile.auth_mode
    assert settings.username == profile.username
    assert settings.client_certificate_path == profile.client_certificate_path
    assert settings.client_private_key_path == profile.client_private_key_path
    assert settings.connect_timeout == profile.connect_timeout
    assert settings.request_timeout == profile.request_timeout
    assert settings.session_timeout_ms == profile.session_timeout_ms
    assert settings.secure_channel_timeout_ms == profile.secure_channel_timeout_ms
    assert settings.auto_reconnect == profile.auto_reconnect
    assert settings.use_certificate_identity is True
    # A profile never carries secrets or a trust decision.
    assert settings.password is None
    assert settings.private_key_password is None
    assert settings.server_certificate_der is None
