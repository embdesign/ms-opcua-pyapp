"""Project files: one JSON document describing every open session's configuration.

A project stores *configuration and editor state only*. It never stores
credentials (passwords, private-key passwords), server certificate bytes or
trust decisions - those stay in the machine trust store and are approved per
machine. Opening a project never connects, subscribes, downloads, writes or runs
anything: it restores the editors and the operator decides what happens next.

The module is deliberately Qt-free so it can be tested and reused headless.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConnectionProfile, assert_no_secrets
from .defaults import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_LATENCY_SAMPLES,
    DEFAULT_MAX_DEPENDENCIES,
    DEFAULT_MAX_NODES,
    DEFAULT_MONITOR_KEEP_ROWS,
    DEFAULT_NOTIFICATION_WAIT_SECONDS,
    DEFAULT_PUBLISHING_INTERVAL_MS,
    DEFAULT_SAMPLING_INTERVAL_MS,
)
from .models import MethodCallTarget, WriteTarget

_logger = logging.getLogger(__name__)

PROJECT_FORMAT = "opcua-explorer-project"
PROJECT_VERSION = 2
PROJECT_SUFFIX = ".opcua-project.json"
PROJECT_FILE_FILTER = "OPC UA Explorer project (*.opcua-project.json *.json);;All files (*)"


_NOTE = (
    "Configuration and editor state only. No passwords, private-key passwords, certificate bytes "
    "or trust decisions are stored; the trust store stays on this machine."
)


class ProjectError(ValueError):
    """A project file could not be read, written or validated."""


def _as_int(data: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _as_float(data: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _as_str(data: dict[str, Any], key: str, default: str | None) -> str | None:
    value = data.get(key, default)
    return default if value is None else str(value)


# ---------------------------------------------------------------------------
# Per-page editor state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExportState:
    """Export tab configuration (download limits, scope, namespace filter)."""

    scope: str = "whole"  # whole | subtree
    subtree_node_id: str | None = None
    max_nodes: int = DEFAULT_MAX_NODES
    max_depth: int = 0
    concurrency: int = DEFAULT_CONCURRENCY
    batch_size: int = DEFAULT_BATCH_SIZE
    max_references_per_node: int = 0
    include_values: bool = False
    include_description: bool = True
    include_device_attributes: bool = False
    #: Checked namespace indexes; ``None`` means "all namespaces".
    namespaces: list[int] | None = None
    include_dependencies: bool = True
    max_dependency_nodes: int = DEFAULT_MAX_DEPENDENCIES
    schema_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExportState:
        data = data if isinstance(data, dict) else {}
        defaults = cls()
        scope = str(data.get("scope", defaults.scope))
        if scope not in ("whole", "subtree"):
            scope = defaults.scope
        namespaces: list[int] | None = None
        raw_namespaces = data.get("namespaces", defaults.namespaces)
        if raw_namespaces is not None:
            try:
                namespaces = sorted({int(item) for item in raw_namespaces})
            except (TypeError, ValueError):
                namespaces = None
        return cls(
            scope=scope,
            subtree_node_id=_as_str(data, "subtree_node_id", defaults.subtree_node_id),
            max_nodes=_as_int(data, "max_nodes", defaults.max_nodes),
            max_depth=_as_int(data, "max_depth", defaults.max_depth),
            concurrency=_as_int(data, "concurrency", defaults.concurrency),
            batch_size=_as_int(data, "batch_size", defaults.batch_size),
            max_references_per_node=_as_int(
                data, "max_references_per_node", defaults.max_references_per_node
            ),
            include_values=bool(data.get("include_values", defaults.include_values)),
            include_description=bool(data.get("include_description", defaults.include_description)),
            include_device_attributes=bool(
                data.get("include_device_attributes", defaults.include_device_attributes)
            ),
            namespaces=namespaces,
            include_dependencies=bool(data.get("include_dependencies", defaults.include_dependencies)),
            max_dependency_nodes=_as_int(
                data, "max_dependency_nodes", defaults.max_dependency_nodes
            ),
            schema_path=_as_str(data, "schema_path", defaults.schema_path),
        )


@dataclass(slots=True)
class MonitorState:
    """Live-monitor targets. Restored as *pending* items; nothing is subscribed."""

    node_ids: list[str] = field(default_factory=list)
    publishing_interval_ms: float = DEFAULT_PUBLISHING_INTERVAL_MS
    sampling_interval_ms: float = DEFAULT_SAMPLING_INTERVAL_MS
    keep_rows: int = DEFAULT_MONITOR_KEEP_ROWS

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["node_ids"] = list(self.node_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorState:
        data = data if isinstance(data, dict) else {}
        defaults = cls()
        raw_ids = data.get("node_ids", [])
        node_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        return cls(
            node_ids=node_ids,
            publishing_interval_ms=_as_float(
                data, "publishing_interval_ms", defaults.publishing_interval_ms
            ),
            sampling_interval_ms=_as_float(
                data, "sampling_interval_ms", defaults.sampling_interval_ms
            ),
            keep_rows=_as_int(data, "keep_rows", defaults.keep_rows),
        )


@dataclass(slots=True)
class TestsState:
    """Tests tab configuration.

    Guarded targets are stored, but ``allow_writes`` is intentionally *not* part of
    a project: the master switch must be re-enabled by the operator for each run.
    """

    root_node_id: str = "i=85"
    variable_node_id: str | None = None
    publishing_interval_ms: float = DEFAULT_PUBLISHING_INTERVAL_MS
    sampling_interval_ms: float = DEFAULT_SAMPLING_INTERVAL_MS
    notification_wait_seconds: float = DEFAULT_NOTIFICATION_WAIT_SECONDS
    latency_samples: int = DEFAULT_LATENCY_SAMPLES
    include_channel_recovery: bool = False
    write_targets: list[WriteTarget] = field(default_factory=list)
    method_calls: list[MethodCallTarget] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_node_id": self.root_node_id,
            "variable_node_id": self.variable_node_id,
            "publishing_interval_ms": self.publishing_interval_ms,
            "sampling_interval_ms": self.sampling_interval_ms,
            "notification_wait_seconds": self.notification_wait_seconds,
            "latency_samples": self.latency_samples,
            "include_channel_recovery": self.include_channel_recovery,
            "write_targets": [target.to_dict() for target in self.write_targets],
            "method_calls": [call.to_dict() for call in self.method_calls],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestsState:
        data = data if isinstance(data, dict) else {}
        defaults = cls()
        write_targets: list[WriteTarget] = []
        for entry in data.get("write_targets", []) or []:
            if isinstance(entry, dict):
                write_targets.append(WriteTarget(**_known(WriteTarget, entry)))
        method_calls: list[MethodCallTarget] = []
        for entry in data.get("method_calls", []) or []:
            if isinstance(entry, dict):
                method_calls.append(MethodCallTarget(**_known(MethodCallTarget, entry)))
        return cls(
            root_node_id=str(data.get("root_node_id") or defaults.root_node_id),
            variable_node_id=_as_str(data, "variable_node_id", defaults.variable_node_id) or None,
            publishing_interval_ms=_as_float(
                data, "publishing_interval_ms", defaults.publishing_interval_ms
            ),
            sampling_interval_ms=_as_float(
                data, "sampling_interval_ms", defaults.sampling_interval_ms
            ),
            notification_wait_seconds=_as_float(
                data, "notification_wait_seconds", defaults.notification_wait_seconds
            ),
            latency_samples=_as_int(data, "latency_samples", defaults.latency_samples),
            include_channel_recovery=bool(
                data.get("include_channel_recovery", defaults.include_channel_recovery)
            ),
            write_targets=write_targets,
            method_calls=method_calls,
        )


def _known(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Filter unknown keys so a project from a newer build cannot inject fields."""
    known = {item.name for item in fields(cls)}
    return {key: value for key, value in data.items() if key in known}


