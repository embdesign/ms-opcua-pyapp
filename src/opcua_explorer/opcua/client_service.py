"""High-level async OPC UA client service.

This is the only module in the application that owns an ``asyncua.Client``.
The GUI never touches asyncua objects: it submits coroutines to the asyncio
thread and receives plain model objects back.

Design notes
------------
* Discovery is done on a temporary ``SecurityPolicyNone`` secure channel
  (socket + Hello + OpenSecureChannel), exactly like asyncua's own
  ``connect_and_get_server_endpoints()`` helper, but explicit so the channel and
  socket are always closed.
* A secure connection never lets asyncua fetch the server certificate on its
  own. The operator-approved DER certificate is passed to ``set_security``.
* Only the requested (policy, mode) pair is used. If the server does not
  advertise it, :class:`EndpointNotAdvertisedError` is raised; there is no
  fallback to a weaker configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from asyncua import Client

from ..config import ConnectionSettings, TrustStore, normalize_fingerprint
from ..errors import (
    CertificateApprovalRequiredError,
    CertificateRejectedError,
    ConnectionFailedError,
    InvalidNodeIdError,
    NotConnectedError,
    OpcUaExplorerError,
)
from ..logging_setup import register_secret
from ..models import (
    AUTHORITY_OPERATOR_DIALOG,
    AddressSpace,
    BrowseOptions,
    ChildEntry,
    ConditionRefreshResult,
    ConditionStateSnapshot,
    ConnectionInfo,
    DiscoveryResult,
    EventConfig,
    EventHistoryConfig,
    EventHistoryResult,
    EventSourceInfo,
    EventStart,
    HistoryConfig,
    HistoryResult,
    HistoryUpdateConfig,
    HistoryUpdateOutcome,
    MonitorConfig,
    MonitoredItemInfo,
    MonitorStart,
    NamespaceEntry,
    NodeCreateOutcome,
    NodeCreateRequest,
    NodeDeleteOutcome,
    NodeDetails,
    Sequence,
    SequenceRun,
    ServerDiscoveryResult,
    ServerStatusSnapshot,
    TestConfig,
    TestProgress,
    TestRun,
    TraversalConfig,
    TraversalProgress,
    ValueReadOptions,
    ValueReadResult,
    ValueSnapshot,
    WriteAuthority,
    condition_action_label,
)
from .condition_state import read_condition_states as read_condition_states_impl
from .event_service import event_id_bytes
from .event_service import read_event_history as read_event_history_impl
from .event_session import EventSession
from .history_service import PrepareHistoryError
from .history_service import history_update as history_update_impl
from .history_service import prepare_history_update as history_update_prepare_impl
from .history_service import read_history as read_history_impl
from .monitor_session import MonitorSession
from .node_ids import normalise_node_id as _normalise_node_id
from .node_management import build_add_node_item as build_add_node_item_impl
from .node_management import create_node as create_node_impl
from .node_management import delete_node as delete_node_impl
from .node_reader import NodeReader
from .nodeset2 import NodeSet2Config, NodeSet2Result, export_nodeset2
from .path_support import PathRequest, PathResolution
from .security import default_application_uri, select_endpoint
from .sequence_runner import SequenceRunner
from .server_diagnostics import read_server_status
from .session_setup import (
    discover as discover_impl,
)
from .session_setup import (
    discover_servers as discover_servers_impl,
)
from .session_setup import load_namespaces, open_session, safe_disconnect
from .session_setup import normalise_url as _normalise_url
from .test_runner import run_functional_tests
from .traversal import download_address_space
from .write_service import AttributeWriteOutcome, CallOutcome, Inspection, PrepareError, WriteResult
from .write_service import call_method as invoke_method_impl
from .write_service import condition_action as condition_action_impl
from .write_service import inspect_call as call_preview_impl
from .write_service import inspect_write as write_preview_impl
from .write_service import prepare_attribute_write as attribute_write_prepare_impl
from .write_service import read_method_signature as read_method_signature_impl
from .write_service import write_and_restore as write_node_value_impl
from .write_service import write_attribute_value as write_attribute_impl

_logger = logging.getLogger(__name__)


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class OpcUaService:
    """Owns the asyncua client and exposes coarse-grained operations."""

    def __init__(self, trust_store: TrustStore | None = None, application_uri: str | None = None) -> None:
        self.trust_store = trust_store if trust_store is not None else TrustStore()
        self._application_uri = application_uri or default_application_uri()
        self._client: Client | None = None
        self._connection: ConnectionInfo | None = None
        self._discovery: DiscoveryResult | None = None
        self._namespaces: list[NamespaceEntry] = []
        self._namespace_index: dict[int, str] = {}
        self._reader = NodeReader(self._require_session, lambda: self._namespace_index)
        self._endpoint_url: str = ""
        self._monitor = MonitorSession()
        self._events = EventSession()
        self._sequence_runner: SequenceRunner | None = None

    # -- state -------------------------------------------------------------
    @property
    def application_uri(self) -> str:
        return self._application_uri

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._connection is not None

    @property
    def connection(self) -> ConnectionInfo | None:
        return self._connection

    @property
    def discovery(self) -> DiscoveryResult | None:
        return self._discovery

    @property
    def namespaces(self) -> list[NamespaceEntry]:
        return list(self._namespaces)

    def _require_client(self) -> Client:
        if self._client is None:
            raise NotConnectedError("No active OPC UA session. Connect to a server first.")
        return self._client

    def _require_session(self) -> Any:
        client = self._require_client()
        session = getattr(client.uaclient, "session", None)
        if session is None:
            raise NotConnectedError("No active OPC UA session. Connect to a server first.")
        return session

    @property
    def endpoint_url(self) -> str:
        """The remembered endpoint URL. Immutable between connects, so reading it is safe."""
        return self._endpoint_url

    async def connection_state(self) -> str:
        """Current asyncua connection state as a short string (for diagnostics)."""
        if self._client is None:
            return "disconnected"
        try:
            return str(self._client.state.value)
        except Exception:  # pragma: no cover - defensive
            return "unknown"

    async def connection_snapshot(self) -> ConnectionInfo | None:
        """Return the current connection info from inside the loop thread.

        The GUI must not read service attributes directly: keeping all access in
        the asyncio thread avoids unsynchronised cross-thread reads.
        """
        return self._connection

    # -- discovery ---------------------------------------------------------
    async def discover(
        self,
        endpoint_url: str,
        *,
        request_timeout: float = 10.0,
        connect_timeout: float | None = None,
    ) -> DiscoveryResult:
        """Read the advertised endpoint list without authenticating.

        Uses a temporary SecurityPolicyNone secure channel: GetEndpoints is a
        channel-level service and requires no session or trusted certificate.
        """
        discovery = await discover_impl(
            endpoint_url,
            application_uri=self._application_uri,
            request_timeout=request_timeout,
            connect_timeout=connect_timeout,
        )
        self._discovery = discovery
        return discovery

    async def discover_servers(
        self,
        discovery_url: str,
        *,
        server_uris: Iterable[str] = (),
        request_timeout: float = 10.0,
        connect_timeout: float | None = None,
    ) -> ServerDiscoveryResult:
        """Ask a discovery endpoint which servers it knows about (FindServers).

        FindServers is a channel-level service: it needs no session, no identity
        and no trust decision, so a temporary SecurityPolicyNone channel is used
        and closed again. ``server_uris`` filters on the application-URI prefix,
        as the service defines; an empty filter returns every known application.
        """
        return await discover_servers_impl(
            discovery_url,
            application_uri=self._application_uri,
            server_uris=server_uris,
            request_timeout=request_timeout,
            connect_timeout=connect_timeout,
        )

    # -- connect / disconnect ---------------------------------------------
    async def connect(self, settings: ConnectionSettings) -> ConnectionInfo:
        """Establish a session using exactly the requested security configuration."""
        problems = settings.validate()
        if problems:
            raise ConnectionFailedError(" ".join(problems))

        url = _normalise_url(settings.endpoint_url)

        # Register secrets so that no log record can ever contain them.
        register_secret(settings.password)
        register_secret(settings.private_key_password)

        discovery = self._discovery
        if discovery is None or discovery.endpoint_url != url:
            discovery = await self.discover(
                url,
                request_timeout=settings.request_timeout,
                connect_timeout=settings.connect_timeout,
            )

        # Raises EndpointNotAdvertisedError instead of downgrading.
        endpoint = select_endpoint(discovery.endpoints, settings.security_policy_uri, settings.security_mode)

        if settings.is_secure:
            if settings.server_certificate_der is None:
                raise CertificateApprovalRequiredError(
                    "The server certificate must be reviewed and approved before a secure connection is opened."
                )
            approved_fingerprint = _sha256_hex(settings.server_certificate_der)
            if endpoint.certificate_sha256 and normalize_fingerprint(endpoint.certificate_sha256) != approved_fingerprint:
                raise CertificateRejectedError(
                    "The certificate advertised by the server changed since it was approved "
                    f"(approved {approved_fingerprint[:16]}..., now {endpoint.certificate_sha256[:16]}...). "
                    "Review the new certificate before connecting."
                )

        await self.disconnect()
        opened = await open_session(
            settings,
            application_uri=self._application_uri,
            discovery=discovery,
            endpoint=endpoint,
        )
        self._client = opened.client
        self._endpoint_url = url
        self._discovery = discovery
        self._namespaces = opened.namespaces
        self._namespace_index = opened.namespace_index
        self._connection = opened.connection
        return self._connection

    async def disconnect(self) -> None:
        await self.event_monitor_stop()
        await self.monitor_stop()
        client = self._client
        self._client = None
        self._connection = None
        self._namespaces = []
        self._namespace_index = {}
        self._sequence_runner = None
        if client is None:
            return
        await safe_disconnect(client)

    async def shutdown(self) -> None:
        """Release session, live-monitor subscription and client resources."""
        await self.disconnect()

    # -- diagnostics -------------------------------------------------------
    async def refresh_namespaces(self) -> list[NamespaceEntry]:
        client = self._require_client()
        namespaces, index = await load_namespaces(client)
        self._namespaces = namespaces
        self._namespace_index = index
        if self._connection is not None:
            self._connection.namespaces = namespaces
        return namespaces

    # -- server status & diagnostics ---------------------------------------
    async def server_status(self) -> ServerStatusSnapshot:
        """One read-only ServerStatus/ServerDiagnostics snapshot.

        Includes this session's live-monitor subscription and items as the
        client sees them; no diagnostic method is called.
        """
        session = self._require_session()
        return await read_server_status(session, monitor=self._monitor)

    # -- raw history -------------------------------------------------------
    async def read_history(self, node_id: Any, config: HistoryConfig) -> HistoryResult:
        """Read raw or one aggregate of one variable; no update and no retry."""
        session = self._require_session()
        return await read_history_impl(session, node_id, config)

    async def history_update_preview(self, config: HistoryUpdateConfig) -> Inspection:
        """Resolve the node's type and parse the values without sending anything."""
        client = self._require_client()
        try:
            prepared = await history_update_prepare_impl(client, config)
        except PrepareHistoryError as exc:
            return Inspection(ok=False, detail=str(exc))
        if prepared.config.action == "delete":
            detail = (
                f"would delete the stored values of {prepared.node_id} between "
                f"{prepared.config.start} and {prepared.config.end}"
            )
            return Inspection(ok=True, detail=detail)
        label = prepared.value_type.label() if prepared.value_type is not None else "value"
        detail = (
            f"would {prepared.config.action} {prepared.sent_value_count} value(s) in "
            f"{prepared.node_id} ({label}) and read the range back"
        )
        return Inspection(
            ok=True,
            detail=detail,
            preview=prepared.config.describe_entries(),
            data_type_label=label,
            value_rank=int(getattr(prepared.value_type, "value_rank", -1)),
        )

    async def write_history(
        self, config: HistoryUpdateConfig, *, reference: str = ""
    ) -> HistoryUpdateOutcome:
        """Send one confirmed ``HistoryUpdate`` and read the affected range back.

        One node, one request, never retried. The operator dialog is the
        authority; a cancelled or refused preparation sends nothing.
        """
        client = self._require_client()
        self._require_session()
        authorization = WriteAuthority(
            AUTHORITY_OPERATOR_DIALOG,
            reference or f"history {config.action} {config.node_id}".strip(),
        )
        return await history_update_impl(client, config, authorization=authorization)

    # -- stored events and condition state ---------------------------------
    async def read_event_history(
        self, node_id: Any, config: EventHistoryConfig
    ) -> EventHistoryResult:
        """Read the stored events of one source node; read-only, one bounded request per page."""
        session = self._require_session()
        return await read_event_history_impl(session, node_id, config)

    async def read_condition_states(self, condition_node_ids: list[Any]) -> ConditionStateSnapshot:
        """Read the state variables of the given condition objects, once.

        Nothing is inferred from an event and nothing is cached: every call reads
        the server's own ``AckedState``/``ConfirmedState``/``EnabledState``/``Retain``
        values again, and a missing state is reported as missing.
        """
        session = self._require_session()
        ids = [_normalise_node_id(node_id) for node_id in condition_node_ids]
        return await read_condition_states_impl(session, ids)

    # -- browsing and reading ----------------------------------------------
    async def browse_children(
        self, node_id: Any, *, follow_subtypes: bool = True, options: BrowseOptions | None = None
    ) -> list[ChildEntry]:
        return await self._reader.browse_children(
            node_id, follow_subtypes=follow_subtypes, options=options
        )

    async def resolve_browse_paths(self, requests: list[PathRequest]) -> list[PathResolution]:
        """Resolve browse paths with TranslateBrowsePathsToNodeIds on the current session.

        Each request yields one resolution; ``PathResolution.ok`` says whether exactly one target
        was found. Nothing is discovered by guessing - ambiguous paths are reported, not picked.
        """
        return await self._reader.resolve_browse_paths(requests)

    async def read_node_details(self, node_id: Any) -> NodeDetails:
        """Read all applicable attributes plus references of one node."""
        return await self._reader.read_node_details(node_id)

    async def read_value(self, node_id: Any) -> ValueSnapshot:
        return await self._reader.read_value(node_id)

    async def read_value_with_options(
        self, node_id: Any, options: ValueReadOptions | None = None
    ) -> ValueReadResult:
        """Read the Value attribute once with IndexRange, MaxAge and TimestampsToReturn."""
        return await self._reader.read_value_with_options(node_id, options)

    # -- live monitoring ---------------------------------------------------
    @property
    def is_monitoring(self) -> bool:
        return self._monitor.is_active

    async def start_monitor(
        self,
        node_ids: list[Any],
        config: MonitorConfig,
        *,
        emit: Callable[[Any], None],
    ) -> MonitorStart:
        """Create one data-change subscription for the given NodeIds.

        ``emit`` is called from the asyncio thread for every data-change
        notification (:class:`~..models.MonitorNotification`) and subscription
        status change (:class:`~..models.MonitorStatusEvent`). It must be
        thread-safe; the GUI passes an :class:`AsyncRunner` progress reporter.
        """
        client = self._require_client()
        self._require_session()
        ids = [_normalise_node_id(node_id) for node_id in node_ids]
        if not ids:
            raise InvalidNodeIdError("Add at least one NodeId before starting the monitor.")
        return await self._monitor.start(client, ids, config, emit=emit)

    async def monitor_add(self, node_ids: list[Any]) -> list[MonitoredItemInfo]:
        """Subscribe more nodes to the running monitor subscription."""
        ids = [_normalise_node_id(node_id) for node_id in node_ids]
        return await self._monitor.add(ids)

    async def monitor_remove(self, handles: list[int]) -> None:
        """Delete the given monitored items from the running subscription."""
        await self._monitor.remove(handles)

    async def monitor_stop(self) -> None:
        """Delete the live-monitor subscription; safe to call when none runs."""
        await self._monitor.stop()

    async def monitor_set_mode(self, mode: str) -> str:
        """Apply ``SetMonitoringMode`` (Reporting/Sampling/Disabled) to the monitor."""
        return await self._monitor.set_monitoring_mode(mode)

    async def monitor_apply_intervals(
        self, *, publishing_interval_ms: float, sampling_interval_ms: float
    ) -> tuple[float, list[str]]:
        """Modify the running subscription's publishing and sampling intervals.

        Returns the publishing interval the server revised to and any per-item
        refusal texts; nothing is changed when the monitor is not running.
        """
        return await self._monitor.apply_intervals(
            publishing_interval_ms=publishing_interval_ms,
            sampling_interval_ms=sampling_interval_ms,
        )

    # -- event monitoring --------------------------------------------------
    @property
    def is_monitoring_events(self) -> bool:
        return self._events.is_active

    async def start_event_monitor(
        self,
        source_node_ids: list[Any],
        config: EventConfig,
        *,
        emit: Callable[[Any], None],
    ) -> EventStart:
        """Create one event subscription for the given source nodes.

        ``emit`` is called from the asyncio thread for every event
        (:class:`~..models.EventNotification`) and subscription status change
        (:class:`~..models.MonitorStatusEvent`). It must be thread-safe.
        """
        client = self._require_client()
        self._require_session()
        ids = [_normalise_node_id(node_id) for node_id in source_node_ids]
        if not ids:
            raise InvalidNodeIdError("Add at least one source NodeId before starting event monitoring.")
        return await self._events.start(client, ids, config, emit=emit)

    async def event_monitor_add(self, source_node_ids: list[Any]) -> list[EventSourceInfo]:
        """Listen to events of more source nodes on the running event subscription."""
        ids = [_normalise_node_id(node_id) for node_id in source_node_ids]
        return await self._events.add(ids)

    async def event_monitor_remove(self, handles: list[int]) -> None:
        """Stop listening to the given event sources."""
        await self._events.remove(handles)

    async def event_monitor_stop(self) -> None:
        """Delete the event subscription; safe to call when none runs."""
        await self._events.stop()

    async def refresh_event_conditions(self, *, reference: str = "") -> ConditionRefreshResult:
        """Ask the server to re-publish the retained conditions of the event subscription.

        One ``ConditionRefresh`` method call on the running subscription, sent
        once. It stores nothing, acknowledges nothing and interrupts nothing, so
        it needs no confirmation dialog; the result reports the StatusCode and
        whether the server's RefreshStart/RefreshEnd events were observed.
        """
        self._require_client()
        self._require_session()
        return await self._events.refresh_conditions(reference=reference)

    async def condition_action(
        self,
        condition_node_id: str,
        event_id_hex: str,
        comment: str,
        *,
        action: str = "acknowledge",
        reference: str = "",
    ) -> CallOutcome:
        """Call one A&C method on one condition event: exactly once, never retried.

        ``action`` selects the standard method (``acknowledge``, ``confirm``,
        ``enable``, ``disable`` or ``comment``); all of them take the event's
        EventId and the operator's comment. The operator dialog is the authority;
        one action per call, no second method, no automatic comment and no retry.
        """
        client = self._require_client()
        self._require_session()
        event_id = event_id_bytes(event_id_hex)
        if event_id is None:
            raise OpcUaExplorerError(
                "The event has no usable EventId; no condition method was called."
            )
        label = condition_action_label(action)
        authorization = WriteAuthority(
            AUTHORITY_OPERATOR_DIALOG,
            reference or f"{label.lower()} {condition_node_id}",
        )
        return await condition_action_impl(
            client,
            condition_node_id,
            event_id,
            comment,
            action=action,
            authorization=authorization,
        )

    # -- interactive write / call ------------------------------------------
    async def method_signature(self, method_node_id: Any) -> list[dict[str, Any]]:
        """``InputArguments`` of one method as plain rows; nothing is called."""
        client = self._require_client()
        target = _normalise_node_id(method_node_id)
        return await read_method_signature_impl(client, target)

    async def write_preview(self, node_id: Any, value_text: str) -> Inspection:
        """Validate one write without sending it (type, ValueRank, parsed preview)."""
        client = self._require_client()
        return await write_preview_impl(client, _normalise_node_id(node_id), value_text)

    async def call_preview(
        self, object_node_id: Any, method_node_id: Any, argument_texts: list[str]
    ) -> Inspection:
        """Validate one method call without calling it."""
        client = self._require_client()
        return await call_preview_impl(
            client,
            _normalise_node_id(object_node_id),
            _normalise_node_id(method_node_id),
            [str(text) for text in argument_texts],
        )

    async def write_node_value(
        self,
        node_id: Any,
        value_text: str,
        *,
        restore_previous: bool = False,
        reference: str = "",
    ) -> WriteResult:
        """One operator-requested write of one variable, read back and optionally restored.

        The operator dialog is the authority. Exactly one write is sent, then read
        back; a restore is one further verified write and happens only when asked.
        """
        client = self._require_client()
        self._require_session()
        target = _normalise_node_id(node_id)
        authorization = WriteAuthority(AUTHORITY_OPERATOR_DIALOG, reference or f"write {target}")
        return await write_node_value_impl(
            client,
            target,
            value_text,
            authorization=authorization,
            restore_previous=restore_previous,
        )

    async def attribute_write_preview(self, node_id: Any, attribute_name: str, text: str) -> Inspection:
        """Validate one DisplayName/Description write without sending it."""
        client = self._require_client()
        target = _normalise_node_id(node_id)
        try:
            prepared = await attribute_write_prepare_impl(client, target, attribute_name, text)
        except PrepareError as exc:
            return Inspection(ok=False, detail=str(exc))
        return Inspection(
            ok=True,
            detail=(
                f"would write {prepared.attribute_name} {prepared.text!r} to {target} "
                f"(currently {prepared.previous_text!r}) and read it back"
            ),
            preview=prepared.text,
        )

    # -- node management ---------------------------------------------------
    async def node_create_preview(self, request: NodeCreateRequest) -> Inspection:
        """Validate one AddNodes request without sending it."""
        client = self._require_client()
        try:
            item = await build_add_node_item_impl(client, request.normalised())
        except OpcUaExplorerError as exc:
            return Inspection(ok=False, detail=str(exc))
        normalised = request.normalised()
        return Inspection(
            ok=True,
            detail=(
                f"would create {normalised.label()} in namespace {item.BrowseName.NamespaceIndex} "
                "and read the new node back"
            ),
            preview=normalised.browse_name,
        )

    async def create_node(self, request: NodeCreateRequest, *, reference: str = "") -> NodeCreateOutcome:
        """One operator-requested AddNodes call; the server assigns the NodeId."""
        client = self._require_client()
        self._require_session()
        normalised = request.normalised()
        authorization = WriteAuthority(
            AUTHORITY_OPERATOR_DIALOG,
            reference or f"create {normalised.node_class} {normalised.browse_name}",
        )
        return await create_node_impl(client, normalised, authorization=authorization)

    async def delete_node(self, node_id: Any, *, reference: str = "") -> NodeDeleteOutcome:
        """One operator-requested DeleteNodes call, verified by reading the node afterwards."""
        client = self._require_client()
        self._require_session()
        target = _normalise_node_id(node_id)
        authorization = WriteAuthority(AUTHORITY_OPERATOR_DIALOG, reference or f"delete {target}")
        return await delete_node_impl(client, target, authorization=authorization)

    async def write_attribute_value(
        self,
        node_id: Any,
        attribute_name: str,
        text: str,
        *,
        reference: str = "",
    ) -> AttributeWriteOutcome:
        """One operator-requested DisplayName/Description write, read back and compared."""
        client = self._require_client()
        self._require_session()
        target = _normalise_node_id(node_id)
        authorization = WriteAuthority(
            AUTHORITY_OPERATOR_DIALOG, reference or f"write {attribute_name} of {target}"
        )
        return await write_attribute_impl(
            client, target, attribute_name, text, authorization=authorization
        )

    async def invoke_method(
        self,
        object_node_id: Any,
        method_node_id: Any,
        argument_texts: list[str],
        *,
        reference: str = "",
    ) -> CallOutcome:
        """One operator-requested method call on one object node; exactly once."""
        client = self._require_client()
        self._require_session()
        owner = _normalise_node_id(object_node_id)
        method = _normalise_node_id(method_node_id)
        authorization = WriteAuthority(AUTHORITY_OPERATOR_DIALOG, reference or f"call {method}")
        return await invoke_method_impl(
            client,
            owner,
            method,
            [str(text) for text in argument_texts],
            authorization=authorization,
        )

    # -- download ----------------------------------------------------------
    async def download(
        self,
        roots: list[str],
        config: TraversalConfig,
        *,
        progress: Callable[[TraversalProgress], None] | None = None,
    ) -> AddressSpace:
        """Download the address space (or a subtree) under the session's rights."""
        session = self._require_session()
        normalised_roots = [_normalise_node_id(root) for root in roots]
        if not normalised_roots:
            raise InvalidNodeIdError("At least one start NodeId is required for a download.")

        snapshot = await download_address_space(
            session,
            normalised_roots,
            namespaces=self._namespaces,
            config=config,
            progress=progress,
            connection=self._connection,
            endpoint_url=self._endpoint_url,
        )
        return snapshot

    # -- NodeSet2 export ---------------------------------------------------
    async def export_nodeset2(
        self,
        path: str | Path,
        snapshot: AddressSpace,
        config: NodeSet2Config,
        *,
        progress: Callable[[TraversalProgress], None] | None = None,
    ) -> NodeSet2Result:
        """Write a NodeSet2 XML file for a previously downloaded snapshot.

        The exporter re-collects dependencies (parents, type definitions,
        encodings, method arguments) from the live session, so the downloaded
        snapshot does not have to be complete by itself.
        """
        client = self._require_client()
        session = self._require_session()
        return await export_nodeset2(client, snapshot, path, config, session=session, progress=progress)

    # -- functional tests --------------------------------------------------
    async def run_functional_tests(
        self,
        config: TestConfig,
        *,
        progress: Callable[[TestProgress], None] | None = None,
    ) -> TestRun:
        """Run the bounded functional check suite on this session.

        The default checks are read-only; writes and method calls run only when
        ``config.allow_writes`` is enabled and explicit targets are configured. The
        security configuration is never changed. The only disruptive check
        (connection interruption) is opt-in through
        ``config.include_channel_recovery``.
        """
        client = self._require_client()
        self._require_session()

        async def discover_now() -> DiscoveryResult:
            return await self.discover(
                self._endpoint_url,
                request_timeout=config.request_timeout,
                connect_timeout=config.request_timeout,
            )

        return await run_functional_tests(
            client,
            connection=self._connection,
            config=config,
            endpoint_url=self._endpoint_url,
            discover=discover_now,
            progress=progress,
        )

    # -- operator-written sequences ---------------------------------------
    def _sequence_runner_for_session(self) -> SequenceRunner:
        """One runner per session, so a second sequence cannot interleave with the first."""
        if self._sequence_runner is None:
            self._sequence_runner = SequenceRunner(self._require_client())
        return self._sequence_runner

    async def run_sequence(
        self,
        sequence: Sequence,
        *,
        inputs: dict[str, str] | None = None,
        dry_run: bool = False,
        on_step: Any = None,
    ) -> SequenceRun:
        """Run one operator-written sequence on this session.

        Nothing here is device-specific: the sequence document owns the steps. A dry run
        resolves every target and type and sends nothing.
        """
        self._require_session()
        runner = self._sequence_runner_for_session()
        return await runner.run(sequence, inputs=inputs, dry_run=dry_run, on_step=on_step)

    async def release_sequence_gate(
        self,
        sequence: Sequence,
        *,
        inputs: dict[str, str] | None = None,
    ) -> SequenceRun:
        """Execute only the sequence's gate release steps (operator-triggered)."""
        self._require_session()
        runner = self._sequence_runner_for_session()
        return await runner.release_gate(sequence, inputs=inputs)

    @property
    def last_sequence_run(self) -> SequenceRun | None:
        """The most recent run of this session, including a cancelled one."""
        runner = self._sequence_runner
        return runner.last_run if runner is not None else None



