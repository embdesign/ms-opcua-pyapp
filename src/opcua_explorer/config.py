"""Application settings, connection settings and the server-certificate trust store.

Two different kinds of configuration exist here:

``ConnectionSettings``
    Runtime configuration of one connection attempt. It *may* carry secrets
    (password, private-key password) but those fields use ``repr=False`` and are
    never written to disk.

``AppSettings`` / ``ConnectionProfile``
    Persisted configuration. Secrets are structurally absent: there is no field
    for a password, so it cannot leak into ``settings.json`` even by mistake.

The trust store records server certificates that a human explicitly approved.
Unknown or changed certificates always require a new decision.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

APP_DIR_NAME = "opcua-explorer"
SETTINGS_VERSION = 1

NONE_POLICY_URI = "http://opcfoundation.org/UA/SecurityPolicy#None"

#: Field names that must never reach a persisted file. Used as a defensive check.
SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_key_password",
        "private_key_password",
        "token",
    }
)


def config_dir() -> Path:
    """Platform-appropriate directory for settings, logs and the trust store."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_DIR_NAME


def normalize_fingerprint(value: str) -> str:
    """Normalise a SHA-256 fingerprint to lowercase hex without separators."""
    return value.replace(":", "").replace(" ", "").strip().lower()


def _contains_secret_key(data: Any) -> list[str]:
    """Return any secret-looking keys found in a nested JSON-like structure."""
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in SECRET_FIELD_NAMES:
                found.append(str(key))
            found.extend(_contains_secret_key(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(_contains_secret_key(item))
    return found


def assert_no_secrets(data: Any, *, context: str) -> None:
    """Guard against accidentally persisting credentials.

    This is a structural safety net, not the only protection: the dataclasses
    below simply have no field for a password.
    """
    leaked = _contains_secret_key(data)
    if leaked:
        raise ValueError(f"Refusing to persist secret fields ({', '.join(sorted(set(leaked)))}) in {context}")


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrustedCertificate:
    """A server certificate a human explicitly approved."""

    sha256: str
    subject: str
    endpoint_url: str
    added_utc: str
    not_after: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrustedCertificate:
        return cls(
            sha256=normalize_fingerprint(str(data.get("sha256", ""))),
            subject=str(data.get("subject", "")),
            endpoint_url=str(data.get("endpoint_url", "")),
            added_utc=str(data.get("added_utc", "")),
            not_after=data.get("not_after"),
        )


class TrustStore:
    """Persistent allow-list of approved server-certificate fingerprints."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else (config_dir() / "trusted_server_certificates.json")
        self._entries: dict[str, TrustedCertificate] = {}
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        self._entries.clear()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            _logger.warning("Trust store %s could not be read: %s", self.path, exc)
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            _logger.warning("Trust store %s is not valid JSON (%s); starting empty", self.path, exc)
            return
        for item in data.get("entries", []):
            if not isinstance(item, dict):
                continue
            try:
                entry = TrustedCertificate.from_dict(item)
            except (TypeError, ValueError):
                continue
            if entry.sha256:
                self._entries[entry.sha256] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "note": "Server certificates explicitly approved by the operator.",
            "entries": [entry.to_dict() for entry in sorted(self._entries.values(), key=lambda e: e.added_utc)],
        }
        assert_no_secrets(payload, context=str(self.path))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- queries ----------------------------------------------------------
    @property
    def entries(self) -> list[TrustedCertificate]:
        return sorted(self._entries.values(), key=lambda entry: entry.added_utc)

    def is_trusted(self, sha256: str) -> bool:
        return normalize_fingerprint(sha256) in self._entries

    def get(self, sha256: str) -> TrustedCertificate | None:
        return self._entries.get(normalize_fingerprint(sha256))

    # -- mutation ---------------------------------------------------------
    def add(self, sha256: str, *, subject: str, endpoint_url: str, not_after: str | None = None) -> TrustedCertificate:
        entry = TrustedCertificate(
            sha256=normalize_fingerprint(sha256),
            subject=subject,
            endpoint_url=endpoint_url,
            added_utc=datetime.now(UTC).isoformat(timespec="seconds"),
            not_after=not_after,
        )
        self._entries[entry.sha256] = entry
        self.save()
        return entry

    def remove(self, sha256: str) -> bool:
        key = normalize_fingerprint(sha256)
        if key not in self._entries:
            return False
        del self._entries[key]
        self.save()
        return True


# ---------------------------------------------------------------------------
# Persisted configuration (never contains secrets)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConnectionProfile:
    """Saved connection profile. By design there is no password field."""

    name: str
    endpoint_url: str
    security_policy_uri: str = NONE_POLICY_URI
    security_mode: str = "None"
    auth_mode: str = "anonymous"
    username: str | None = None
    client_certificate_path: str | None = None
    client_private_key_path: str | None = None
    connect_timeout: float = 10.0
    request_timeout: float = 10.0
    session_timeout_ms: int = 600_000
    secure_channel_timeout_ms: int = 3_600_000
    auto_reconnect: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        assert_no_secrets(data, context="connection profile")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectionProfile:
        known = {f.name for f in fields(cls)}
        filtered = {key: value for key, value in data.items() if key in known}
        filtered.setdefault("name", "Imported profile")
        filtered.setdefault("endpoint_url", "opc.tcp://127.0.0.1:4840")
        return cls(**filtered)

    def to_settings(self) -> ConnectionSettings:
        """Runtime settings for this profile.

        The secret fields of :class:`ConnectionSettings` are left unset: a profile
        has no password or private-key password by design, and no approved server
        certificate (that decision belongs to the trust store).
        """
        return ConnectionSettings(
            endpoint_url=self.endpoint_url,
            security_policy_uri=self.security_policy_uri,
            security_mode=self.security_mode,
            auth_mode=self.auth_mode,
            username=self.username,
            client_certificate_path=self.client_certificate_path,
            client_private_key_path=self.client_private_key_path,
            use_certificate_identity=self.auth_mode == "certificate",
            connect_timeout=self.connect_timeout,
            request_timeout=self.request_timeout,
            session_timeout_ms=self.session_timeout_ms,
            secure_channel_timeout_ms=self.secure_channel_timeout_ms,
            auto_reconnect=self.auto_reconnect,
        )


@dataclass(slots=True)
class AppSettings:
    """Persisted application state."""

    last_endpoint: str = "opc.tcp://127.0.0.1:4840"
    log_level: str = "INFO"
    last_export_dir: str | None = None
    #: Last opened/saved project file, used as the dialog starting point.
    last_project_path: str | None = None
    credentials_in_profiles: bool = False  # explicit, always False for now
    #: Keep retrying when the link drops instead of releasing the session.
    auto_reconnect: bool = True
    #: Show each page's full description instead of its one-line summary.
    show_tab_descriptions: bool = False
    profiles: list[ConnectionProfile] = field(default_factory=list)

    # -- persistence ------------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls, path: Path | str | None = None) -> AppSettings:
        target = Path(path) if path is not None else cls.path()
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            _logger.warning("Settings %s could not be read: %s", target, exc)
            return cls()
        try:
            data = json.loads(raw)
        except ValueError as exc:
            _logger.warning("Settings %s is not valid JSON (%s); using defaults", target, exc)
            return cls()

        profiles: list[ConnectionProfile] = []
        for item in data.get("profiles", []):
            if isinstance(item, dict):
                try:
                    profiles.append(ConnectionProfile.from_dict(item))
                except TypeError:
                    _logger.warning("Ignoring malformed saved profile: %s", item.get("name", "?"))
        return cls(
            last_endpoint=str(data.get("last_endpoint", cls().last_endpoint)),
            log_level=str(data.get("log_level", "INFO")),
            last_export_dir=data.get("last_export_dir"),
            last_project_path=data.get("last_project_path"),
            credentials_in_profiles=False,
            auto_reconnect=bool(data.get("auto_reconnect", True)),
            show_tab_descriptions=bool(data.get("show_tab_descriptions", False)),
            profiles=profiles,
        )

    def save(self, path: Path | str | None = None) -> None:
        target = Path(path) if path is not None else self.path()
        payload = {
            "version": SETTINGS_VERSION,
            "note": "Connection profiles never contain passwords or private-key passwords.",
            "last_endpoint": self.last_endpoint,
            "log_level": self.log_level,
            "last_export_dir": self.last_export_dir,
            "last_project_path": self.last_project_path,
            "auto_reconnect": self.auto_reconnect,
            "show_tab_descriptions": self.show_tab_descriptions,
            "profiles": [profile.to_dict() for profile in self.profiles],
        }
        assert_no_secrets(payload, context=str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)


# ---------------------------------------------------------------------------
# Runtime connection settings (may hold secrets, never persisted)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConnectionSettings:
    """One connection attempt.

    ``password`` and ``private_key_password`` are deliberately ``repr=False`` so
    that logging a settings object cannot leak them.
    """

    endpoint_url: str
    security_policy_uri: str = NONE_POLICY_URI
    security_mode: str = "None"
    auth_mode: str = "anonymous"  # anonymous | username | certificate
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    client_certificate_path: str | None = None
    client_private_key_path: str | None = None
    private_key_password: str | None = field(default=None, repr=False)
    use_certificate_identity: bool = False
    connect_timeout: float = 10.0
    request_timeout: float = 10.0
    session_timeout_ms: int = 600_000
    secure_channel_timeout_ms: int = 3_600_000
    application_uri: str | None = None
    #: Keep retrying when the link drops instead of releasing the dead session.
    auto_reconnect: bool = True
    #: DER bytes of the server certificate the operator approved (secure modes only)
    server_certificate_der: bytes | None = field(default=None, repr=False)

    @property
    def is_secure(self) -> bool:
        return self.security_mode not in ("None", "Invalid") and self.security_policy_uri != NONE_POLICY_URI

    def validate(self, *, require_server_certificate: bool = True) -> list[str]:
        """Return a list of human-readable configuration problems.

        ``require_server_certificate=False`` is used before endpoint discovery,
        when the certificate to approve is not known yet.
        """
        problems: list[str] = []
        if not self.endpoint_url.lower().startswith(("opc.tcp://", "opc.https://", "opc.wss://", "https://")):
            problems.append("Endpoint URL must start with opc.tcp:// (or another OPC UA scheme).")
        if self.security_mode == "Invalid":
            problems.append("Invalid message security mode.")
        if self.security_policy_uri == NONE_POLICY_URI and self.security_mode not in ("None", "Invalid"):
            problems.append("SecurityPolicy None requires MessageSecurityMode None.")
        if self.security_policy_uri != NONE_POLICY_URI and self.security_mode == "None":
            problems.append("A secure security policy cannot be combined with MessageSecurityMode None.")
        if self.is_secure:
            if not self.client_certificate_path:
                problems.append("A secure connection requires a client certificate (.der or .pem).")
            if not self.client_private_key_path:
                problems.append("A secure connection requires a client private key (.pem).")
            if require_server_certificate and self.server_certificate_der is None:
                problems.append("The server certificate must be reviewed and approved before connecting.")
        if self.auth_mode == "username" and not self.username:
            problems.append("Username authentication requires a user name.")
        if self.auth_mode == "certificate" and (
            not self.client_certificate_path or not self.client_private_key_path
        ):
            problems.append("Certificate authentication requires a client certificate and private key.")
        if self.connect_timeout <= 0 or self.request_timeout <= 0:
            problems.append("Timeouts must be greater than zero.")
        return problems

    def redacted(self) -> dict[str, Any]:
        """Log-safe description of this connection attempt."""
        data = {
            "endpoint_url": self.endpoint_url,
            "security_policy_uri": self.security_policy_uri,
            "security_mode": self.security_mode,
            "auth_mode": self.auth_mode,
            "username": self.username,
            "use_certificate_identity": self.use_certificate_identity,
            "connect_timeout": self.connect_timeout,
            "request_timeout": self.request_timeout,
            "session_timeout_ms": self.session_timeout_ms,
            "auto_reconnect": self.auto_reconnect,
            "has_password": bool(self.password),
            "has_private_key_password": bool(self.private_key_password),
            "server_certificate_present": self.server_certificate_der is not None,
        }
        assert_no_secrets(data, context="redacted connection settings")
        return data

    def to_profile(self, name: str) -> ConnectionProfile:
        """Create a persistable profile; credentials are intentionally dropped."""
        return ConnectionProfile(
            name=name,
            endpoint_url=self.endpoint_url,
            security_policy_uri=self.security_policy_uri,
            security_mode=self.security_mode,
            auth_mode=self.auth_mode,
            username=self.username,
            client_certificate_path=self.client_certificate_path,
            client_private_key_path=self.client_private_key_path,
            connect_timeout=self.connect_timeout,
            request_timeout=self.request_timeout,
            session_timeout_ms=self.session_timeout_ms,
            secure_channel_timeout_ms=self.secure_channel_timeout_ms,
            auto_reconnect=self.auto_reconnect,
        )
