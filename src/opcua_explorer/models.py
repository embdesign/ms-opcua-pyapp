"""In-memory data model shared by the OPC UA service, exporters and GUI.

The model is deliberately transport-agnostic: nothing here imports asyncua or
Qt. Values are stored in a JSON-friendly form (see ``opcua.serialize``) so a
snapshot can be exported or inspected by the GUI without touching a session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .defaults import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_EVENT_PUBLISHING_INTERVAL_MS,
    DEFAULT_HISTORY_MAX_VALUES,
    DEFAULT_LATENCY_SAMPLES,
    DEFAULT_MAX_NODES,
    DEFAULT_NOTIFICATION_WAIT_SECONDS,
    DEFAULT_PUBLISHING_INTERVAL_MS,
    DEFAULT_READ_MAX_AGE_MS,
    DEFAULT_SAMPLING_INTERVAL_MS,
    MAX_HISTORY_MAX_VALUES,
    MAX_READ_MAX_AGE_MS,
)


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


@dataclass(slots=True)
class ValueSnapshot:
    """A variable value with its status and timestamps.

    ``encoding`` is ``"json"`` for successfully serialized values,
    ``"unavailable"`` when the server returned a bad StatusCode, and
    ``"unsupported"`` when parts of the value could not be represented
    faithfully (details are kept in ``unsupported``).
    """

    value: Any
    status: str = "Good"
    variant_type: str | None = None
    source_timestamp: str | None = None
    server_timestamp: str | None = None
    encoding: str = "json"
    unsupported: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "variant_type": self.variant_type,
            "status": self.status,
            "source_timestamp": self.source_timestamp,
            "server_timestamp": self.server_timestamp,
            "encoding": self.encoding,
            "unsupported_encodings": list(self.unsupported),
        }

    @property
    def unsupported_encoding(self) -> bool:
        return bool(self.unsupported)


#: ``TimestampsToReturn`` names this client offers; the server may still return
#: fewer timestamps than asked for.
TIMESTAMPS_TO_RETURN = ("Source", "Server", "Both", "Neither")

#: The only non-Value attributes this client writes: their text is metadata, not
#: process data, and the server still decides whether the write is allowed.
WRITABLE_TEXT_ATTRIBUTES = ("DisplayName", "Description")


@dataclass(slots=True)
class ValueReadOptions:
    """Options of one explicit Value read: a slice, a cache age and timestamps.

    ``index_range`` is a NumericRange string (``"1:3"``, ``"0,2"``) applied to the
    Value attribute; empty means the whole value. ``max_age_ms`` is the protocol
    ``MaxAge``: ``0`` lets the server answer from a cache, the maximum asks for a
    value from the device. ``timestamps`` selects ``TimestampsToReturn``.
    """

    index_range: str = ""
    max_age_ms: int = DEFAULT_READ_MAX_AGE_MS
    timestamps: str = "Both"

    def normalised(self) -> ValueReadOptions:
        name = self.timestamps if self.timestamps in TIMESTAMPS_TO_RETURN else "Both"
        return ValueReadOptions(
            index_range=self.index_range.strip(),
            max_age_ms=max(0, min(int(self.max_age_ms), MAX_READ_MAX_AGE_MS)),
            timestamps=name,
        )

    def label(self) -> str:
        parts = [f"timestamps {self.timestamps}", f"MaxAge {self.max_age_ms} ms"]
        if self.index_range:
            parts.insert(0, f"IndexRange {self.index_range}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_range": self.index_range,
            "max_age_ms": self.max_age_ms,
            "timestamps": self.timestamps,
        }


@dataclass(slots=True)
class ValueReadResult:
    """One explicit Value read with its options and the decoded snapshot."""

    node_id: str
    options: ValueReadOptions
    snapshot: ValueSnapshot
    duration_ms: float = 0.0
    detail: str = ""
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        head = f"Value of {self.node_id} [{self.snapshot.status}]"
        return f"{head} — {self.detail}" if self.detail else head

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "options": self.options.to_dict(),
            "value": self.snapshot.to_dict(),
            "duration_ms": round(self.duration_ms, 3),
            "detail": self.detail,
            "received_at": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class NodeRecord:
    """Canonical representation of one address-space node."""

    node_id: str
    node_class: str = "Unspecified"
    browse_name: str = ""
    browse_name_namespace_index: int = 0
    display_name: str = ""
    namespace_uri: str | None = None
    description: str | None = None
    data_type: str | None = None
    data_type_name: str | None = None
    value_rank: int | None = None
    array_dimensions: list[int] | None = None
    access_level: int | None = None
    access_level_names: list[str] = field(default_factory=list)
    user_access_level: int | None = None
    user_access_level_names: list[str] = field(default_factory=list)
    type_definition: str | None = None
    type_definition_name: str | None = None
    value: ValueSnapshot | None = None
    #: attribute name -> OPC UA StatusCode name for attributes the server refused
    unavailable: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_class": self.node_class,
            "browse_name": self.browse_name,
            "browse_name_namespace_index": self.browse_name_namespace_index,
            "display_name": self.display_name,
            "namespace_uri": self.namespace_uri,
            "description": self.description,
            "data_type": self.data_type,
            "data_type_name": self.data_type_name,
            "value_rank": self.value_rank,
            "array_dimensions": self.array_dimensions,
            "access_level": self.access_level,
            "access_level_names": list(self.access_level_names),
            "user_access_level": self.user_access_level,
            "user_access_level_names": list(self.user_access_level_names),
            "type_definition": self.type_definition,
            "type_definition_name": self.type_definition_name,
            "value": None if self.value is None else self.value.to_dict(),
            "unavailable_attributes": dict(self.unavailable),
            "errors": list(self.errors),
        }

    @property
    def has_value(self) -> bool:
        return self.value is not None

    @property
    def is_writable(self) -> bool:
        return "CurrentWrite" in self.access_level_names


@dataclass(slots=True)
class Edge:
    """A directed reference, normalised to its forward direction.

    An inverse reference ``A --(InverseOf)--> B`` observed while browsing ``A``
    is stored as the forward edge ``B -> A`` so the same relationship discovered
    from both ends collapses into one row. ``reference_type_name`` is resolved
    after the traversal completes, which is why this dataclass is not frozen.
    """

    source: str
    target: str
    reference_type: str
    reference_type_name: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.reference_type)


@dataclass(slots=True)
class ChildEntry:
    """One child as shown by the lazy GUI tree."""

    node_id: str
    browse_name: str
    browse_name_namespace_index: int
    display_name: str
    node_class: str
    reference_type: str
    reference_type_name: str | None = None
    is_forward: bool = True
    type_definition: str | None = None
    type_definition_name: str | None = None
    #: Extra reference types that pointed at the same node and were merged into
    #: this row, so the tree can explain why the server reported it more than
    #: once (for example ``Organizes`` next to ``HasComponent``).
    additional_reference_types: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReferenceInfo:
    """One reference row of the node details panel."""

    reference_type: str
    reference_type_name: str | None
    direction: str
    target_node_id: str
    target_browse_name: str
    target_display_name: str
    target_node_class: str


@dataclass(slots=True)
class AttributeResult:
    """One attribute of one node, already decoded for display.

    ``value`` stays native (NodeId, QualifiedName, int, bool, ...) so a caller can
    compare it; ``display`` optionally carries a human-readable rendering such as
    the decoded names of a bit field (``"1 (SubscribeToEvents)"``).
    """

    attribute: str
    value: Any = None
    status: str = "Good"
    ok: bool = True
    display: str | None = None


@dataclass(slots=True)
class NodeDetails:
    """Everything the browser shows for a selected node."""

    record: NodeRecord
    attributes: list[AttributeResult] = field(default_factory=list)
    references: list[ReferenceInfo] = field(default_factory=list)


@dataclass(slots=True)
class TraversalConfig:
    """Bounds for an address-space download.

    All limits are optional; ``None`` means "no limit". Every limit is reported
    back in the export summary when it stopped the traversal.
    """

    max_depth: int | None = None
    max_nodes: int | None = DEFAULT_MAX_NODES
    concurrency: int = DEFAULT_CONCURRENCY
    batch_size: int = DEFAULT_BATCH_SIZE
    max_references_per_node: int = 0
    namespace_filter: frozenset[int] | None = None
    include_values: bool = False
    include_description: bool = True
    follow_subtypes: bool = True

    def normalised(self) -> TraversalConfig:
        return TraversalConfig(
            max_depth=None if self.max_depth is None or self.max_depth < 0 else self.max_depth,
            max_nodes=None if self.max_nodes is None or self.max_nodes <= 0 else self.max_nodes,
            concurrency=max(1, min(int(self.concurrency), 32)),
            batch_size=max(1, min(int(self.batch_size), 500)),
            max_references_per_node=max(0, int(self.max_references_per_node)),
            namespace_filter=self.namespace_filter,
            include_values=bool(self.include_values),
            include_description=bool(self.include_description),
            follow_subtypes=bool(self.follow_subtypes),
        )


@dataclass(slots=True)
class TraversalProgress:
    """Progress notification emitted from the asyncio thread."""

    phase: str
    message: str = ""
    nodes_collected: int = 0
    nodes_browsed: int = 0
    nodes_pending: int = 0
    references_found: int = 0
    depth: int = 0


# ---------------------------------------------------------------------------
# Functional test results
# ---------------------------------------------------------------------------

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

#: Shown in the GUI and in every exported report. The checks are behavioural
#: smoke checks, they are not a certification of OPC UA conformance.
FUNCTIONAL_TEST_DISCLAIMER = (
    "Functional and interoperability checks - not official OPC UA certification."
)


#: Where one write or method call was authorised. Every send records its authority, so a
#: result can never look like an unattributed write.
AUTHORITY_OPERATOR_DIALOG = "operator-dialog"
AUTHORITY_SEQUENCE_DOCUMENT = "sequence-document"


@dataclass(frozen=True, slots=True)
class WriteAuthority:
    """Who authorised one write or method call, and what that authority pointed at.

    ``source`` is one of the ``AUTHORITY_*`` constants; ``reference`` names the check id or the
    sequence step, so a report can be traced back to the confirmation the operator saw.
    """

    source: str
    reference: str = ""

    def describe(self) -> str:
        return f"{self.source}: {self.reference}" if self.reference else self.source

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "reference": self.reference}


@dataclass(slots=True)
class WriteTarget:
    """One explicitly requested write. There is no way to write to more than one node per entry."""

    node_id: str
    value_text: str
    #: Off by default: a previous value is only restored when the operator asks for it.
    restore_previous_value: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "value_text": self.value_text,
            "restore_previous_value": self.restore_previous_value,
            "note": self.note,
        }


@dataclass(slots=True)
class MethodCallTarget:
    """One explicitly requested method call on one object node."""

    object_node_id: str
    method_node_id: str
    #: Raw argument texts in server order; validated against ``InputArguments``.
    arguments: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_node_id": self.object_node_id,
            "method_node_id": self.method_node_id,
            "arguments": list(self.arguments),
            "note": self.note,
        }


@dataclass(slots=True)
class TestConfig:
    """Bounded configuration for one functional test run."""

    root_node_id: str = "i=85"
    variable_node_id: str | None = None
    publishing_interval_ms: float = DEFAULT_PUBLISHING_INTERVAL_MS
    sampling_interval_ms: float = DEFAULT_SAMPLING_INTERVAL_MS
    notification_wait_seconds: float = DEFAULT_NOTIFICATION_WAIT_SECONDS
    latency_samples: int = DEFAULT_LATENCY_SAMPLES
    max_browse_nodes: int = 50
    #: Opt-in and destructive: closes the secure channel and reconnects.
    include_channel_recovery: bool = False
    request_timeout: float = 10.0
    #: Master switch. Without it, configured write/method targets are skipped.
    allow_writes: bool = False
    write_targets: list[WriteTarget] = field(default_factory=list)
    method_calls: list[MethodCallTarget] = field(default_factory=list)

    def normalised(self) -> TestConfig:
        return TestConfig(
            root_node_id=(self.root_node_id or "i=85").strip(),
            variable_node_id=(self.variable_node_id or "").strip() or None,
            publishing_interval_ms=min(60_000.0, max(50.0, float(self.publishing_interval_ms))),
            sampling_interval_ms=min(60_000.0, max(0.0, float(self.sampling_interval_ms))),
            notification_wait_seconds=min(120.0, max(0.5, float(self.notification_wait_seconds))),
            latency_samples=min(200, max(1, int(self.latency_samples))),
            max_browse_nodes=min(5000, max(1, int(self.max_browse_nodes))),
            include_channel_recovery=bool(self.include_channel_recovery),
            request_timeout=min(300.0, max(0.5, float(self.request_timeout))),
            allow_writes=bool(self.allow_writes),
            write_targets=[
                WriteTarget(
                    node_id=(target.node_id or "").strip(),
                    value_text=str(target.value_text),
                    restore_previous_value=bool(target.restore_previous_value),
                    note=target.note,
                )
                for target in self.write_targets
                if (target.node_id or "").strip()
            ],
            method_calls=[
                MethodCallTarget(
                    object_node_id=(call.object_node_id or "").strip(),
                    method_node_id=(call.method_node_id or "").strip(),
                    arguments=[str(value) for value in call.arguments],
                    note=call.note,
                )
                for call in self.method_calls
                if (call.method_node_id or "").strip()
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_node_id": self.root_node_id,
            "variable_node_id": self.variable_node_id,
            "publishing_interval_ms": self.publishing_interval_ms,
            "sampling_interval_ms": self.sampling_interval_ms,
            "notification_wait_seconds": self.notification_wait_seconds,
            "latency_samples": self.latency_samples,
            "max_browse_nodes": self.max_browse_nodes,
            "include_channel_recovery": self.include_channel_recovery,
            "request_timeout": self.request_timeout,
            "allow_writes": self.allow_writes,
            "write_targets": [target.to_dict() for target in self.write_targets],
            "method_calls": [call.to_dict() for call in self.method_calls],
        }


@dataclass(slots=True)
class TestCheck:
    """Result of one functional check."""

    id: str
    name: str
    category: str
    status: str = STATUS_SKIPPED
    detail: str = ""
    started_at: datetime = field(default_factory=utc_now)
    duration_ms: float = 0.0
    status_codes: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASSED

    def add_status(self, operation: str, status: str, *, node_id: str | None = None) -> None:
        entry: dict[str, Any] = {"operation": operation, "status": status}
        if node_id:
            entry["node_id"] = node_id
        self.status_codes.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "detail": self.detail,
            "started_at_utc": iso_utc(self.started_at),
            "duration_ms": self.duration_ms,
            "status_codes": list(self.status_codes),
            "data": self.data,
            "notes": list(self.notes),
            "error": self.error,
        }


@dataclass(slots=True)
class TestProgress:
    """Progress notification for a running test suite."""

    phase: str
    message: str = ""
    current: int = 0
    total: int = 0


@dataclass(slots=True)
class TestRun:
    """Result of one functional test suite execution."""

    endpoint_url: str
    config: TestConfig = field(default_factory=TestConfig)
    connection: ConnectionInfo | None = None
    checks: list[TestCheck] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    coverage_limitations: list[str] = field(default_factory=list)
    inaccessible_nodes: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    cancelled: bool = False

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def counts(self) -> dict[str, int]:
        counts = {STATUS_PASSED: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "total": len(self.checks),
            "passed": counts.get(STATUS_PASSED, 0),
            "failed": counts.get(STATUS_FAILED, 0),
            "skipped": counts.get(STATUS_SKIPPED, 0),
            "cancelled": self.cancelled,
            "duration_seconds": None if self.duration_seconds is None else round(self.duration_seconds, 3),
            "started_at_utc": iso_utc(self.started_at),
            "finished_at_utc": iso_utc(self.finished_at),
        }

    def summary_text(self) -> str:
        summary = self.summary()
        lines = [
            FUNCTIONAL_TEST_DISCLAIMER,
            f"Endpoint:  {self.endpoint_url}",
            f"Result:    {summary['passed']} passed, {summary['failed']} failed, "
            f"{summary['skipped']} skipped (of {summary['total']})",
            f"Duration:  {summary['duration_seconds']} s",
        ]
        if self.cancelled:
            lines.append("State:     CANCELLED (partial results)")
        failed = [check for check in self.checks if check.status == STATUS_FAILED]
        for check in failed:
            lines.append(f"Failed:    {check.name} - {check.detail or check.error or ''}")
        for check in self.coverage_limitations:
            lines.append(f"Limitation: {check}")
        return "\n".join(lines)


#: Standard OPC UA attributes read only for the Address space details panel
#: (``opcua/attributes.py``), never during a download, so no snapshot export carries
#: them. Kept as text here because ``models`` must not import the asyncua-based reader.
DETAILS_ONLY_ATTRIBUTES: tuple[str, ...] = (
    "EventNotifier",
    "WriteMask",
    "UserWriteMask",
    "Historizing",
    "MinimumSamplingInterval",
    "IsAbstract",
    "Symmetric",
    "InverseName",
    "ContainsNoLoops",
    "DataTypeDefinition",
    "AccessRestrictions",
    "RolePermissions",
    "UserRolePermissions",
    "Executable",
    "UserExecutable",
)


@dataclass(slots=True)
class AddressSpace:
    """A downloaded (possibly partial) address-space snapshot."""

    endpoint_url: str
    roots: list[str] = field(default_factory=list)
    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    references: list[Edge] = field(default_factory=list)
    namespaces: list[NamespaceEntry] = field(default_factory=list)
    connection: ConnectionInfo | None = None
    values_included: bool = False
    config: TraversalConfig = field(default_factory=TraversalConfig)
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    cancelled: bool = False
    limits_hit: list[str] = field(default_factory=list)
    inaccessible: dict[str, str] = field(default_factory=dict)
    unsupported_encodings: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def namespace_uri(self, index: int) -> str | None:
        for entry in self.namespaces:
            if entry.index == index:
                return entry.uri
        return None

    def summary(self) -> dict[str, Any]:
        classes: dict[str, int] = {}
        for record in self.nodes.values():
            classes[record.node_class] = classes.get(record.node_class, 0) + 1
        included = set(self.nodes)
        dangling = sum(1 for edge in self.references if edge.target not in included)
        return {
            "nodes": len(self.nodes),
            "references": len(self.references),
            "references_to_nodes_outside_snapshot": dangling,
            "namespaces_recorded": len(self.namespaces),
            "node_classes": dict(sorted(classes.items())),
            "values_included": self.values_included,
            "cancelled": self.cancelled,
            "limits_hit": list(self.limits_hit),
            "inaccessible_nodes": len(self.inaccessible),
            "unsupported_value_encodings": len(self.unsupported_encodings),
            "errors": len(self.errors),
            "duration_seconds": None if self.duration_seconds is None else round(self.duration_seconds, 3),
            "started_at_utc": iso_utc(self.started_at),
            "finished_at_utc": iso_utc(self.finished_at),
        }

    def summary_text(self) -> str:
        """Human-readable summary used by the export tab."""
        summary = self.summary()
        classes = ", ".join(f"{name}={count}" for name, count in summary["node_classes"].items())
        lines = [
            f"Endpoint:        {self.endpoint_url}",
            f"Nodes:           {summary['nodes']}",
            f"References:      {summary['references']} "
            f"({summary['references_to_nodes_outside_snapshot']} point outside the snapshot)",
            f"Node classes:    {classes or '-'}",
            f"Values included: {'yes' if self.values_included else 'no'}",
            f"Duration:        {summary['duration_seconds']} s",
        ]
        if self.cancelled:
            lines.append("State:           CANCELLED (partial snapshot)")
        if self.limits_hit:
            lines.append(f"Limits hit:      {', '.join(self.limits_hit)}")
        if self.inaccessible:
            lines.append(f"Inaccessible:    {len(self.inaccessible)} node(s)")
        if self.unsupported_encodings:
            lines.append(f"Unsupported:     {len(self.unsupported_encodings)} value encoding(s)")
        if self.errors:
            lines.append(f"Errors:          {len(self.errors)}")
            for message in self.errors[:5]:
                lines.append(f"  - {message}")
        for note in self.limitations:
            lines.append(f"Note:            {note}")
        return "\n".join(lines)

    def coverage_text(self) -> str:
        """What this snapshot contains and what no snapshot export can contain.

        The snapshot is the authoritative record: ``config`` holds the options the
        *download* used, which may differ from the options the Export tab shows now.
        """
        included = ["base attributes and references"]
        excluded: list[str] = []
        if self.config.include_values:
            included.append("variable values (Value, SourceTimestamp, ServerTimestamp)")
        else:
            excluded.append("variable values (Value, SourceTimestamp, ServerTimestamp)")
        if self.config.include_description:
            included.append("node descriptions")
        else:
            excluded.append("node descriptions")

        lines = [
            f"Nodes:          {len(self.nodes)}",
            f"References:     {len(self.references)}",
            f"Included:       {', '.join(included)}",
        ]
        if excluded:
            lines.append(f"NOT included:   {', '.join(excluded)}")
        lines.append(
            "Never in a snapshot (details panel only): " + ", ".join(DETAILS_ONLY_ATTRIBUTES) + "."
        )
        if self.cancelled:
            lines.append("State:          CANCELLED (partial snapshot)")
        if self.limits_hit:
            lines.append(f"Limits hit:     {', '.join(self.limits_hit)} (partial snapshot)")
        if self.inaccessible:
            lines.append(f"Inaccessible:   {len(self.inaccessible)} node(s) could not be browsed")
        refused = sum(len(record.unavailable) for record in self.nodes.values())
        if refused:
            lines.append(f"Refused attrs:  {refused} attribute read(s) returned a bad StatusCode")
        if self.unsupported_encodings:
            lines.append(
                f"Unsupported:    {len(self.unsupported_encodings)} value encoding(s) flagged as approximate"
            )
        if self.errors:
            lines.append(f"Errors:         {len(self.errors)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live monitoring (data-change subscriptions)
# ---------------------------------------------------------------------------

#: A notification value longer than this is truncated before it is sent to the
#: GUI, so a burst of large arrays cannot exhaust memory.
MONITOR_VALUE_CHAR_LIMIT = 4000

#: Data change triggers the client can request (``DataChangeTrigger`` values).
DATA_CHANGE_TRIGGERS = ("Status", "StatusValue", "StatusValueTimestamp")
#: Deadband types the client can request (``DeadbandType`` values).
DEADBAND_TYPES = ("None", "Absolute", "Percent")


@dataclass(slots=True)
class MonitorConfig:
    """Timing and filter for one live-monitoring subscription.

    The server may revise both intervals; the values here are the requested
    ones. Sampling interval ``0`` means "fastest the server supports".

    ``trigger`` and ``deadband_type`` map to the OPC UA ``DataChangeFilter``.
    The default pair (``StatusValue`` / ``None``) is exactly the server's
    default behaviour, so no filter is sent unless the operator changes one.
    """

    publishing_interval_ms: float = DEFAULT_PUBLISHING_INTERVAL_MS
    sampling_interval_ms: float = DEFAULT_SAMPLING_INTERVAL_MS
    #: Server-side queue size per monitored item; ``0`` uses the server default.
    queue_size: int = 0
    trigger: str = "StatusValue"
    deadband_type: str = "None"
    deadband_value: float = 0.0

    def normalised(self) -> MonitorConfig:
        trigger = self.trigger if self.trigger in DATA_CHANGE_TRIGGERS else "StatusValue"
        deadband_type = self.deadband_type if self.deadband_type in DEADBAND_TYPES else "None"
        deadband_value = max(0.0, float(self.deadband_value))
        if deadband_type == "Percent":
            deadband_value = min(100.0, deadband_value)
        return MonitorConfig(
            publishing_interval_ms=min(60_000.0, max(50.0, float(self.publishing_interval_ms))),
            sampling_interval_ms=min(60_000.0, max(0.0, float(self.sampling_interval_ms))),
            queue_size=min(1000, max(0, int(self.queue_size))),
            trigger=trigger,
            deadband_type=deadband_type,
            deadband_value=deadband_value,
        )

    @property
    def needs_filter(self) -> bool:
        """True when the requested trigger/deadband differs from the UA default."""
        config = self.normalised()
        return config.trigger != "StatusValue" or config.deadband_type != "None"

    def to_dict(self) -> dict[str, Any]:
        return {
            "publishing_interval_ms": self.publishing_interval_ms,
            "sampling_interval_ms": self.sampling_interval_ms,
            "queue_size": self.queue_size,
            "trigger": self.trigger,
            "deadband_type": self.deadband_type,
            "deadband_value": self.deadband_value,
        }


@dataclass(slots=True)
class MonitoredItemInfo:
    """One requested monitored item and what the server made of it."""

    node_id: str
    status: str = "pending"
    ok: bool = True
    client_handle: int | None = None
    sampling_interval_ms: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "ok": self.ok,
            "client_handle": self.client_handle,
            "sampling_interval_ms": self.sampling_interval_ms,
            "detail": self.detail,
        }


@dataclass(slots=True)
class MonitorNotification:
    """One data-change notification, already JSON-safe for display."""

    sequence: int
    node_id: str
    value: Any = None
    value_text: str = ""
    status: str = "Good"
    variant_type: str | None = None
    source_timestamp: str | None = None
    server_timestamp: str | None = None
    received_at: datetime = field(default_factory=utc_now)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "node_id": self.node_id,
            "value": self.value,
            "status": self.status,
            "variant_type": self.variant_type,
            "source_timestamp": self.source_timestamp,
            "server_timestamp": self.server_timestamp,
            "received_at_utc": iso_utc(self.received_at),
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class MonitorStatusEvent:
    """A subscription status-change notification from the server."""

    status: str
    detail: str = ""
    received_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class MonitorStart:
    """Result of creating a live-monitoring subscription."""

    subscription_id: int | None
    requested_publishing_interval_ms: float
    sampling_interval_ms: float
    items: list[MonitoredItemInfo] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        accepted = sum(1 for item in self.items if item.ok)
        return (
            f"Subscription {self.subscription_id}: {accepted} of {len(self.items)} monitored item(s) active; "
            f"requested publishing interval {self.requested_publishing_interval_ms:.0f} ms, "
            f"sampling interval {self.sampling_interval_ms:.0f} ms."
        )


# ---------------------------------------------------------------------------
# Event subscriptions. Condition events additionally carry the identity needed
# for one guarded Acknowledge call; this data alone never writes anything.
# ---------------------------------------------------------------------------

#: One event field longer than this is shortened before it reaches the GUI or a
#: log; event payloads are server-defined and may contain large structures.
EVENT_FIELD_CHAR_LIMIT = 2000


@dataclass(slots=True)
class EventConfig:
    """Timing and scope of one event subscription.

    ``event_type_node_id`` selects the event type whose fields are requested.
    Empty means the standard ``BaseEventType``: every event the source emits,
    with the standard fields.
    """

    publishing_interval_ms: float = DEFAULT_EVENT_PUBLISHING_INTERVAL_MS
    #: Server-side queue size per monitored item; ``0`` uses the server default.
    queue_size: int = 0
    event_type_node_id: str = ""

    def normalised(self) -> EventConfig:
        return EventConfig(
            publishing_interval_ms=min(60_000.0, max(100.0, float(self.publishing_interval_ms))),
            queue_size=min(1000, max(0, int(self.queue_size))),
            event_type_node_id=str(self.event_type_node_id or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "publishing_interval_ms": self.publishing_interval_ms,
            "queue_size": self.queue_size,
            "event_type_node_id": self.event_type_node_id,
        }


@dataclass(slots=True)
class EventSourceInfo:
    """One node whose events were requested, and what the server made of it."""

    node_id: str
    status: str = "pending"
    ok: bool = True
    client_handle: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "ok": self.ok,
            "client_handle": self.client_handle,
            "detail": self.detail,
        }


@dataclass(slots=True)
class EventNotification:
    """One event notification, already reduced to display-safe text.

    ``fields`` keeps every field the subscription selected, keyed by the name
    the server's event type gave it, so custom event types stay inspectable.
    """

    sequence: int
    event_type: str = ""
    event_type_id: str = ""
    source_node: str = ""
    source_name: str = ""
    severity: int | None = None
    message: str = ""
    time: str | None = None
    receive_time: str | None = None
    event_id: str = ""
    #: The condition node when the subscription selected condition fields; empty otherwise.
    condition_node_id: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    received_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "event_type_id": self.event_type_id,
            "source_node": self.source_node,
            "source_name": self.source_name,
            "severity": self.severity,
            "message": self.message,
            "time": self.time,
            "receive_time": self.receive_time,
            "event_id": self.event_id,
            "condition_node_id": self.condition_node_id,
            "fields": dict(self.fields),
            "received_at_utc": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class EventStart:
    """Result of creating an event subscription."""

    subscription_id: int | None
    requested_publishing_interval_ms: float
    sources: list[EventSourceInfo] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        accepted = sum(1 for source in self.sources if source.ok)
        return (
            f"Subscription {self.subscription_id}: {accepted} of {len(self.sources)} event source(s) active; "
            f"requested publishing interval {self.requested_publishing_interval_ms:.0f} ms."
        )


# ---------------------------------------------------------------------------
# Server status and diagnostics (read-only snapshot)
# ---------------------------------------------------------------------------


    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "current_time": self.current_time,
            "start_time": self.start_time,
            "uptime_seconds": self.uptime_seconds,
            "seconds_till_shutdown": self.seconds_till_shutdown,
            "shutdown_reason": self.shutdown_reason,
            "diagnostics_enabled": self.diagnostics_enabled,
            "counters": dict(self.counters),
            "limits": dict(self.limits),
            "not_provided": list(self.not_provided),
            "unavailable": dict(self.unavailable),
            "monitor_subscription_id": self.monitor_subscription_id,
            "monitored_items": [item.to_dict() for item in self.monitored_items],
            "received_at_utc": iso_utc(self.received_at),
        }


# ---------------------------------------------------------------------------
# Raw history reads
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HistoryConfig:
    """Bounds for one raw ``HistoryRead`` call.

    ``start``/``end`` of ``None`` mean an open range; ``max_values`` is a hard
    client-side cap that also becomes ``NumValuesPerNode``.
    """

    start: datetime | None = None
    end: datetime | None = None
    max_values: int = DEFAULT_HISTORY_MAX_VALUES

    def normalised(self) -> HistoryConfig:
        return HistoryConfig(
            start=self.start,
            end=self.end,
            max_values=min(MAX_HISTORY_MAX_VALUES, max(1, int(self.max_values))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": iso_utc(self.start),
            "end": iso_utc(self.end),
            "max_values": self.max_values,
        }


@dataclass(slots=True)
class HistoryResult:
    """Raw history of one variable, oldest value first.

    ``status`` is the HistoryRead StatusCode; ``historizing`` is the node's
    Historizing attribute when it could be read. A Good StatusCode with no
    values is not an error - many servers simply have no data in the range.
    """

    node_id: str
    values: list[ValueSnapshot] = field(default_factory=list)
    status: str = "Good"
    historizing: bool | None = None
    truncated: bool = False
    requested_max_values: int = 0
    pages: int = 0
    detail: str = ""
    duration_ms: float = 0.0
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        parts = [f"History for {self.node_id}: {len(self.values)} value(s)"]
        if self.status != "Good":
            parts.append(f"HistoryRead {self.status}")
        if self.historizing is False:
            parts.append("Historizing = false")
        if self.truncated:
            parts.append("truncated at the requested maximum")
        if self.detail:
            parts.append(self.detail)
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "historizing": self.historizing,
            "truncated": self.truncated,
            "requested_max_values": self.requested_max_values,
            "pages": self.pages,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "values": [snapshot.to_dict() for snapshot in self.values],
            "received_at_utc": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class ServerStatusSnapshot:
    """One read-only look at ServerStatus, ServerDiagnostics and this session.

    Optional standard variables that a server reports as Good-but-null are in
    ``not_provided``; values with a Bad StatusCode are in ``unavailable``. The
    monitored items are the client's own view of its live-monitor subscription
    (``GetMonitoredItems`` is not called).
    """

    state: str = "Unknown"
    current_time: str | None = None
    start_time: str | None = None
    uptime_seconds: float | None = None
    seconds_till_shutdown: int | None = None
    shutdown_reason: str | None = None
    diagnostics_enabled: bool | None = None
    counters: dict[str, int] = field(default_factory=dict)
    limits: dict[str, int | float] = field(default_factory=dict)
    not_provided: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)
    monitor_subscription_id: int | None = None
    monitored_items: list[MonitoredItemInfo] = field(default_factory=list)
    received_at: datetime = field(default_factory=utc_now)

    @property
    def has_monitor(self) -> bool:
        return self.monitor_subscription_id is not None

    def summary_text(self) -> str:
        parts = [f"Server {self.state}"]
        if self.uptime_seconds is not None:
            parts.append(f"uptime {self.uptime_seconds:.0f} s")
        parts.append(f"{len(self.counters)} diagnostic value(s)")
        if self.not_provided:
            parts.append(f"{len(self.not_provided)} not provided")
        if self.unavailable:
            parts.append(f"{len(self.unavailable)} unavailable")
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "current_time": self.current_time,
            "start_time": self.start_time,
            "uptime_seconds": self.uptime_seconds,
            "seconds_till_shutdown": self.seconds_till_shutdown,
            "shutdown_reason": self.shutdown_reason,
            "diagnostics_enabled": self.diagnostics_enabled,
            "counters": dict(self.counters),
            "limits": dict(self.limits),
            "not_provided": list(self.not_provided),
            "unavailable": dict(self.unavailable),
            "monitor_subscription_id": self.monitor_subscription_id,
            "monitored_items": [item.to_dict() for item in self.monitored_items],
            "received_at_utc": iso_utc(self.received_at),
        }


# ---------------------------------------------------------------------------------------
# Universal sequences (device control)
#
# A sequence is an operator-written document: preconditions, an optional gate (user-defined
# acquire / keepalive / release steps) and a body. Nothing in the model names a device, a
# vendor or a lock protocol - see ``opcua/sequence_runner.py`` for the mechanical rules.
# ---------------------------------------------------------------------------------------

#: Identifier and version of the sequence document written to and read from disk.
SEQUENCE_FORMAT = "opcua-explorer-sequence"
SEQUENCE_VERSION = 1

#: Outcomes that only exist for operator-written sequences: a step that was cancelled after
#: it was sent may have taken effect, so it is never reported as "failed".
STATUS_CANCELLED = "cancelled"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"

STEP_WRITE = "write"
STEP_EXPECT = "expect"
STEP_ASSERT = "assert"
STEP_ENSURE = "ensure"
STEP_CALL = "call"
STEP_DELAY = "delay"
STEP_WAIT_UNTIL = "wait_until"

#: Every supported step kind. A sequence may only contain these; anything else is refused
#: before the first request is sent.
SEQUENCE_STEP_KINDS = (
    STEP_WRITE,
    STEP_EXPECT,
    STEP_ASSERT,
    STEP_ENSURE,
    STEP_CALL,
    STEP_DELAY,
    STEP_WAIT_UNTIL,
)

#: Where a result was produced, so a gate release is visibly not a body step.
PHASE_PRECONDITION = "precondition"
PHASE_GATE_ON = "gate_on"
PHASE_GATE_VERIFY = "gate_verify"
PHASE_STEP = "step"
PHASE_KEEPALIVE = "keepalive"
PHASE_GATE_OFF = "gate_off"

#: Failure policies for the optional keepalive step.
KEEPALIVE_ABORT = "abort"
KEEPALIVE_WARN = "warn"


@dataclass(slots=True)
class SequenceStep:
    """One step of an operator-written sequence.

    The fields are generic; a step only uses the ones its ``kind`` needs. Texts may contain
    ``${name}`` placeholders that are substituted from the sequence inputs before the step runs.
    """

    kind: str
    id: str = ""
    node_id: str = ""
    #: Browse path resolved with TranslateBrowsePathsToNodeIds when ``node_id`` is empty.
    node_path: list[str] = field(default_factory=list)
    #: Start node for ``node_path``; the Objects folder by default.
    path_root: str = "i=85"
    #: Value to write (``write``, ``ensure``) or the expected value (``expect``, ``assert``, ``wait_until``).
    value: str = ""
    expected: str = ""
    #: Absolute tolerance for numeric comparisons; ``None`` means exact.
    tolerance: float | None = None
    method_node_id: str = ""
    #: Browse name resolved below the object node when ``method_node_id`` is empty.
    method_name: str = ""
    arguments: list[str] = field(default_factory=list)
    seconds: float = 0.0
    timeout_seconds: float = 10.0
    poll_seconds: float = 0.2
    #: Read back after a write (default off, so a step says explicitly whether it verifies).
    verify: bool = False
    #: ``ensure`` only: a missing node is skipped with a reason instead of failing.
    optional: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "node_id": self.node_id,
            "node_path": list(self.node_path),
            "path_root": self.path_root,
            "value": self.value,
            "expected": self.expected,
            "tolerance": self.tolerance,
            "method_node_id": self.method_node_id,
            "method_name": self.method_name,
            "arguments": list(self.arguments),
            "seconds": self.seconds,
            "timeout_seconds": self.timeout_seconds,
            "poll_seconds": self.poll_seconds,
            "verify": self.verify,
            "optional": self.optional,
            "note": self.note,
        }


@dataclass(slots=True)
class SequenceGate:
    """Operator-defined exclusive access around the body.

    ``on`` runs before the body, ``off`` runs unconditionally afterwards. ``keepalive`` is an
    optional step repeated every ``keepalive_every_seconds`` while the body runs (renewal,
    heartbeat, watchdog kick). No step here is device-specific by construction.
    """

    on: list[SequenceStep] = field(default_factory=list)
    off: list[SequenceStep] = field(default_factory=list)
    #: Must pass after ``on``; otherwise the body never starts.
    verify: SequenceStep | None = None
    keepalive: SequenceStep | None = None
    keepalive_every_seconds: float = 5.0
    #: ``abort`` stops the body when a keepalive step fails, ``warn`` only records it.
    keepalive_on_failure: str = KEEPALIVE_ABORT

    def is_empty(self) -> bool:
        return not self.on and not self.off and self.verify is None and self.keepalive is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "on": [step.to_dict() for step in self.on],
            "off": [step.to_dict() for step in self.off],
            "verify": self.verify.to_dict() if self.verify is not None else None,
            "keepalive": self.keepalive.to_dict() if self.keepalive is not None else None,
            "keepalive_every_seconds": self.keepalive_every_seconds,
            "keepalive_on_failure": self.keepalive_on_failure,
        }


@dataclass(slots=True)
class Sequence:
    """One complete operator-written sequence."""

    name: str = ""
    description: str = ""
    #: ``${name}`` substitutions; a caller-supplied value wins over the document value.
    inputs: dict[str, str] = field(default_factory=dict)
    preconditions: list[SequenceStep] = field(default_factory=list)
    gate: SequenceGate = field(default_factory=SequenceGate)
    steps: list[SequenceStep] = field(default_factory=list)
    #: ``False`` keeps running after a failed body step; the run is still reported as failed.
    abort_on_failure: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": SEQUENCE_FORMAT,
            "version": SEQUENCE_VERSION,
            "name": self.name,
            "description": self.description,
            "inputs": dict(self.inputs),
            "preconditions": [step.to_dict() for step in self.preconditions],
            "gate": self.gate.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "abort_on_failure": self.abort_on_failure,
        }


@dataclass(slots=True)
class SequenceStepResult:
    """Outcome of one executed step (body, precondition, gate or keepalive)."""

    step_id: str
    kind: str
    phase: str = PHASE_STEP
    status: str = STATUS_SKIPPED
    detail: str = ""
    node_id: str = ""
    previous_value: str = ""
    new_value: str = ""
    status_code: str = ""
    #: Only set for a step that sent a write or a call: the authority it was sent under.
    authorization: str = ""
    started_at: datetime = field(default_factory=utc_now)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "phase": self.phase,
            "status": self.status,
            "detail": self.detail,
            "node_id": self.node_id,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "status_code": self.status_code,
            "authorization": self.authorization,
            "started_at": iso_utc(self.started_at),
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class SequenceRun:
    """Result of one sequence run, including the gate release outcome."""

    name: str = ""
    status: str = STATUS_SKIPPED
    dry_run: bool = False
    inputs: dict[str, str] = field(default_factory=dict)
    results: list[SequenceStepResult] = field(default_factory=list)
    error: str = ""
    #: "not engaged", "released", "release failed: ..." - never left empty after a run.
    gate_state: str = ""
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    duration_ms: float = 0.0
    #: The step currently awaiting a response; used to report a cancellation honestly.
    in_flight: SequenceStepResult | None = None

    def step_results(self, phase: str) -> list[SequenceStepResult]:
        return [result for result in self.results if result.phase == phase]

    def summary_text(self) -> str:
        prefix = "Dry run" if self.dry_run else "Sequence"
        text = f"{prefix} {self.name or '(unnamed)'}: {self.status}"
        if self.error:
            text += f" - {self.error}"
        if self.gate_state:
            text += f" ({self.gate_state})"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "dry_run": self.dry_run,
            "inputs": dict(self.inputs),
            "error": self.error,
            "gate_state": self.gate_state,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "duration_ms": self.duration_ms,
            "steps": [result.to_dict() for result in self.results],
        }
