"""Functional-check configuration and result records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import (
    DEFAULT_LATENCY_SAMPLES,
    DEFAULT_NOTIFICATION_WAIT_SECONDS,
    DEFAULT_PUBLISHING_INTERVAL_MS,
    DEFAULT_SAMPLING_INTERVAL_MS,
    MAX_LATENCY_SAMPLES,
    MAX_NOTIFICATION_WAIT_SECONDS,
    MAX_PUBLISHING_INTERVAL_MS,
    MIN_NOTIFICATION_WAIT_SECONDS,
    MIN_PUBLISHING_INTERVAL_MS,
)
from .common import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    ConnectionInfo,
    iso_utc,
    utc_now,
)
from .writes import MethodCallTarget, WriteTarget

#: Shown in the GUI and in every exported report. The checks are behavioural
#: smoke checks, they are not a certification of OPC UA conformance.
FUNCTIONAL_TEST_DISCLAIMER = (
    "Functional and interoperability checks - not official OPC UA certification."
)


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
            publishing_interval_ms=min(
                MAX_PUBLISHING_INTERVAL_MS, max(MIN_PUBLISHING_INTERVAL_MS, float(self.publishing_interval_ms))
            ),
            sampling_interval_ms=min(MAX_PUBLISHING_INTERVAL_MS, max(0.0, float(self.sampling_interval_ms))),
            notification_wait_seconds=min(
                MAX_NOTIFICATION_WAIT_SECONDS, max(MIN_NOTIFICATION_WAIT_SECONDS, float(self.notification_wait_seconds))
            ),
            latency_samples=min(MAX_LATENCY_SAMPLES, max(1, int(self.latency_samples))),
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
