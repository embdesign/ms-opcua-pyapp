"""Functional test runner.

Design rules (from the project brief):

* The default suite is **read-only**. No value is written, no method is called.
* Writes and method calls exist only as **guarded** checks: one explicitly
  configured target each, validated against the server's DataType /
  ``InputArguments``, off unless ``TestConfig.allow_writes`` is set, and never
  discovered, scanned or repeated.
* A check that cannot run reports ``skipped`` with a reason; it never pretends to
  have run and never fails the server for a client-side precondition.
* Creating a subscription and *receiving a data change* are two separate checks.
  A value that does not change is **not** a failure: the monitor reports its
  initial value, and "no change within the observation window" is skipped with an
  explanation.
* The connection-interruption check is opt-in because it closes the secure
  channel and reconnects, which interrupts active subscriptions.
* Latency is a small, bounded sample of ordinary reads - not a benchmark.

All checks share one existing session, so nothing here re-authenticates or
changes the security configuration.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from asyncua import ua
from asyncua.common.subscription import DataChangeNotificationHandlerAsync

from ..models import (
    FUNCTIONAL_TEST_DISCLAIMER,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    ConnectionInfo,
    DiscoveryResult,
    TestCheck,
    TestConfig,
    TestProgress,
    TestRun,
    utc_now,
)
from .attributes import AttributeSpec, format_attribute_value, read_attributes_bulk, read_values_bulk, status_name
from .browse import browse_many
from .check_support import CheckSkipped, environment_info
from .check_support import preview_value as _preview
from .guarded_operations import check_method_call, check_write_target

_logger = logging.getLogger(__name__)

ProgressCallback = Callable[[TestProgress], None]

#: Static, honest scope statement included in every run and report.
COVERAGE_LIMITATIONS: tuple[str, ...] = (
    FUNCTIONAL_TEST_DISCLAIMER,
    "The suite exercises one session, one browse root and one variable; it does not verify the whole address "
    "space or every DataType.",
    "Writes and method calls are off by default and, when enabled, run only against the single targets the "
    "operator configured and confirmed. No writable node is discovered or written automatically.",
    "Subscription checks record the initial value plus any changes within the observation window; a value "
    "that does not change is reported as passed with an explicit note, not as a failure.",
    "Latency figures are client-side round-trip measurements of a few reads; they are not a performance "
    "benchmark and do not include server-internal processing time.",
    "Access-denied nodes, timeouts and unsupported DataTypes are reported as limitations, not silently ignored.",
)

_CORE_ATTRIBUTES = ("NodeClass", "BrowseName", "DisplayName")
_BROWSE_NAME_SPEC = AttributeSpec(ua.AttributeIds.BrowseName, "BrowseName")
_ATTRIBUTE_SPECS: tuple[AttributeSpec, ...] = (
    AttributeSpec(ua.AttributeIds.NodeClass, "NodeClass"),
    AttributeSpec(ua.AttributeIds.BrowseName, "BrowseName"),
    AttributeSpec(ua.AttributeIds.DisplayName, "DisplayName"),
    AttributeSpec(ua.AttributeIds.Description, "Description"),
)


class _RecordingHandler(DataChangeNotificationHandlerAsync):
    """Collects data-change notifications without ever blocking the publish loop."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []
        self.event = asyncio.Event()

    async def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        entry: dict[str, Any] = {"node_id": getattr(getattr(node, "nodeid", None), "to_string", lambda: "")()}
        entry["value"] = _preview(val)
        try:
            entry["status"] = status_name(data.monitored_item.Value.StatusCode)
        except Exception:  # pragma: no cover - structure of data varies by server
            entry["status"] = "unknown"
        self.notifications.append(entry)
        self.event.set()


@dataclass(frozen=True, slots=True)
class _CheckDefinition:
    id: str
    name: str
    category: str
    method: str