@dataclass(slots=True)
class SequenceState:
    """Sequence tab editor state: which document belongs to this session and its inputs.

    The sequence itself stays a file next to the device documentation. A project records only
    the path and the operator's input values - opening a project never loads or runs a sequence.
    """

    path: str = ""
    inputs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "inputs": dict(self.inputs)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SequenceState:
        data = data if isinstance(data, dict) else {}
        raw_inputs = data.get("inputs") or {}
        inputs = (
            {str(name): str(value) for name, value in raw_inputs.items()}
            if isinstance(raw_inputs, dict)
            else {}
        )
        return cls(path=str(data.get("path") or ""), inputs=inputs)

# ---------------------------------------------------------------------------
# Session and project documents
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProjectSession:
    """One session: connection configuration plus the state of its persisted pages.

    The Events page is deliberately not part of a project: its sources are live
    session state, like the event log itself.
    """

    name: str = "Session"
    connection: ConnectionProfile = field(
        default_factory=lambda: ConnectionProfile(name="Session", endpoint_url="opc.tcp://127.0.0.1:4840")
    )
    start_node_id: str = "i=85"
    #: Title of the page that was open when the project was saved ("" = the first page).
    active_page: str = ""
    export: ExportState = field(default_factory=ExportState)
    monitor: MonitorState = field(default_factory=MonitorState)
    tests: TestsState = field(default_factory=TestsState)
    sequence: SequenceState = field(default_factory=SequenceState)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connection": self.connection.to_dict(),
            "start_node_id": self.start_node_id,
            "active_page": self.active_page,
            "export": self.export.to_dict(),
            "monitor": self.monitor.to_dict(),
            "tests": self.tests.to_dict(),
            "sequence": self.sequence.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectSession:
        if not isinstance(data, dict):
            raise ProjectError("a session entry is not an object")
        connection_data = data.get("connection", {})
        if not isinstance(connection_data, dict):
            raise ProjectError("a session's connection entry is not an object")
        connection = ConnectionProfile.from_dict(connection_data)
        return cls(
            name=str(data.get("name") or connection.name or "Session"),
            connection=connection,
            start_node_id=str(data.get("start_node_id") or "i=85"),
            active_page=str(data.get("active_page") or ""),
            export=ExportState.from_dict(data.get("export", {})),
            monitor=MonitorState.from_dict(data.get("monitor", {})),
            tests=TestsState.from_dict(data.get("tests", {})),
            sequence=SequenceState.from_dict(data.get("sequence", {})),
        )


