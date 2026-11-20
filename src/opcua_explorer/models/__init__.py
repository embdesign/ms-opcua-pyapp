"""In-memory data model shared by the OPC UA service, exporters and GUI.

The package is deliberately transport-agnostic: nothing here imports asyncua or
Qt. Values are stored in a JSON-friendly form (see ``opcua.serialize``) so a
snapshot can be exported or inspected by the GUI without touching a session.

Every public name is re-exported here, so callers keep using
``from opcua_explorer.models import ...`` unchanged. The implementation lives
in one module per feature cluster:

* ``common`` - timestamps, namespace/connection records, shared status names
* ``discovery`` - GetEndpoints and FindServers results
* ``graph`` - node records, references, traversal config and ``AddressSpace``
* ``values`` - explicit Value-read options and results
* ``writes`` - write/call authority, targets and node-management requests
* ``functional_tests`` - ``TestConfig`` / ``TestRun`` and the check vocabulary
* ``monitor`` - live data-change subscription records
* ``events`` - event subscription records
* ``history`` - raw history read records
* ``server_status`` - read-only server diagnostics snapshot
* ``sequences`` - operator-written sequence documents and run results
"""

from __future__ import annotations

from .common import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    ConnectionInfo,
    NamespaceEntry,
    iso_utc,
    utc_now,
)
from .discovery import (
    DiscoveredServer,
    DiscoveryResult,
    EndpointInfo,
    ServerDiscoveryResult,
)
from .events import (
    EVENT_FIELD_CHAR_LIMIT,
    EventConfig,
    EventNotification,
    EventSourceInfo,
    EventStart,
)
from .functional_tests import (
    FUNCTIONAL_TEST_DISCLAIMER,
    TestCheck,
    TestConfig,
    TestProgress,
    TestRun,
)
from .graph import (
    BROWSE_DIRECTIONS,
    BROWSE_NODE_CLASSES,
    BROWSE_REFERENCE_TYPES,
    DETAILS_ONLY_ATTRIBUTES,
    AddressSpace,
    AttributeResult,
    BrowseOptions,
    ChildEntry,
    Edge,
    NodeDetails,
    NodeRecord,
    ReferenceInfo,
    TraversalConfig,
    TraversalProgress,
    ValueSnapshot,
)
from .history import HistoryConfig, HistoryResult
from .monitor import (
    DATA_CHANGE_TRIGGERS,
    DEADBAND_TYPES,
    MONITOR_VALUE_CHAR_LIMIT,
    MonitorConfig,
    MonitoredItemInfo,
    MonitorNotification,
    MonitorStart,
    MonitorStatusEvent,
)
from .sequences import (
    KEEPALIVE_ABORT,
    KEEPALIVE_WARN,
    PHASE_GATE_OFF,
    PHASE_GATE_ON,
    PHASE_GATE_VERIFY,
    PHASE_KEEPALIVE,
    PHASE_PRECONDITION,
    PHASE_STEP,
    SEQUENCE_FORMAT,
    SEQUENCE_STEP_KINDS,
    SEQUENCE_VERSION,
    STEP_ASSERT,
    STEP_CALL,
    STEP_DELAY,
    STEP_ENSURE,
    STEP_EXPECT,
    STEP_WAIT_UNTIL,
    STEP_WRITE,
    Sequence,
    SequenceGate,
    SequenceRun,
    SequenceStep,
    SequenceStepResult,
)
from .server_status import ServerStatusSnapshot
from .values import TIMESTAMPS_TO_RETURN, ValueReadOptions, ValueReadResult
from .writes import (
    AUTHORITY_OPERATOR_DIALOG,
    AUTHORITY_SEQUENCE_DOCUMENT,
    CREATABLE_NODE_CLASSES,
    WRITABLE_TEXT_ATTRIBUTES,
    MethodCallTarget,
    NodeCreateOutcome,
    NodeCreateRequest,
    NodeDeleteOutcome,
    WriteAuthority,
    WriteTarget,
)

__all__ = [
    "AUTHORITY_OPERATOR_DIALOG",
    "AUTHORITY_SEQUENCE_DOCUMENT",
    "AddressSpace",
    "AttributeResult",
    "BROWSE_DIRECTIONS",
    "BROWSE_NODE_CLASSES",
    "BROWSE_REFERENCE_TYPES",
    "BrowseOptions",
    "CREATABLE_NODE_CLASSES",
    "ChildEntry",
    "ConnectionInfo",
    "DATA_CHANGE_TRIGGERS",
    "DEADBAND_TYPES",
    "DETAILS_ONLY_ATTRIBUTES",
    "DiscoveredServer",
    "DiscoveryResult",
    "EVENT_FIELD_CHAR_LIMIT",
    "Edge",
    "EndpointInfo",
    "EventConfig",
    "EventNotification",
    "EventSourceInfo",
    "EventStart",
    "FUNCTIONAL_TEST_DISCLAIMER",
    "HistoryConfig",
    "HistoryResult",
    "KEEPALIVE_ABORT",
    "KEEPALIVE_WARN",
    "MONITOR_VALUE_CHAR_LIMIT",
    "MethodCallTarget",
    "MonitorConfig",
    "MonitorNotification",
    "MonitorStart",
    "MonitorStatusEvent",
    "MonitoredItemInfo",
    "NamespaceEntry",
    "NodeCreateOutcome",
    "NodeCreateRequest",
    "NodeDeleteOutcome",
    "NodeDetails",
    "NodeRecord",
    "PHASE_GATE_OFF",
    "PHASE_GATE_ON",
    "PHASE_GATE_VERIFY",
    "PHASE_KEEPALIVE",
    "PHASE_PRECONDITION",
    "PHASE_STEP",
    "ReferenceInfo",
    "SEQUENCE_FORMAT",
    "SEQUENCE_STEP_KINDS",
    "SEQUENCE_VERSION",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_OUTCOME_UNKNOWN",
    "STATUS_PASSED",
    "STATUS_SKIPPED",
    "STEP_ASSERT",
    "STEP_CALL",
    "STEP_DELAY",
    "STEP_ENSURE",
    "STEP_EXPECT",
    "STEP_WAIT_UNTIL",
    "STEP_WRITE",
    "Sequence",
    "SequenceGate",
    "SequenceRun",
    "SequenceStep",
    "SequenceStepResult",
    "ServerDiscoveryResult",
    "ServerStatusSnapshot",
    "TIMESTAMPS_TO_RETURN",
    "TestCheck",
    "TestConfig",
    "TestProgress",
    "TestRun",
    "TraversalConfig",
    "TraversalProgress",
    "ValueReadOptions",
    "ValueReadResult",
    "ValueSnapshot",
    "WRITABLE_TEXT_ATTRIBUTES",
    "WriteAuthority",
    "WriteTarget",
    "iso_utc",
    "utc_now",
]