_CHECKS: tuple[_CheckDefinition, ...] = (
    _CheckDefinition("endpoint_discovery", "Endpoint discovery", "connection", "_check_endpoint_discovery"),
    _CheckDefinition("session_state", "Session state & authentication", "connection", "_check_session_state"),
    _CheckDefinition("namespace_array", "Namespace array", "connection", "_check_namespace_array"),
    _CheckDefinition("browse_root", "Browse root", "browse", "_check_browse_root"),
    _CheckDefinition("browse_target", "Browse target node", "browse", "_check_browse_target"),
    _CheckDefinition("attribute_reads", "Attribute reads", "read", "_check_attribute_reads"),
    _CheckDefinition("variable_read", "Variable read & StatusCode", "read", "_check_variable_read"),
    _CheckDefinition("subscription_create", "Subscription creation", "subscription", "_check_subscription_create"),
    _CheckDefinition(
        "subscription_data_change", "Monitored-item notifications", "subscription", "_check_subscription_data_change"
    ),
    _CheckDefinition("latency", "Bounded read latency", "latency", "_check_latency"),
    _CheckDefinition("connection_recovery", "Connection loss & recovery", "resilience", "_check_connection_recovery"),
)


class FunctionalTestRunner:
    """Runs the bounded check suite against an existing session.

    The default checks are read-only; configured write/method targets add guarded
    checks that run only when ``TestConfig.allow_writes`` is enabled.
    """

    def __init__(
        self,
        client: Any,
        *,
        connection: ConnectionInfo | None,
        config: TestConfig,
        endpoint_url: str,
        discover: Callable[[], Any] | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self._client = client
        self._connection = connection
        self._config = config.normalised()
        self._endpoint_url = endpoint_url
        self._discover = discover
        self._progress = progress

        self._subscription: Any | None = None
        self._handler: _RecordingHandler | None = None
        self._monitored_handles: list[int] = []
        self._variable_node_id: str | None = self._config.variable_node_id
        self._variable_source: str = "configured" if self._variable_node_id else ""
        self._variable_lookup_done = bool(self._variable_node_id)
        self._inaccessible: dict[str, str] = {}

    # -- accessors ---------------------------------------------------------
    @property
    def session(self) -> Any:
        """Always the *current* session: a reconnect replaces the token inside it."""
        return self._client.uaclient.session

    def _emit(self, phase: str, message: str, current: int, total: int) -> None:
        if self._progress is None:
            return
        try:
            self._progress(TestProgress(phase=phase, message=message, current=current, total=total))
        except Exception:  # pragma: no cover - a GUI callback must not break the run
            _logger.debug("Test progress callback failed", exc_info=True)

    # -- run ---------------------------------------------------------------
    def _plan(self) -> list[tuple[_CheckDefinition, Callable[[TestCheck], Awaitable[None]]]]:
        """Static read-only checks plus one check per configured guarded operation.

        Configured write/method targets always produce a check, even when writes are
        disabled, so the report shows that they were *not* run and why.
        """
        plan: list[tuple[_CheckDefinition, Callable[[TestCheck], Awaitable[None]]]] = [
            (definition, getattr(self, definition.method)) for definition in _CHECKS
        ]
        for index, target in enumerate(self._config.write_targets, start=1):
            plan.append(
                (
                    _CheckDefinition(
                        f"write_value_{index}",
                        f"Guarded write: {target.node_id}",
                        "write",
                        "_",
                    ),
                    functools.partial(check_write_target, self._client, self._config, target=target),
                )
            )
        for index, call in enumerate(self._config.method_calls, start=1):
            plan.append(
                (
                    _CheckDefinition(
                        f"method_call_{index}",
                        f"Guarded method call: {call.method_node_id} on {call.object_node_id}",
                        "write",
                        "_",
                    ),
                    functools.partial(check_method_call, self._client, self._config, call=call),
                )
            )
        return plan

    async def run(self) -> TestRun:
        run = TestRun(
            endpoint_url=self._endpoint_url,
            config=self._config,
            connection=self._connection,
            environment=environment_info(),
            coverage_limitations=list(COVERAGE_LIMITATIONS),
        )

        plan = self._plan()
        total = len(plan)
        try:
            for index, (definition, execute) in enumerate(plan, start=1):
                self._emit("running", definition.name, index - 1, total)
                check = TestCheck(id=definition.id, name=definition.name, category=definition.category)
                started = time.perf_counter()
                try:
                    await execute(check)
                    if check.status not in (STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED):
                        check.status = STATUS_FAILED
                        check.detail = check.detail or "The check did not report a result."
                except CheckSkipped as exc:
                    check.status = STATUS_SKIPPED
                    check.detail = str(exc) or "Skipped"
                except asyncio.CancelledError:
                    check.status = STATUS_SKIPPED
                    check.detail = "Cancelled before the check completed."
                    raise
                except Exception as exc:
                    check.status = STATUS_FAILED
                    check.error = f"{type(exc).__name__}: {exc}"
                    check.detail = check.detail or check.error
                    _logger.warning("Functional check %s failed: %s", check.id, check.error)
                finally:
                    check.duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    run.checks.append(check)
                self._emit("running", f"{definition.name} finished", index, total)
        except asyncio.CancelledError:
            run.cancelled = True
            run.notes.append("The test run was cancelled; the results shown are partial.")
            _logger.info("Functional test run cancelled after %d check(s)", len(run.checks))
        finally:
            await self._cleanup_subscription(run)
            run.inaccessible_nodes = dict(self._inaccessible)
            run.finished_at = utc_now()
            self._emit("complete", "Test run finished", total, total)

        if self._variable_node_id:
            run.notes.append(
                f"Target variable: {self._variable_node_id} ({self._variable_source or 'configured'})."
            )
        else:
            run.notes.append(
                "No variable was configured and none could be discovered; value, subscription-notification and "
                "value-latency checks were skipped."
            )
        if self._config.write_targets or self._config.method_calls:
            if self._config.allow_writes:
                run.notes.append(
                    f"Guarded operations: {len(self._config.write_targets)} write(s) and "
                    f"{len(self._config.method_calls)} method call(s) were configured and confirmed by the "
                    "operator. Exactly these targets were touched; nothing was discovered, scanned or repeated."
                )
            else:
                run.notes.append(
                    f"{len(self._config.write_targets)} write(s) and {len(self._config.method_calls)} method "
                    "call(s) were configured but writes are disabled, so none of them was performed."
                )
        else:
            run.notes.append("No write or method call was configured, so the run was read-only.")
        return run

    async def _cleanup_subscription(self, run: TestRun) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is None:
            return
        try:
            if self._monitored_handles:
                await subscription.unsubscribe(self._monitored_handles)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            run.notes.append(f"Deleting monitored items reported: {exc}")
        finally:
            self._monitored_handles = []
        try:
            await subscription.delete()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - transport may be gone after recovery check
            run.notes.append(f"Deleting the subscription reported: {exc}")
        self._handler = None

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _pass(check: TestCheck, detail: str) -> None:
        check.status = STATUS_PASSED
        check.detail = detail

    @staticmethod
    def _fail(check: TestCheck, detail: str) -> None:
        check.status = STATUS_FAILED
        check.detail = detail

    def _note_inaccessible(self, node_id: str, status: str) -> None:
        self._inaccessible[node_id] = status

    async def _target_variable(self) -> str:
        """Configured variable, or the first readable Variable found near the root."""
        if self._variable_node_id:
            return self._variable_node_id
        if self._variable_lookup_done:
            raise CheckSkipped("No variable node was configured and none was found near the browse root.")

        self._variable_lookup_done = True
        root = self._config.root_node_id
        queue: deque[str] = deque([root])
        seen: set[str] = {root}
        scanned = 0
        limit = self._config.max_browse_nodes

        while queue and scanned < limit:
            batch = [queue.popleft() for _ in range(min(10, len(queue)))]
            outcomes = await browse_many(
                self.session,
                batch,
                reference_type=ua.ObjectIds.HierarchicalReferences,
                direction=ua.BrowseDirection.Forward,
                include_subtypes=True,
                batch_size=len(batch),
            )
            for outcome in outcomes:
                scanned += 1
                if not outcome.ok:
                    self._note_inaccessible(outcome.node_id, outcome.status)
                    continue
                for reference in outcome.references:
                    if reference.NodeClass == ua.NodeClass.Variable:
                        self._variable_node_id = reference.NodeId.to_string()
                        self._variable_source = "discovered"
                        return self._variable_node_id
                    child_id = reference.NodeId.to_string()
                    if child_id not in seen and len(seen) < limit:
                        seen.add(child_id)
                        queue.append(child_id)
        raise CheckSkipped(
            f"No variable node was configured and none was found within {limit} nodes below {root}."
        )

    # -- checks ------------------------------------------------------------
    async def _check_endpoint_discovery(self, check: TestCheck) -> None:
        if self._discover is None:
            raise CheckSkipped("Endpoint discovery is not available for this session.")
        discovery: DiscoveryResult = await self._discover()
        check.data["endpoint_count"] = len(discovery.endpoints)
        check.data["endpoints"] = [endpoint.to_dict() for endpoint in discovery.endpoints]
        if not discovery.endpoints:
            self._fail(check, "The server returned an empty endpoint list.")
            return

        if self._connection is None:
            self._pass(check, f"{len(discovery.endpoints)} endpoint(s) advertised.")
            return

        advertised = any(
            endpoint.security_policy_uri == self._connection.security_policy_uri
            and endpoint.security_mode == self._connection.security_mode
            for endpoint in discovery.endpoints
        )
        check.data["active_configuration_advertised"] = advertised
        check.data["active_security"] = (
            f"{self._connection.security_policy_label} / {self._connection.security_mode}"
        )
        if not advertised:
            self._fail(
                check,
                "The active security configuration is no longer advertised by the server.",
            )
            return
        self._pass(check, f"{len(discovery.endpoints)} endpoint(s); active configuration is advertised.")

    async def _check_session_state(self, check: TestCheck) -> None:
        state = str(getattr(self._client.state, "value", self._client.state))
        check.data["state"] = state
        check.data["session_timeout_ms"] = int(getattr(self._client, "session_timeout", 0) or 0)
        check.data["application_uri"] = getattr(self._client, "application_uri", None)
        check.data["authentication"] = self._connection.auth_mode if self._connection else "unknown"
        check.data["security_mode"] = self._connection.security_mode if self._connection else "unknown"
        if state != "connected":
            self._fail(check, f"The client state is {state!r}, not 'connected'.")
            return
        auth = check.data["authentication"]
        self._pass(
            check,
            f"Session active (identity: {auth}); session timeout {check.data['session_timeout_ms']} ms.",
        )

    async def _check_namespace_array(self, check: TestCheck) -> None:
        namespaces: list[str] = await self._client.get_namespace_array()
        check.data["count"] = len(namespaces)
        check.data["uris"] = namespaces[:20]
        has_standard = any(uri == "http://opcfoundation.org/UA/" for uri in namespaces)
        check.data["standard_namespace_present"] = has_standard
        if not namespaces:
            self._fail(check, "The server returned an empty namespace array.")
            return
        if not has_standard:
            self._fail(check, "Namespace 0 (http://opcfoundation.org/UA/) is missing.")
            return
        self._pass(check, f"{len(namespaces)} namespace(s); namespace 0 present.")

    async def _check_browse_root(self, check: TestCheck) -> None:
        root = self._config.root_node_id
        check.data["root"] = root
        outcomes = await browse_many(
            self.session,
            [root],
            reference_type=ua.ObjectIds.HierarchicalReferences,
            direction=ua.BrowseDirection.Forward,
            include_subtypes=True,
            batch_size=1,
        )
        if not outcomes:
            self._fail(check, "The browse request returned no result.")
            return
        outcome = outcomes[0]
        check.add_status("Browse", outcome.status, node_id=root)
        check.data["status"] = outcome.status
        check.data["children"] = len(outcome.references)
        check.data["child_names"] = [
            reference.BrowseName.Name if isinstance(reference.BrowseName, ua.QualifiedName) else ""
            for reference in outcome.references[:10]
        ]
        if not outcome.ok:
            self._note_inaccessible(root, outcome.status)
            self._fail(check, f"Browsing {root} returned {outcome.status}.")
            return
        self._pass(check, f"Browsing {root} succeeded ({len(outcome.references)} child reference(s)).")

    async def _check_browse_target(self, check: TestCheck) -> None:
        target = await self._target_variable()
        check.data["target"] = target
        outcomes = await browse_many(
            self.session,
            [target],
            reference_type=ua.ObjectIds.HierarchicalReferences,
            direction=ua.BrowseDirection.Forward,
            include_subtypes=True,
            batch_size=1,
        )
        if not outcomes:
            self._fail(check, f"Browsing {target} returned no result.")
            return
        outcome = outcomes[0]
        check.add_status("Browse", outcome.status, node_id=target)
        check.data["status"] = outcome.status
        check.data["children"] = len(outcome.references)
        if not outcome.ok:
            self._note_inaccessible(target, outcome.status)
            self._fail(check, f"Browsing {target} returned {outcome.status}.")
            return
        self._pass(check, f"Browsing {target} succeeded ({len(outcome.references)} child reference(s)).")

    async def _check_attribute_reads(self, check: TestCheck) -> None:
        try:
            target = await self._target_variable()
        except CheckSkipped:
            target = self._config.root_node_id
        check.data["target"] = target

        results = await read_attributes_bulk(
            self.session, [(target, spec) for spec in _ATTRIBUTE_SPECS], chunk_size=16
        )
        attributes: dict[str, Any] = {}
        for spec in _ATTRIBUTE_SPECS:
            result = results.get((target, spec.name))
            if result is None:
                attributes[spec.name] = {"status": "BadNoResult", "ok": False}
                check.add_status("Read " + spec.name, "BadNoResult", node_id=target)
                continue
            check.add_status("Read " + spec.name, result.status, node_id=target)
            attributes[spec.name] = {
                "status": result.status,
                "ok": result.ok,
                "value": format_attribute_value(result.value)[:200],
            }
        check.data["attributes"] = attributes

        missing = [name for name in _CORE_ATTRIBUTES if not attributes.get(name, {}).get("ok")]
        if missing:
            self._fail(check, f"Core attribute(s) could not be read: {', '.join(missing)}.")
            return
        self._pass(check, f"Core attributes read successfully from {target}.")

    async def _check_variable_read(self, check: TestCheck) -> None:
        target = await self._target_variable()
        check.data["target"] = target
        snapshots = await read_values_bulk(self.session, [target])
        snapshot = snapshots.get(target)
        if snapshot is None:  # pragma: no cover - read_values_bulk always returns an entry
            self._fail(check, "No read result was returned.")
            return

        check.add_status("Read Value", snapshot.status, node_id=target)
        check.data.update(
            {
                "status": snapshot.status,
                "variant_type": snapshot.variant_type,
                "encoding": snapshot.encoding,
                "source_timestamp": snapshot.source_timestamp,
                "server_timestamp": snapshot.server_timestamp,
                "value_preview": _preview(snapshot.value),
            }
        )
        if snapshot.encoding == "unavailable":
            self._fail(check, f"Reading the value returned {snapshot.status}.")
            return
        detail = f"Value read with status {snapshot.status}"
        if snapshot.variant_type:
            detail += f" ({snapshot.variant_type})"
        if snapshot.unsupported_encoding:
            detail += "; some parts of the value could not be encoded faithfully"
            check.notes.append("The value contains an encoding that is flagged in the export.")
        self._pass(check, detail + ".")

    async def _check_subscription_create(self, check: TestCheck) -> None:
        params = ua.CreateSubscriptionParameters()
        params.RequestedPublishingInterval = self._config.publishing_interval_ms
        params.RequestedLifetimeCount = 3000
        params.RequestedMaxKeepAliveCount = 10
        params.MaxNotificationsPerPublish = 100
        params.PublishingEnabled = True
        params.Priority = 0

        handler = _RecordingHandler()
        subscription = await self._client.create_subscription(params, handler)
        self._subscription = subscription
        self._handler = handler

        effective = getattr(subscription, "parameters", None)
        effective_interval = getattr(effective, "RequestedPublishingInterval", None)
        check.data.update(
            {
                "subscription_id": getattr(subscription, "subscription_id", None),
                "requested_publishing_interval_ms": float(params.RequestedPublishingInterval),
                "publishing_interval_ms": float(effective_interval) if effective_interval is not None else None,
                "requested_lifetime_count": int(params.RequestedLifetimeCount),
                "requested_max_keep_alive_count": int(params.RequestedMaxKeepAliveCount),
            }
        )
        self._pass(
            check,
            f"Subscription {check.data['subscription_id']} created "
            f"(publishing interval {check.data['publishing_interval_ms']} ms).",
        )

    async def _check_subscription_data_change(self, check: TestCheck) -> None:
        if self._subscription is None:
            raise CheckSkipped("No subscription is available because its creation did not succeed.")
        target = await self._target_variable()

        handles = await self._subscription.subscribe_data_change(
            self._client.get_node(target),
            sampling_interval=float(self._config.sampling_interval_ms),
        )
        if isinstance(handles, int):
            self._monitored_handles = [handles]
        else:
            self._monitored_handles = [handle for handle in handles if isinstance(handle, int)]
            rejected = [handle for handle in handles if not isinstance(handle, int)]
            if rejected:
                check.notes.append(
                    "The server returned a non-Good StatusCode for the monitored item; see the details."
                )
                for handle in rejected:
                    check.add_status("CreateMonitoredItem", status_name(getattr(handle, "value", handle)), node_id=target)

        check.data.update(
            {
                "target": target,
                "monitored_item_handles": list(self._monitored_handles),
                "sampling_interval_ms": float(self._config.sampling_interval_ms),
                "observation_window_seconds": float(self._config.notification_wait_seconds),
            }
        )

        received = await self._wait_for_notifications(2, self._config.notification_wait_seconds)
        handler = self._handler
        notifications = list(handler.notifications) if handler is not None else []
        check.data["notifications_received"] = received
        check.data["values"] = notifications[:5]

        for notification in notifications[:5]:
            check.add_status("DataChange", str(notification.get("status", "unknown")), node_id=target)

        if received == 0:
            check.status = STATUS_SKIPPED
            check.detail = (
                "The subscription and monitored item were created, but no notification arrived within "
                f"{self._config.notification_wait_seconds:.1f} s. This is not treated as a failure."
            )
            check.notes.append(
                "Some servers publish only on keep-alive intervals; consider increasing the observation window."
            )
            return

        changes = max(0, received - 1)
        check.data["value_changes_observed"] = changes
        if changes == 0:
            self._pass(
                check,
                "The monitored item reported its initial value; no value change occurred within the "
                "observation window (this is not a failure).",
            )
            return
        self._pass(check, f"{received} notification(s) received, {changes} value change(s) observed.")

    async def _wait_for_notifications(self, target: int, timeout: float) -> int:
        handler = self._handler
        if handler is None:  # pragma: no cover - guarded by the caller
            return 0
        deadline = time.monotonic() + timeout
        while len(handler.notifications) < target:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            handler.event.clear()
            try:
                await asyncio.wait_for(handler.event.wait(), timeout=remaining)
            except TimeoutError:
                break
        return len(handler.notifications)

    async def _resolve_probe_target(self) -> tuple[str, bool]:
        """Return ``(node_id, use_value)`` for latency/recovery read probes.

        Without a variable the probe reads BrowseName on the browse root, which is a
        legal attribute for every NodeClass, so the check still measures a real
        round trip instead of producing BadAttributeIdInvalid.
        """
        try:
            return await self._target_variable(), True
        except CheckSkipped:
            return self._config.root_node_id, False

    async def _probe_read(self, target: str, *, use_value: bool) -> str:
        if use_value:
            snapshots = await read_values_bulk(self.session, [target])
            snapshot = snapshots.get(target)
            return snapshot.status if snapshot is not None else "BadNoResult"
        results = await read_attributes_bulk(self.session, [(target, _BROWSE_NAME_SPEC)], chunk_size=1)
        result = results.get((target, "BrowseName"))
        return result.status if result is not None else "BadNoResult"

    async def _wait_for_recovery(self, target: str, *, use_value: bool) -> tuple[str, str]:
        """Wait until the reconnect supervisor restored the session and a read succeeds.

        With automatic reconnect enabled the supervisor owns the retry loop, so the
        check must not call ``connect()`` again; it observes the state instead.
        """
        deadline = time.monotonic() + max(10.0, self._config.request_timeout)
        state = str(getattr(self._client.state, "value", self._client.state))
        status = "unknown"
        while time.monotonic() < deadline:
            state = str(getattr(self._client.state, "value", self._client.state))
            if state == "connected":
                try:
                    status = await self._probe_read(target, use_value=use_value)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    status = type(exc).__name__
                if status == "Good":
                    return state, status
            await asyncio.sleep(0.1)
        return state, status

    async def _check_latency(self, check: TestCheck) -> None:
        target, use_value = await self._resolve_probe_target()
        samples: list[float] = []
        statuses: list[str] = []

        for _ in range(self._config.latency_samples):
            started = time.perf_counter()
            status = await self._probe_read(target, use_value=use_value)
            samples.append((time.perf_counter() - started) * 1000.0)
            statuses.append(status)

        check.data.update(
            {
                "target": target,
                "read_attribute": "Value" if use_value else "BrowseName",
                "samples": len(samples),
                "samples_ms": [round(value, 2) for value in samples],
                "min_ms": round(min(samples), 2),
                "median_ms": round(statistics.median(samples), 2),
                "max_ms": round(max(samples), 2),
                "statuses": sorted(set(statuses)),
            }
        )
        check.add_status("Read", statuses[-1], node_id=target)

        bad = [status for status in statuses if status != "Good"]
        if bad:
            self._fail(check, f"{len(bad)} of {len(samples)} read(s) did not return Good ({bad[0]}).")
            return
        self._pass(
            check,
            f"{len(samples)} read(s): median {check.data['median_ms']} ms, "
            f"min {check.data['min_ms']} ms, max {check.data['max_ms']} ms.",
        )

    async def _check_connection_recovery(self, check: TestCheck) -> None:
        if not self._config.include_channel_recovery:
            raise CheckSkipped(
                "Opt-in check: it closes the secure channel and reconnects, which interrupts active "
                "subscriptions. Enable 'include connection-interruption check' to run it."
            )
        try:
            target, use_value = await self._resolve_probe_target()
        except CheckSkipped:  # pragma: no cover - _resolve_probe_target never raises
            target, use_value = self._config.root_node_id, False

        before = str(getattr(self._client.state, "value", self._client.state))
        check.data["state_before"] = before
        if before != "connected":
            self._fail(check, f"The session was already {before!r} before the check started.")
            return

        await self._client.close_secure_channel()
        after_loss = str(getattr(self._client.state, "value", self._client.state))
        check.data["state_after_close"] = after_loss
        check.data["loss_detected"] = after_loss != "connected"

        read_after_loss = "unknown"
        try:
            await self._probe_read(target, use_value=use_value)
            read_after_loss = "unexpectedly succeeded"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            read_after_loss = type(exc).__name__
        check.data["read_after_loss"] = read_after_loss

        recovery_started = time.perf_counter()
        auto_reconnect = bool(getattr(self._client, "_auto_reconnect", False))
        check.data["automatic_reconnect"] = auto_reconnect
        if auto_reconnect:
            # The supervisor retries on its own; a second connect() would race with it.
            after_recovery, recovery_status = await self._wait_for_recovery(target, use_value=use_value)
            recovery_ms = round((time.perf_counter() - recovery_started) * 1000.0, 1)
        else:
            await self._client.connect()
            recovery_ms = round((time.perf_counter() - recovery_started) * 1000.0, 1)
            after_recovery = str(getattr(self._client.state, "value", self._client.state))
            recovery_status = await self._probe_read(target, use_value=use_value)

        check.data.update(
            {
                "recovery_ms": recovery_ms,
                "state_after_recovery": after_recovery,
                "read_after_recovery": recovery_status,
            }
        )
        check.add_status("Reconnect", after_recovery)
        check.add_status("Read", recovery_status, node_id=target)

        if after_recovery != "connected" or recovery_status != "Good":
            self._fail(
                check,
                f"Reconnect reached {after_recovery!r} and the follow-up read returned {recovery_status}.",
            )
            return
        if check.data["loss_detected"]:
            self._pass(
                check,
                f"Connection loss detected ({after_loss}); session re-established in {recovery_ms} ms and "
                "a read succeeded.",
            )
        else:
            self._pass(
                check,
                f"The channel was reopened without an observable disconnect; reconnect took {recovery_ms} ms "
                "and a read succeeded.",
            )


async def run_functional_tests(
    client: Any,
    *,
    connection: ConnectionInfo | None,
    config: TestConfig,
    endpoint_url: str,
    discover: Callable[[], Any] | None = None,
    progress: ProgressCallback | None = None,
) -> TestRun:
    """Convenience entry point used by :class:`OpcUaService`."""
    runner = FunctionalTestRunner(
        client,
        connection=connection,
        config=config,
        endpoint_url=endpoint_url,
        discover=discover,
        progress=progress,
    )
    return await runner.run()
