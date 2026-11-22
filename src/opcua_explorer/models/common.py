"""Shared helpers and session-level records used by every model cluster.

The model package is deliberately transport-agnostic: nothing here imports
asyncua or Qt. Values are stored in a JSON-friendly form (see
``opcua.serialize``) so a snapshot can be exported or inspected by the GUI
without touching a session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def iso_utc(moment: datetime | None) -> str | None:
    """Format a datetime as ISO-8601 with a trailing ``Z``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class NamespaceEntry:
    """One row of the server namespace table.

    Namespace *indexes* are session-local and can change between connections,
    therefore every export carries the URI table next to the NodeIds.
    """

    index: int
    uri: str

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "uri": self.uri}


@dataclass(slots=True)
class ConnectionInfo:
    """Diagnostics for an established session."""

    endpoint_url: str
    security_policy_uri: str
    security_policy_label: str
    security_mode: str
    auth_mode: str
    namespaces: list[NamespaceEntry] = field(default_factory=list)
    application_uri: str | None = None
    session_timeout_ms: int | None = None
    server_application_name: str | None = None
    server_application_uri: str | None = None
    product_uri: str | None = None
    manufacturer_name: str | None = None
    software_version: str | None = None
    build_number: str | None = None
    server_certificate_sha256: str | None = None
    connected_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_url": self.endpoint_url,
            "security_policy_uri": self.security_policy_uri,
            "security_policy_label": self.security_policy_label,
            "security_mode": self.security_mode,
            "authentication": self.auth_mode,
            "application_uri": self.application_uri,
            "session_timeout_ms": self.session_timeout_ms,
            "server_application_name": self.server_application_name,
            "server_application_uri": self.server_application_uri,
            "product_uri": self.product_uri,
            "manufacturer_name": self.manufacturer_name,
            "software_version": self.software_version,
            "build_number": self.build_number,
            "server_certificate_sha256": self.server_certificate_sha256,
            "namespaces": [entry.to_dict() for entry in self.namespaces],
            "connected_at_utc": iso_utc(self.connected_at),
        }


# ---------------------------------------------------------------------------
# Shared result vocabulary
#
# Functional checks and operator sequences report into the same GUI and report
# renderers, so the status names live together here.
# ---------------------------------------------------------------------------

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

#: Outcomes that only exist for operator-written sequences: a step that was cancelled after
#: it was sent may have taken effect, so it is never reported as "failed".
STATUS_CANCELLED = "cancelled"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"
