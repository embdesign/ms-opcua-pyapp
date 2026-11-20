"""Endpoint discovery and server-directory records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .common import iso_utc, utc_now


@dataclass(slots=True)
class EndpointInfo:
    """A server-advertised endpoint plus a locally computed certificate summary."""

    endpoint_url: str
    security_policy_uri: str
    security_mode: str
    security_policy_label: str
    security_level: int = 0
    token_types: list[str] = field(default_factory=list)
    certificate_sha256: str | None = None
    certificate_subject: str | None = None
    certificate_not_after: str | None = None
    certificate_der: bytes | None = None
    transport_profile_uri: str | None = None

    @property
    def is_secure(self) -> bool:
        return self.security_mode not in ("None", "Invalid") and not self.security_policy_uri.endswith("#None")

    def label(self) -> str:
        parts = [self.security_policy_label or self.security_policy_uri, self.security_mode]
        return " / ".join(parts)

    def to_dict(self, include_certificate: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "endpoint_url": self.endpoint_url,
            "security_policy_uri": self.security_policy_uri,
            "security_policy_label": self.security_policy_label,
            "security_mode": self.security_mode,
            "security_level": self.security_level,
            "token_types": list(self.token_types),
            "server_certificate_sha256": self.certificate_sha256,
            "server_certificate_subject": self.certificate_subject,
            "server_certificate_not_after": self.certificate_not_after,
        }
        if include_certificate and self.certificate_der is not None:
            import base64

            data["server_certificate_der_base64"] = base64.b64encode(self.certificate_der).decode("ascii")
        return data


@dataclass(slots=True)
class DiscoveryResult:
    """Result of the unauthenticated GetEndpoints discovery step."""

    endpoint_url: str
    endpoints: list[EndpointInfo] = field(default_factory=list)
    server_application: dict[str, Any] = field(default_factory=dict)

    def find(self, policy_uri: str, security_mode: str) -> EndpointInfo | None:
        for endpoint in self.endpoints:
            if endpoint.security_policy_uri == policy_uri and endpoint.security_mode == security_mode:
                return endpoint
        return None

    def advertised_combinations(self) -> list[str]:
        return [f"{endpoint.label()}" for endpoint in self.endpoints]


@dataclass(slots=True)
class DiscoveredServer:
    """One `ApplicationDescription` returned by a FindServers directory call.

    The fields are exactly what the directory published; this client never
    invents an application name, URI or endpoint. ``discovery_urls`` are the
    server's own advertised URLs and may be empty (some servers only fill them
    when asked over the same host name).
    """

    application_uri: str
    application_name: str = ""
    application_type: str = "Unknown"
    product_uri: str | None = None
    gateway_server_uri: str | None = None
    discovery_profile_uri: str | None = None
    discovery_urls: list[str] = field(default_factory=list)

    def label(self) -> str:
        return self.application_name or self.application_uri or "(unnamed application)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_uri": self.application_uri,
            "application_name": self.application_name,
            "application_type": self.application_type,
            "product_uri": self.product_uri,
            "gateway_server_uri": self.gateway_server_uri,
            "discovery_profile_uri": self.discovery_profile_uri,
            "discovery_urls": list(self.discovery_urls),
        }


@dataclass(slots=True)
class ServerDiscoveryResult:
    """Answer to one directory (FindServers) request on a discovery endpoint."""

    discovery_url: str
    server_uris: list[str] = field(default_factory=list)
    servers: list[DiscoveredServer] = field(default_factory=list)
    duration_ms: float = 0.0
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        if not self.servers:
            if self.server_uris:
                return (
                    f"The server at {self.discovery_url} knows no application matching "
                    f"{', '.join(self.server_uris)}."
                )
            return f"The server at {self.discovery_url} returned an empty server list."
        return f"{len(self.servers)} server(s) reported by {self.discovery_url}."

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovery_url": self.discovery_url,
            "server_uris": list(self.server_uris),
            "servers": [server.to_dict() for server in self.servers],
            "duration_ms": round(self.duration_ms, 3),
            "received_at": iso_utc(self.received_at),
        }