@dataclass(slots=True)
class ProjectFile:
    """A versioned OPC UA Explorer project document."""

    sessions: list[ProjectSession] = field(default_factory=list)
    saved_at: str = ""
    application: str = ""

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "saved_at": self.saved_at or datetime.now(UTC).isoformat(timespec="seconds"),
            "application": self.application or f"OPC UA Explorer {__version__}",
            "note": _NOTE,
            "sessions": [session.to_dict() for session in self.sessions],
        }

    def problems(self) -> list[str]:
        """Human-readable validation problems that make the project unusable."""
        problems: list[str] = []
        if not self.sessions:
            problems.append("the project contains no sessions")
        for index, session in enumerate(self.sessions, start=1):
            label = (session.name or f"session {index}").strip()
            settings = session.connection.to_settings()
            problems.extend(
                f"{label}: {problem}" for problem in settings.validate(require_server_certificate=False)
            )
        return problems

    # -- files -------------------------------------------------------------
    def save(self, path: str | Path) -> None:
        target = Path(path)
        problems = self.problems()
        if problems:
            raise ProjectError("Refusing to save an incomplete project:\n- " + "\n- ".join(problems))
        payload = self.to_dict()
        try:
            assert_no_secrets(payload, context=str(target))
        except ValueError as exc:  # pragma: no cover - structurally impossible
            raise ProjectError(str(exc)) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(target)
        _logger.info("Project saved: %s (%d session(s))", target, len(self.sessions))

    @classmethod
    def load(cls, path: str | Path) -> ProjectFile:
        target = Path(path)
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectError(f"Could not read {target}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ProjectError(f"{target} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectError(f"{target} is not an OPC UA Explorer project file.")

        try:
            assert_no_secrets(data, context=str(target))
        except ValueError as exc:
            raise ProjectError(f"Refusing to load {target}: {exc}") from exc

        file_format = data.get("format")
        if file_format is not None and file_format != PROJECT_FORMAT:
            raise ProjectError(
                f"{target} is not an OPC UA Explorer project file (format {file_format!r})."
            )
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        if version <= 0:
            raise ProjectError(f"{target} has no usable project version.")
        if version > PROJECT_VERSION:
            raise ProjectError(
                f"{target} was written by a newer OPC UA Explorer "
                f"(project version {version}; this build supports {PROJECT_VERSION})."
            )

        raw_sessions = data.get("sessions")
        if not isinstance(raw_sessions, list) or not raw_sessions:
            raise ProjectError(f"{target} contains no sessions.")
        sessions: list[ProjectSession] = []
        for entry in raw_sessions:
            try:
                sessions.append(ProjectSession.from_dict(entry))
            except (ProjectError, TypeError, ValueError) as exc:
                raise ProjectError(f"{target} contains a malformed session: {exc}") from exc

        project = cls(
            sessions=sessions,
            saved_at=str(data.get("saved_at", "")),
            application=str(data.get("application", "")),
        )
        problems = project.problems()
        if problems:
            raise ProjectError(f"{target} cannot be loaded:\n- " + "\n- ".join(problems))
        _logger.info("Project loaded: %s (%d session(s))", target, len(project.sessions))
        return project
