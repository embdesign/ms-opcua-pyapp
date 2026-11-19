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

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from asyncua import Client, ua

from ..config import ConnectionSettings, TrustStore, normalize_fingerprint
from ..errors import (
    CertificateApprovalRequiredError,
    CertificateRejectedError,
    ConnectionFailedError,
    InvalidNodeIdError,
    MonitorNotActiveError,
    NotConnectedError,
    OpcUaExplorerError,
)
from ..logging_setup import register_secret
from ..models import (
    AUTHORITY_OPERATOR_DIALOG,
    AddressSpace,
    BrowseOptions,
    ChildEntry,
    ConnectionInfo,
    DiscoveredServer,
    DiscoveryResult,
    EventConfig,
    EventSourceInfo,
    EventStart,
    HistoryConfig,
    HistoryResult,
    MonitorConfig,
    MonitoredItemInfo,
    MonitorStart,
    NamespaceEntry,
    NodeDetails,
    ReferenceInfo,
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
    utc_now,
)
from .attributes import AttributeSpec, read_attributes_bulk, read_node_attributes, read_values_bulk
from .browse import (
    browse_children as browse_children_impl,
)
from .browse import (
    browse_many,
    parse_node_id,
    resolve_browse_names,
)
from .event_service import LiveEvents, event_id_bytes
from .history_service import read_history as read_history_impl
from .monitor_service import LiveMonitor
from .nodeset2 import NodeSet2Config, NodeSet2Result, export_nodeset2
from .path_support import PathRequest, PathResolution, resolve_paths
from .security import (
    default_application_uri,
    endpoint_to_info,
    mode_from_name,
    policy_class,
    policy_label,
    select_endpoint,
    summarize_certificate,
)
from .sequence_runner import SequenceRunner
from .server_diagnostics import read_server_status
from .test_runner import run_functional_tests
from .traversal import download_address_space
from .ua_names import node_class_name
from .value_read import read_value_with_options as read_value_with_options_impl
from .write_service import AttributeWriteOutcome, CallOutcome, Inspection, PrepareError, WriteResult
from .write_service import acknowledge_condition as acknowledge_condition_impl
from .write_service import call_method as invoke_method_impl
from .write_service import inspect_call as call_preview_impl
from .write_service import inspect_write as write_preview_impl
from .write_service import prepare_attribute_write as attribute_write_prepare_impl
from .write_service import read_method_signature as read_method_signature_impl
from .write_service import write_and_restore as write_node_value_impl
from .write_service import write_attribute_value as write_attribute_impl

CLIENT_NAME = "OPC UA Explorer"
CLIENT_APPLICATION_NAME = "OPC UA Explorer"

#: Upper bound for the automatic-reconnect backoff (asyncua doubles 1 s steps up to this).
RECONNECT_MAX_DELAY = 30.0

_logger = logging.getLogger(__name__)

_SERVER_IDENTITY_IDS: dict[str, int] = {
    "product_name": ua.ObjectIds.Server_ServerStatus_BuildInfo_ProductName,
    "product_uri": ua.ObjectIds.Server_ServerStatus_BuildInfo_ProductUri,
    "manufacturer_name": ua.ObjectIds.Server_ServerStatus_BuildInfo_ManufacturerName,
    "software_version": ua.ObjectIds.Server_ServerStatus_BuildInfo_SoftwareVersion,
    "build_number": ua.ObjectIds.Server_ServerStatus_BuildInfo_BuildNumber,
    "build_date": ua.ObjectIds.Server_ServerStatus_BuildInfo_BuildDate,
}


def _normalise_url(endpoint_url: str) -> str:
    url = (endpoint_url or "").strip()
    if not url:
        raise ConnectionFailedError("An endpoint URL is required, for example opc.tcp://192.168.0.10:4840.")
    if "://" not in url:
        url = "opc.tcp://" + url
    return url


