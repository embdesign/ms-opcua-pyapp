"""Discovery, connection establishment and namespace loading.

``OpcUaService`` still owns the session state and the public methods; the
procedure that opens a client, applies security/identity and builds the
``ConnectionInfo`` lives here. Keeping it out of the façade makes the connect
flow reviewable on its own and leaves the service as a thin state holder.

Security invariants are unchanged: only the requested (policy, mode) pair is
used, the operator-approved DER is handed to ``set_security``, and the approved
fingerprint must still match the advertised certificate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asyncua import Client, ua

from ..config import ConnectionSettings
from ..errors import ConnectionFailedError, OpcUaExplorerError
from ..models import (
    ConnectionInfo,
    DiscoveredServer,
    DiscoveryResult,
    EndpointInfo,
    NamespaceEntry,
    ServerDiscoveryResult,
    utc_now,
)
from .attributes import AttributeSpec, read_attributes_bulk
from .security import endpoint_to_info, mode_from_name, policy_class, policy_label

CLIENT_NAME = "OPC UA Explorer"

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


def normalise_url(endpoint_url: str) -> str:
    url = (endpoint_url or "").strip()
    if not url:
        raise ConnectionFailedError("An endpoint URL is required, for example opc.tcp://192.168.0.10:4840.")
    if "://" not in url:
        url = "opc.tcp://" + url
    return url


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True)
class OpenedSession:
    """Everything ``OpcUaService.connect`` assigns after a successful open."""

    client: Client
    connection: ConnectionInfo
    namespaces: list[NamespaceEntry]
    namespace_index: dict[int, str]


async def _log_connection_loss(exc: Exception) -> None:
    """asyncua supervisor callback: record the loss before it retries or gives up."""
    _logger.warning("Connection to the OPC UA server was lost: %s", exc)


async def discover(
    endpoint_url: str,
    *,
    application_uri: str,
    request_timeout: float = 10.0,
    connect_timeout: float | None = None,
) -> DiscoveryResult:
    """Read the advertised endpoint list without authenticating."""
    url = normalise_url(endpoint_url)
    client = Client(url, timeout=float(request_timeout))
    client.name = CLIENT_NAME
    client.application_uri = application_uri
    timeout = float(connect_timeout if connect_timeout is not None else max(request_timeout, 5.0))

    try:
        raw_endpoints = await asyncio.wait_for(_fetch_endpoints(client), timeout=timeout)
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
    result = DiscoveryResult(
        endpoint_url=url,
        endpoints=endpoints,
        server_application=_describe_application(raw_endpoints[0].Server),
    )
    _logger.info(
        "Discovered %d endpoint(s) at %s (%s)",
        len(endpoints),
        url,
        result.server_application.get("application_name") or "unknown server",
    )
    return result


async def discover_servers(
    discovery_url: str,
    *,
    application_uri: str,
    server_uris: Iterable[str] = (),
    request_timeout: float = 10.0,
    connect_timeout: float | None = None,
) -> ServerDiscoveryResult:
    """Ask a discovery endpoint which servers it knows about (FindServers)."""
    url = normalise_url(discovery_url)
    wanted = [str(uri).strip() for uri in server_uris if str(uri).strip()]
    client = Client(url, timeout=float(request_timeout))
    client.name = CLIENT_NAME
    client.application_uri = application_uri
    timeout = float(connect_timeout if connect_timeout is not None else max(request_timeout, 5.0))
    started = time.monotonic()

    try:
        descriptions = await asyncio.wait_for(_fetch_find_servers(client, wanted), timeout=timeout)
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

    servers = [_describe_discovered_server(description) for description in descriptions]
    servers.sort(key=lambda item: (item.application_name.lower(), item.application_uri))
    result = ServerDiscoveryResult(
        discovery_url=url,
        server_uris=wanted,
        servers=servers,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )
    _logger.info("FindServers at %s reported %d application(s)", url, len(servers))
    return result


async def _fetch_find_servers(client: Client, server_uris: list[str]) -> list[ua.ApplicationDescription]:
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


async def _fetch_endpoints(client: Client) -> list[ua.EndpointDescription]:
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


async def open_session(
    settings: ConnectionSettings,
    *,
    application_uri: str,
    discovery: DiscoveryResult,
    endpoint: EndpointInfo,
) -> OpenedSession:
    """Create the client for ``endpoint``, connect and read the session facts.

    The caller selects the endpoint (which raises instead of downgrading) and
    has already validated/approved the certificate.
    """
    url = normalise_url(settings.endpoint_url)
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
    client.application_uri = application_uri
    client.session_timeout = int(settings.session_timeout_ms)
    client.secure_channel_timeout = int(settings.secure_channel_timeout_ms)
    client.connection_lost_callback = _log_connection_loss

    try:
        if settings.is_secure:
            await _apply_security(client, settings)
        _apply_identity(client, settings)
        await _apply_certificate_identity(client, settings)
        try:
            await asyncio.wait_for(client.connect(), timeout=float(settings.connect_timeout))
        except TimeoutError as exc:
            raise ConnectionFailedError(
                f"Connecting to {url} timed out after {settings.connect_timeout:.1f} s."
            ) from exc
    except asyncio.CancelledError:
        await safe_disconnect(client)
        raise
    except OpcUaExplorerError:
        await safe_disconnect(client)
        raise
    except Exception as exc:
        await safe_disconnect(client)
        raise ConnectionFailedError(_connection_error_text(url, settings, exc)) from exc

    namespaces, namespace_index = await load_namespaces(client)
    identity = await _read_server_identity(client)

    connection = ConnectionInfo(
        endpoint_url=url,
        security_policy_uri=settings.security_policy_uri,
        security_policy_label=policy_label(settings.security_policy_uri),
        security_mode=settings.security_mode,
        auth_mode=settings.auth_mode,
        namespaces=namespaces,
        application_uri=application_uri,
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
    return OpenedSession(
        client=client,
        connection=connection,
        namespaces=namespaces,
        namespace_index=namespace_index,
    )


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


async def _apply_security(client: Client, settings: ConnectionSettings) -> None:
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


def _apply_identity(client: Client, settings: ConnectionSettings) -> None:
    if settings.auth_mode == "username":
        client.set_user(settings.username or "")
        if settings.password is not None:
            client.set_password(settings.password)
    elif settings.auth_mode == "certificate":
        # The user certificate is loaded asynchronously below.
        pass
    elif settings.auth_mode != "anonymous":
        raise ConnectionFailedError(f"Unknown authentication mode: {settings.auth_mode}")


async def _apply_certificate_identity(client: Client, settings: ConnectionSettings) -> None:
    if settings.auth_mode != "certificate":
        return
    if not settings.client_certificate_path or not settings.client_private_key_path:
        raise ConnectionFailedError("Certificate authentication requires a client certificate and private key.")
    await client.load_client_certificate(settings.client_certificate_path)
    await client.load_private_key(
        Path(settings.client_private_key_path),
        settings.private_key_password,
    )


async def load_namespaces(client: Client) -> tuple[list[NamespaceEntry], dict[int, str]]:
    """Read the namespace array; a failed read yields an empty table, never an exception."""
    try:
        array = await client.get_namespace_array()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _logger.warning("Reading the server namespace array failed: %s", exc)
        return [], {}
    namespaces = [NamespaceEntry(index=index, uri=uri) for index, uri in enumerate(array)]
    return namespaces, {entry.index: entry.uri for entry in namespaces}


async def _read_server_identity(client: Client) -> dict[str, str]:
    """Best-effort read of the standard ServerStatus/BuildInfo variables."""
    session = getattr(client.uaclient, "session", None)
    if session is None:
        return {}
    specs = [
        (ua.NodeId(node_id).to_string(), AttributeSpec(ua.AttributeIds.Value, name))
        for name, node_id in _SERVER_IDENTITY_IDS.items()
    ]
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


async def safe_disconnect(client: Client) -> None:
    """Disconnect without masking an in-flight cancellation."""
    try:
        await client.disconnect()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _logger.warning("Disconnecting the OPC UA session reported: %s", exc)


__all__ = [
    "CLIENT_NAME",
    "OpenedSession",
    "discover",
    "discover_servers",
    "load_namespaces",
    "normalise_url",
    "open_session",
    "safe_disconnect",
]