def _normalise_node_id(value: Any) -> str:
    if isinstance(value, str):
        return parse_node_id(value).to_string()
    if isinstance(value, ua.NodeId):
        return value.to_string()
    if isinstance(value, int):
        return ua.NodeId(value).to_string()
    raise InvalidNodeIdError(f"Cannot interpret {type(value).__name__} as a NodeId")


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


async def _log_connection_loss(exc: Exception) -> None:
    """asyncua supervisor callback: record the loss before it retries or gives up."""
    _logger.warning("Connection to the OPC UA server was lost: %s", exc)


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
        self._endpoint_url: str = ""
        self._monitor: LiveMonitor | None = None
        self._events: LiveEvents | None = None
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
        url = _normalise_url(endpoint_url)
        client = Client(url, timeout=float(request_timeout))
        client.name = CLIENT_NAME
        client.application_uri = self._application_uri
        timeout = float(connect_timeout if connect_timeout is not None else max(request_timeout, 5.0))

        try:
            raw_endpoints = await asyncio.wait_for(self._fetch_endpoints(client), timeout=timeout)
        except TimeoutError as exc:
            raise ConnectionFailedError(
                f"Endpoint discovery timed out after {timeout:.1f} s. Check the URL, the network and the "
                "server's port (usually 4840)."
            ) from exc
        except Exception as exc:
            raise ConnectionFailedError(
                f"Could not read the endpoint list from {url}: {exc}. "
                "Verify the URL, that the server is running, and that no firewall blocks the port."
            ) from exc

        if not raw_endpoints:
            raise ConnectionFailedError(f"{url} returned an empty endpoint list.")

        endpoints = [endpoint_to_info(endpoint) for endpoint in raw_endpoints]
        endpoints.sort(
            key=lambda item: (
                not item.is_secure,  # secure endpoints first
                item.security_policy_label,
                item.security_mode,
            )
        )
        discovery = DiscoveryResult(
            endpoint_url=url,
            endpoints=endpoints,
            server_application=self._describe_application(raw_endpoints[0].Server),
        )
        self._discovery = discovery
        _logger.info(
            "Discovered %d endpoint(s) at %s (%s)",
            len(endpoints),
            url,
            discovery.server_application.get("application_name") or "unknown server",
        )
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
        url = _normalise_url(discovery_url)
        wanted = [str(uri).strip() for uri in server_uris if str(uri).strip()]
        client = Client(url, timeout=float(request_timeout))
        client.name = CLIENT_NAME
        client.application_uri = self._application_uri
        timeout = float(connect_timeout if connect_timeout is not None else max(request_timeout, 5.0))
        started = time.monotonic()

        try:
            descriptions = await asyncio.wait_for(self._fetch_find_servers(client, wanted), timeout=timeout)
        except TimeoutError as exc:
            raise ConnectionFailedError(
                f"The server directory at {url} did not answer FindServers within {timeout:.1f} s. "
                "Check the URL, the network and that the discovery server is running."
            ) from exc
        except Exception as exc:
            raise ConnectionFailedError(
                f"Could not read the server directory at {url}: {exc}. "
                "A server that is not a discovery server may refuse FindServers; "
                "you can still connect to it directly."
            ) from exc

        servers = [self._describe_discovered_server(description) for description in descriptions]
        servers.sort(key=lambda item: (item.application_name.lower(), item.application_uri))
        result = ServerDiscoveryResult(
            discovery_url=url,
            server_uris=wanted,
            servers=servers,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
        _logger.info("FindServers at %s reported %d application(s)", url, len(servers))
        return result

    async def _fetch_find_servers(
        self, client: Client, server_uris: list[str]
    ) -> list[ua.ApplicationDescription]:
        await client.connect_socket()
        try:
            await client.send_hello()
            await client.open_secure_channel()
            try:
                return await client.find_servers(server_uris)
            finally:
                try:
                    await client.close_secure_channel()
                except Exception as exc:  # pragma: no cover - channel teardown best effort
                    _logger.debug("Closing the directory secure channel failed: %s", exc)
        finally:
            try:
                client.disconnect_socket()
            except Exception:  # pragma: no cover - socket teardown best effort
                _logger.debug("Closing the directory socket failed", exc_info=True)

    @staticmethod
    def _describe_discovered_server(application: ua.ApplicationDescription) -> DiscoveredServer:
        name = application.ApplicationName
        display = name.Text if isinstance(name, ua.LocalizedText) else str(name or "")
        try:
            application_type = ua.ApplicationType(int(application.ApplicationType)).name
        except Exception:  # pragma: no cover - defensive
            application_type = str(application.ApplicationType)
        return DiscoveredServer(
            application_uri=application.ApplicationUri or "",
            application_name=display or "",
            application_type=application_type,
            product_uri=application.ProductUri,
            gateway_server_uri=application.GatewayServerUri,
            discovery_profile_uri=application.DiscoveryProfileUri,
            discovery_urls=[url for url in (application.DiscoveryUrls or []) if url],
        )

    async def _fetch_endpoints(self, client: Client) -> list[ua.EndpointDescription]:
        await client.connect_socket()
        try:
            await client.send_hello()
            await client.open_secure_channel()
            try:
                return await client.get_endpoints()
            finally:
                try:
                    await client.close_secure_channel()
                except Exception as exc:  # pragma: no cover - channel teardown best effort
                    _logger.debug("Closing the discovery secure channel failed: %s", exc)
        finally:
            try:
                client.disconnect_socket()
            except Exception:  # pragma: no cover - socket teardown best effort
                _logger.debug("Closing the discovery socket failed", exc_info=True)

    @staticmethod
    def _describe_application(application: ua.ApplicationDescription | None) -> dict[str, Any]:
        if application is None:
            return {}
        name = application.ApplicationName
        display = name.Text if isinstance(name, ua.LocalizedText) else str(name or "")
        try:
            application_type = ua.ApplicationType(int(application.ApplicationType)).name
        except Exception:  # pragma: no cover - defensive
            application_type = str(application.ApplicationType)
        return {
            "application_name": display,
            "application_uri": application.ApplicationUri,
            "product_uri": application.ProductUri,
            "application_type": application_type,
            "discovery_urls": list(application.DiscoveryUrls or []),
            "gateway_server_uri": application.GatewayServerUri,
        }

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

        client = Client(
            url,
            timeout=float(settings.request_timeout),
            auto_reconnect=bool(settings.auto_reconnect),
            reconnect_max_delay=RECONNECT_MAX_DELAY,
            # While the supervisor retries, a call issued from the GUI waits at most
            # one request timeout before failing, instead of hanging for minutes.
            reconnect_request_timeout=float(settings.request_timeout),
        )
        client.name = CLIENT_NAME
        client.application_uri = self._application_uri
        client.session_timeout = int(settings.session_timeout_ms)
        client.secure_channel_timeout = int(settings.secure_channel_timeout_ms)
        client.connection_lost_callback = _log_connection_loss

        try:
            if settings.is_secure:
                await self._apply_security(client, settings)
            self._apply_identity(client, settings)
            await self._apply_certificate_identity(client, settings)
            try:
                await asyncio.wait_for(client.connect(), timeout=float(settings.connect_timeout))
            except TimeoutError as exc:
                raise ConnectionFailedError(
                    f"Connecting to {url} timed out after {settings.connect_timeout:.1f} s."
                ) from exc
        except asyncio.CancelledError:
            await self._safe_disconnect(client)
            raise
        except OpcUaExplorerError:
            await self._safe_disconnect(client)
            raise
        except Exception as exc:
            await self._safe_disconnect(client)
            raise ConnectionFailedError(self._connection_error_text(url, settings, exc)) from exc

        self._client = client
        self._endpoint_url = url
        self._discovery = discovery

        namespaces = await self._load_namespaces(client)
        identity = await self._read_server_identity(client)

        self._connection = ConnectionInfo(
            endpoint_url=url,
            security_policy_uri=settings.security_policy_uri,
            security_policy_label=policy_label(settings.security_policy_uri),
            security_mode=settings.security_mode,
            auth_mode=settings.auth_mode,
            namespaces=namespaces,
            application_uri=self._application_uri,
            session_timeout_ms=int(getattr(client, "session_timeout", 0) or 0) or None,
            server_application_name=discovery.server_application.get("application_name"),
            server_application_uri=discovery.server_application.get("application_uri"),
            product_uri=identity.get("product_uri"),
            manufacturer_name=identity.get("manufacturer_name"),
            software_version=identity.get("software_version"),
            build_number=identity.get("build_number"),
            server_certificate_sha256=endpoint.certificate_sha256,
            connected_at=utc_now(),
        )
        _logger.info(
            "Connected to %s using %s / %s (auth=%s, %d namespace(s))",
            url,
            policy_label(settings.security_policy_uri),
            settings.security_mode,
            settings.auth_mode,
            len(namespaces),
        )
        return self._connection

    @staticmethod
    def _connection_error_text(url: str, settings: ConnectionSettings, exc: Exception) -> str:
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        text = f"Connection to {url} failed ({detail})."
        if settings.is_secure:
            text += (
                " For secure connections, check that the client certificate contains the application URI "
                f"({settings.application_uri or 'urn:<host>:OPCUAExplorer:Client'}) and that the server trusts it."
            )
        elif not settings.endpoint_url.lower().startswith("opc.tcp://"):
            text += " Check that the endpoint scheme is opc.tcp://."
        return text

    async def _apply_security(self, client: Client, settings: ConnectionSettings) -> None:
        policy = policy_class(settings.security_policy_uri)
        if policy is None:
            raise ConnectionFailedError(f"Unsupported security policy: {settings.security_policy_uri}")
        if not settings.client_certificate_path or not settings.client_private_key_path:
            raise ConnectionFailedError("A secure connection requires a client certificate and private key.")
        await client.set_security(
            policy,
            settings.client_certificate_path,
            settings.client_private_key_path,
            settings.private_key_password,
            settings.server_certificate_der,
            mode_from_name(settings.security_mode),
        )

    def _apply_identity(self, client: Client, settings: ConnectionSettings) -> None:
        if settings.auth_mode == "username":
            client.set_user(settings.username or "")
            if settings.password is not None:
                client.set_password(settings.password)
        elif settings.auth_mode == "certificate":
            # The user certificate is loaded asynchronously below.
            pass
        elif settings.auth_mode != "anonymous":
            raise ConnectionFailedError(f"Unknown authentication mode: {settings.auth_mode}")

    async def _apply_certificate_identity(self, client: Client, settings: ConnectionSettings) -> None:
        if settings.auth_mode != "certificate":
            return
        if not settings.client_certificate_path or not settings.client_private_key_path:
            raise ConnectionFailedError("Certificate authentication requires a client certificate and private key.")
        await client.load_client_certificate(settings.client_certificate_path)
        await client.load_private_key(
            Path(settings.client_private_key_path),
            settings.private_key_password,
        )

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
        await self._safe_disconnect(client)

    @staticmethod
    async def _safe_disconnect(client: Client) -> None:
        try:
            await client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("Disconnecting the OPC UA session reported: %s", exc)

    async def shutdown(self) -> None:
        """Release session, live-monitor subscription and client resources."""
        await self.disconnect()

    # -- diagnostics -------------------------------------------------------
    async def _load_namespaces(self, client: Client) -> list[NamespaceEntry]:
        try:
            array = await client.get_namespace_array()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("Reading the server namespace array failed: %s", exc)
            return []
        self._namespaces = [NamespaceEntry(index=index, uri=uri) for index, uri in enumerate(array)]
        self._namespace_index = {entry.index: entry.uri for entry in self._namespaces}
        return list(self._namespaces)

    async def _read_server_identity(self, client: Client) -> dict[str, str]:
        """Best-effort read of the standard ServerStatus/BuildInfo variables."""
        session = getattr(client.uaclient, "session", None)
        if session is None:
            return {}
        specs = [(ua.NodeId(node_id).to_string(), AttributeSpec(ua.AttributeIds.Value, name)) for name, node_id in _SERVER_IDENTITY_IDS.items()]
        try:
            results = await read_attributes_bulk(session, specs, chunk_size=16)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.debug("Reading server build information failed: %s", exc)
            return {}

        identity: dict[str, str] = {}
        for key, node_id in _SERVER_IDENTITY_IDS.items():
            result = results.get((ua.NodeId(node_id).to_string(), key))
            if result is None or not result.ok or result.value is None:
                continue
            identity[key] = str(result.value)
        return identity

    async def refresh_namespaces(self) -> list[NamespaceEntry]:
        client = self._require_client()
        namespaces = await self._load_namespaces(client)
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
        """Read raw history of one variable; no HistoryUpdate and no retry."""
        session = self._require_session()
        return await read_history_impl(session, node_id, config)

    # -- browsing ----------------------------------------------------------
    async def browse_children(
        self, node_id: Any, *, follow_subtypes: bool = True, options: BrowseOptions | None = None
    ) -> list[ChildEntry]:
        session = self._require_session()
        return await browse_children_impl(
            session,
            _normalise_node_id(node_id),
            follow_subtypes=follow_subtypes,
            options=options,
        )

    async def resolve_browse_paths(self, requests: list[PathRequest]) -> list[PathResolution]:
        """Resolve browse paths with TranslateBrowsePathsToNodeIds on the current session.

        Each request yields one resolution; ``PathResolution.ok`` says whether exactly one target
        was found. Nothing is discovered by guessing - ambiguous paths are reported, not picked.
        """
        session = self._require_session()
        return await resolve_paths(session, requests)

    async def read_node_details(self, node_id: Any) -> NodeDetails:
        """Read all applicable attributes plus references of one node."""
        session = self._require_session()
        node_id_string = _normalise_node_id(node_id)

        node_class_result = await read_attributes_bulk(
            session, [(node_id_string, AttributeSpec(ua.AttributeIds.NodeClass, "NodeClass"))]
        )
        class_result = node_class_result.get((node_id_string, "NodeClass"))
        node_class = str(class_result.value) if class_result and class_result.ok and class_result.value else "Unspecified"

        record, attributes = await read_node_attributes(
            session,
            node_id_string,
            node_class=node_class,
            include_value=True,
            extended=True,
            namespace_uri_of=self._namespace_index.get,
        )

        references = await self._read_references(session, node_id_string, record)

        # Human-readable names for the DataType and TypeDefinition columns.
        wanted = {item for item in (record.data_type, record.type_definition) if item}
        if wanted:
            try:
                names = await resolve_browse_names(session, wanted)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - cosmetic only
                _logger.debug("Resolving type names for %s failed: %s", node_id_string, exc)
                names = {}
            if record.data_type:
                record.data_type_name = names.get(record.data_type)
            if record.type_definition:
                record.type_definition_name = names.get(record.type_definition)

        return NodeDetails(
            record=record,
            attributes=self._detail_attributes(record, attributes),
            references=references,
        )

    @staticmethod
    def _detail_attributes(record: Any, attributes: list[Any]) -> list[Any]:
        """Add values that come from browsing rather than from a Read service call.

        NodeId and the namespace URI are known from the browse result, and the
        resolved type names are looked up separately; showing them next to the
        read attributes makes the details panel match the export fields.
        """
        from ..models import AttributeResult

        leading = [
            AttributeResult(attribute="NodeId", value=record.node_id),
            AttributeResult(attribute="NamespaceUri", value=record.namespace_uri or "(not in namespace table)"),
        ]
        trailing: list[AttributeResult] = []
        if record.type_definition:
            label = record.type_definition_name or "unknown"
            trailing.append(
                AttributeResult(attribute="TypeDefinition", value=f"{label} ({record.type_definition})")
            )
        if record.data_type and record.data_type_name:
            trailing.append(AttributeResult(attribute="DataTypeName", value=record.data_type_name))
        return leading + list(attributes) + trailing

    async def _read_references(self, session: Any, node_id: str, record: Any) -> list[ReferenceInfo]:
        outcomes = await browse_many(
            session,
            [node_id],
            reference_type=ua.ObjectIds.References,
            direction=ua.BrowseDirection.Both,
            include_subtypes=True,
            batch_size=1,
        )
        if not outcomes:
            return []
        outcome = outcomes[0]
        if not outcome.ok:
            record.errors.append(f"Browsing references failed: {outcome.status}")
            return []

        reference_type_ids = {reference.ReferenceTypeId.to_string() for reference in outcome.references}
        names = await resolve_browse_names(session, reference_type_ids) if reference_type_ids else {}

        infos: list[ReferenceInfo] = []
        for reference in outcome.references:
            reference_type_id = reference.ReferenceTypeId.to_string()
            target_id = reference.NodeId.to_string()

            if reference.IsForward and reference.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasTypeDefinition):
                record.type_definition = target_id
                record.type_definition_name = reference.BrowseName.Name if isinstance(reference.BrowseName, ua.QualifiedName) else None

            infos.append(
                ReferenceInfo(
                    reference_type=reference_type_id,
                    reference_type_name=names.get(reference_type_id),
                    direction="Forward" if reference.IsForward else "Inverse",
                    target_node_id=target_id,
                    target_browse_name=(
                        reference.BrowseName.Name if isinstance(reference.BrowseName, ua.QualifiedName) else ""
                    ),
                    target_display_name=(
                        reference.DisplayName.Text if isinstance(reference.DisplayName, ua.LocalizedText) else ""
                    ),
                    target_node_class=node_class_name(reference.NodeClass),
                )
            )

        infos.sort(
            key=lambda info: (info.direction != "Forward", info.reference_type_name or info.reference_type, info.target_display_name)
        )
        return infos

    async def read_value(self, node_id: Any) -> ValueSnapshot:
        session = self._require_session()
        node_id_string = _normalise_node_id(node_id)
        snapshots = await read_values_bulk(session, [node_id_string])
        snapshot = snapshots.get(node_id_string)
        if snapshot is None:  # pragma: no cover - read_values_bulk always returns an entry
            return ValueSnapshot(value=None, status="BadNoResult", encoding="unavailable")
        return snapshot

    async def read_value_with_options(
        self, node_id: Any, options: ValueReadOptions | None = None
    ) -> ValueReadResult:
        """Read the Value attribute once with IndexRange, MaxAge and TimestampsToReturn."""
        session = self._require_session()
        target = _normalise_node_id(node_id)
        return await read_value_with_options_impl(
            session, target, (options or ValueReadOptions()).normalised()
        )

    # -- live monitoring ---------------------------------------------------
    @property
    def is_monitoring(self) -> bool:
        return self._monitor is not None and self._monitor.is_active

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

        await self.monitor_stop()
        self._monitor = LiveMonitor(client, emit)
        try:
            start = await self._monitor.start(ids, config)
        except BaseException:
            self._monitor = None
            raise
        _logger.info(
            "Live monitor started: subscription %s, %d of %d item(s)",
            start.subscription_id,
            sum(1 for item in start.items if item.ok),
            len(start.items),
        )
        return start

    async def monitor_add(self, node_ids: list[Any]) -> list[MonitoredItemInfo]:
        """Subscribe more nodes to the running monitor subscription."""
        monitor = self._monitor
        if monitor is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")
        ids = [_normalise_node_id(node_id) for node_id in node_ids]
        if not ids:
            return []
        return await monitor.add(ids)

    async def monitor_remove(self, handles: list[int]) -> None:
        """Delete the given monitored items from the running subscription."""
        monitor = self._monitor
        if monitor is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")
        await monitor.remove(handles)

    async def monitor_stop(self) -> None:
        """Delete the live-monitor subscription; safe to call when none runs."""
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            await monitor.stop()

    async def monitor_set_mode(self, mode: str) -> str:
        """Apply ``SetMonitoringMode`` (Reporting/Sampling/Disabled) to the monitor."""
        monitor = self._monitor
        if monitor is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")
        return await monitor.set_monitoring_mode(mode)

    async def monitor_apply_intervals(
        self, *, publishing_interval_ms: float, sampling_interval_ms: float
    ) -> tuple[float, list[str]]:
        """Modify the running subscription's publishing and sampling intervals.

        Returns the publishing interval the server revised to and any per-item
        refusal texts; nothing is changed when the monitor is not running.
        """
        monitor = self._monitor
        if monitor is None:
            raise MonitorNotActiveError("No live monitor is running. Start monitoring first.")
        revised = await monitor.set_publishing_interval(publishing_interval_ms)
        failures = await monitor.set_sampling_interval(sampling_interval_ms)
        return revised, failures

    # -- event monitoring --------------------------------------------------
    @property
    def is_monitoring_events(self) -> bool:
        return self._events is not None and self._events.is_active

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

        await self.event_monitor_stop()
        self._events = LiveEvents(client, emit)
        try:
            start = await self._events.start(ids, config)
        except BaseException:
            self._events = None
            raise
        _logger.info(
            "Event subscription started: subscription %s, %d of %d source(s) accepted",
            start.subscription_id,
            sum(1 for source in start.sources if source.ok),
            len(start.sources),
        )
        return start

    async def event_monitor_add(self, source_node_ids: list[Any]) -> list[EventSourceInfo]:
        """Listen to events of more source nodes on the running event subscription."""
        events = self._events
        if events is None:
            raise MonitorNotActiveError("No event subscription is running. Start event monitoring first.")
        ids = [_normalise_node_id(node_id) for node_id in source_node_ids]
        if not ids:
            return []
        return await events.add(ids)

    async def event_monitor_remove(self, handles: list[int]) -> None:
        """Stop listening to the given event sources."""
        events = self._events
        if events is None:
            raise MonitorNotActiveError("No event subscription is running. Start event monitoring first.")
        await events.remove(handles)

    async def event_monitor_stop(self) -> None:
        """Delete the event subscription; safe to call when none runs."""
        events, self._events = self._events, None
        if events is not None:
            await events.stop()

    async def acknowledge_condition(
        self,
        condition_node_id: str,
        event_id_hex: str,
        comment: str,
        *,
        reference: str = "",
    ) -> CallOutcome:
        """Acknowledge one condition event: one ``Acknowledge`` call, exactly once.

        The operator dialog is the authority; there is no retry, no automatic
        comment and no other condition method (``Confirm``/``Enable``) is called.
        """
        client = self._require_client()
        self._require_session()
        event_id = event_id_bytes(event_id_hex)
        if event_id is None:
            raise OpcUaExplorerError(
                "The event has no usable EventId; nothing was acknowledged."
            )
        authorization = WriteAuthority(
            AUTHORITY_OPERATOR_DIALOG, reference or f"acknowledge {condition_node_id}"
        )
        return await acknowledge_condition_impl(
            client, condition_node_id, event_id, comment, authorization=authorization
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

    # -- certificate approval ---------------------------------------------
    def certificate_is_trusted(self, certificate_der: bytes | None) -> bool:
        if not certificate_der:
            return False
        return self.trust_store.is_trusted(_sha256_hex(certificate_der))

    def approve_certificate(self, certificate_der: bytes, *, subject: str, endpoint_url: str, not_after: str | None) -> None:
        """Record an explicit operator approval of a server certificate."""
        self.trust_store.add(
            _sha256_hex(certificate_der),
            subject=subject,
            endpoint_url=endpoint_url,
            not_after=not_after,
        )

    def describe_certificate(self, certificate_der: bytes | None) -> Any:
        return summarize_certificate(certificate_der)

