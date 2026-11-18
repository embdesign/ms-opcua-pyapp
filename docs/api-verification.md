# Verified asyncua API (as used by this project)

The brief requires verifying the real library API instead of inventing methods. Everything below was read from
the installed package (`asyncua 2.1.0`, Python 3.11) via `inspect`/source inspection, and is exercised by the
test suite.

Environment: Python 3.11.2 · asyncua 2.1.0 · PySide6 6.11.3 · cryptography 50.0.2 · uv 0.13.7.

## Client

| API | Verified behaviour |
|---|---|
| `Client(url, timeout=4, watchdog_intervall=1.0, auto_reconnect=False, …)` | `timeout` is the per-request timeout in seconds |
| Instance attributes | `application_uri`, `name`, `session_timeout` (ms), `secure_channel_timeout` (ms), `max_messagesize`, `max_chunkcount`, `security_policy`, `user_certificate`, `user_private_key` |
| `client.session` | **does not exist**; the session is `client.uaclient.session` (`UaSession`) |
| `client.state` | `UaClientState` (`disconnected`, `connecting`, `socket_open`, `channel_open`, `connected`, `reconnecting`, `disconnecting`) |
| `await client.connect(...)` / `await client.disconnect()` | `disconnect()` closes session, channel and socket; `disconnect_socket()` itself is synchronous |
| `await client.set_security(policy_class, certificate, private_key, private_key_password=None, server_certificate=None, mode=SignAndEncrypt, certificate_chain=None)` | **coroutine**. With `server_certificate=None` it opens a discovery channel and installs the server certificate it finds — this project always passes approved DER bytes to avoid that implicit trust |
| `load_client_certificate(path)` / `load_private_key(path, password=None)` | coroutines; set the *user* certificate for `ActivateSession` |
| `setup_self_signed_certificate(key_file, cert_file, subject_attrs=None, extended=None, host_name=None)` | coroutine; returns `(cert_file, key_file)`; certificate contains `application_uri` as a URI SAN |
| `get_endpoints()` | channel-level service, no session required |
| `connect_and_get_server_endpoints()` | `_with_temp_channel(self.get_endpoints)` — socket + Hello + OpenSecureChannel, then close |
| `find_endpoint(endpoints, security_mode, policy_uri)` | **staticmethod** |
| `get_namespace_array()`, `get_node()`, `get_root_node()`, `get_objects_node()` | as expected |
| `export_xml(nodes, path, export_values=False)` | wraps `XmlExporter`; does **not** collect dependencies |

## Node

| API | Verified behaviour |
|---|---|
| `get_children(refs=33, nodeclassmask=0)` | one Browse per node |
| `get_references(refs=31, direction=2, …)` | returns `ReferenceDescription` list |
| `get_children_descriptions(...)` | has **no** continuation-point parameter; follows `BrowseNext` internally with `ReleaseContinuationPoints=False` (never releases) |
| `read_attributes(attrs)` | returns `list[DataValue]`, does **not** raise on bad StatusCodes |
| `get_access_level()` | `ua.AccessLevel.parse_bitfield(int)`; `AccessLevel` enum values are **bit positions** (`CurrentRead = 0`, `CurrentWrite = 1`, …), not masks |
| `read_data_value(raise_on_bad_status=True)` / `read_value()` | as expected |

## Session (used directly for continuation points)

```python
UaSession.browse(ua.BrowseParameters)      -> list[ua.BrowseResult]
UaSession.browse_next(ua.BrowseNextParameters) -> list[ua.BrowseResult]
UaSession.read(ua.ReadParameters)          -> list[ua.DataValue]
```

* `ua.BrowseParameters: View, RequestedMaxReferencesPerNode, NodesToBrowse` — set
  `params.View.Timestamp = ua.get_win_epoch()` as asyncua does.
* `ua.BrowseResult: StatusCode, ContinuationPoint, References`.
* `ua.BrowseNextParameters: ReleaseContinuationPoints, ContinuationPoints`.
* `ua.ReadValueId: NodeId, AttributeId, IndexRange, DataEncoding`.

## Types and encodings

* `ua.StatusCode` is a small dataclass wrapper: `int(code)` fails, use `code.value`; `code.name` and
  `code.is_good()` work.
* `ua.ExpandedNodeId` **subclasses** `ua.NodeId` — test it first when dispatching.
* `ua.DataValue: Encoding, Value, StatusCode, SourceTimestamp, ServerTimestamp, SourcePicoseconds, ServerPicoseconds`;
  `DataValue.Value` is a `ua.Variant` (`.Value`, `.VariantType`, `.Dimensions`, `.is_array`).
* `ua.ExtensionObject(TypeId, Body)` is **frozen** and its constructor accepts only `TypeId` and `Body`;
  `Encoding` stays `0` even for a binary body, so the exporter derives the encoding code from the body type.
* `ua.LocalizedText: Encoding, Locale, Text`; `ua.QualifiedName: NamespaceIndex, Name` (positional order
  `QualifiedName(name, namespace_index)` works).
* Attribute ids: NodeId 1, NodeClass 2, BrowseName 3, DisplayName 4, Description 5, Value 13, DataType 14,
  ValueRank 15, ArrayDimensions 16, AccessLevel 17, UserAccessLevel 18.
* Attribute ids used by the details panel (verified 2026-11-16): WriteMask 6, UserWriteMask 7, IsAbstract 8,
  Symmetric 9, InverseName 10, ContainsNoLoops 11, EventNotifier 12, MinimumSamplingInterval 19, Historizing 20,
  DataTypeDefinition 23, RolePermissions 24, UserRolePermissions 25, AccessRestrictions 26. `NodeVersion` does
  not exist in asyncua 2.1.0's `AttributeIds`.
* Bit fields are modelled two different ways and must not be decoded with the same helper:
  `ua.AccessLevel` and `ua.EventNotifier` carry bit **positions** (`EventNotifier.SubscribeToEvents == 0`) and
  decode with `Enum.parse_bitfield(value)`; `ua.AttributeWriteMask`, `ua.AccessRestrictionType` and
  `ua.PermissionType` carry bit **masks** (`AttributeWriteMask.AccessLevel == 1`,
  `AttributeWriteMask.EventNotifier == 128`) and decode by testing `value & int(member)`.
  `ua.RolePermissionType` has `RoleId` (NodeId) and `Permissions` (PermissionType mask).
* ObjectIds used: References 31, HierarchicalReferences 33, HasChild 34, Organizes 35, HasTypeDefinition 40,
  HasSubtype 45, HasProperty 46, HasComponent 47.

## Security

* Policy classes live in `asyncua.crypto.security_policies`: `SecurityPolicyNone`, `Basic128Rsa15`, `Basic256`,
  `Basic256Sha256`, `Aes128Sha256RsaOaep`, `Aes256Sha256RsaPss`; each exposes `.URI` (no `.Mode`).
* URIs: `http://opcfoundation.org/UA/SecurityPolicy#None | #Basic128Rsa15 | #Basic256 | #Basic256Sha256 |
  #Aes128_Sha256_RsaOaep | #Aes256_Sha256_RsaPss`.
* `ua.MessageSecurityMode`: `Invalid=0, None_=1, Sign=2, SignAndEncrypt=3` (the member is `None_`).
* `ua.UserTokenType`: `Anonymous=0, UserName=1, Certificate=2, IssuedToken=3`.
* `ua.EndpointDescription: EndpointUrl, Server, ServerCertificate, SecurityMode, SecurityPolicyUri,
  UserIdentityTokens, TransportProfileUri, SecurityLevel`.
* `Client.set_security` calls `uacrypto.check_certificate(cert, application_uri, socket.gethostname())` on the
  *client* certificate: the certificate must contain the application URI, otherwise the call fails.

## Server (demo/tests only)

* `await Server.init()`, `server.set_endpoint(url)`, `server.set_endpoint` is synchronous; `set_server_name`
  synchronous; `await server.set_application_uri(uri)` is a **coroutine**; `await server.set_build_info(...)`.
* `await server.start()` / `await server.stop()`.
* `Server(user_manager=...)` → `InternalServer(user_manager=...)`; a `UserManager.get_user(...)` returning
  `None` makes the server answer `ActivateSession` with `BadUserAccessDenied`.
* The `certificate` argument of `get_user` is the secure-channel peer certificate; for `SecurityPolicy#None`
  it is an empty `bytes` value, so it must be tested for truthiness, not `is not None`.
* `create_custom_data_type(idx, name, basetype, properties=None, description=None)` is a coroutine; its
  `properties=` shortcut reuses the DataType's own NodeId for every member node and fails with
  `BadNodeIdExists` when `idx` is a `NodeId` — this project adds properties explicitly.
* `Node.add_method(nodeid, browsename, callback, [input variant types], [output variant types])`.
* `XmlExporter(server, export_values=False)` with `build_etree(node_list, add_all_namespaces=False)`,
  `build_etree_chunked(...)`, `write_xml(path, pretty=True)` — no `export_xml_node` helper.

## Subscription (used by the functional test suite)

* `await client.create_subscription(period: float | ua.CreateSubscriptionParameters, handler, publishing=True,
  queue_maxsize=1000, overflow=OverflowPolicy.DROP_OLDEST)` returns a `Subscription`; internally it creates the
  subscription, applies the server's revised parameters and registers the subscription on the client.
* `Subscription.parameters` holds the **effective** parameters after the server's revision: `create_subscription`
  applies the revised values through `update(new_params)`, so `parameters.RequestedPublishingInterval` is what the
  server actually uses. A later explicit `update(requested)` copies the **requested** values into it; the revised
  value is in the returned `ModifySubscriptionResult` (`RevisedPublishingInterval`).
* `Subscription.subscribe_data_change(nodes, attr=AttributeIds.Value, queuesize=0,
  monitoring=MonitoringMode.Reporting, sampling_interval=50.0)` returns **an int for a single node**, or a list
  of `int | StatusCode` for several nodes — handle both shapes.
* Handlers: `DataChangeNotificationHandler` (sync callback) and `DataChangeNotificationHandlerAsync`
  (coroutine callback, awaited by asyncua). Both receive `(node, val, data)`; the StatusCode is at
  `data.monitored_item.Value.StatusCode`.
* **A newly created monitored item reports its current value immediately.** A constant variable therefore
  produces exactly one notification (the initial value) and no further ones. "No value change" must not be
  interpreted as a broken subscription.
* `await subscription.unsubscribe(handle | [handles])` and `await subscription.delete()`; `delete()` tolerates a
  transport that is already gone and only cleans up locally.
* `ua.CreateSubscriptionParameters: RequestedPublishingInterval, RequestedLifetimeCount,
  RequestedMaxKeepAliveCount, MaxNotificationsPerPublish, PublishingEnabled, Priority`.
* **`subscribe_data_change()` has no filter parameter** (`nodes, attr, queuesize, monitoring, sampling_interval`).
  A `DataChangeFilter` therefore requires the public low-level path: build
  `ua.MonitoredItemCreateRequest` (`ItemToMonitor` = `ReadValueId(NodeId, AttributeIds.Value)`,
  `MonitoringMode`, `RequestedParameters` = `ua.MonitoringParameters` with a unique `ClientHandle`,
  `SamplingInterval`, `QueueSize`, `DiscardOldest` and `Filter`) and call
  `await subscription.create_monitored_items([...])`. It returns one `int` server handle or a `StatusCode` per
  request; those handles are what `unsubscribe()` and `modify_monitored_item()` expect.
* `ua.DataChangeFilter: Trigger (ua.DataChangeTrigger), DeadbandType (ua.DeadbandType), DeadbandValue`;
  `DeadbandType.None_ = 0`, `Absolute = 1`, `Percent = 2` (percent needs an `EURange` property).
* Runtime changes: `await subscription.set_monitoring_mode(ua.MonitoringMode.Reporting | Sampling | Disabled)`
  applies to **all** items of the subscription and returns a StatusCode list;
  `await subscription.modify_monitored_item(handle, new_samp_time, new_queuesize, mod_filter_val=-1)` returns
  `MonitoredItemModifyResult` objects (`-1` keeps the existing filter);
  `await subscription.update(ua.ModifySubscriptionParameters(...))` sends `ModifySubscription` and copies the
  requested values into `subscription.parameters` (the response carries `RevisedPublishingInterval`).
  `Subscription.set_publishing_mode(bool)` only pauses publishing and does not change the monitoring mode.

## Event subscriptions (`opcua/event_service.py`, verified 2026-11-16)

* `await subscription.subscribe_events(sourcenode=ObjectIds.Server, evtypes=ObjectIds.BaseEventType,
  evfilter=None, queuesize=0, where_clause_generation=True)` returns **one int handle** per call (the
  `MonitoredItemId`), the same value `unsubscribe()` expects. It subscribes the source's
  `AttributeIds.EventNotifier`, not `Value`.
* With `evfilter=None` asyncua builds the filter **from the server's event type** by browsing its aggregate
  children, so a custom type contributes its own properties. `WhereClause` generation is skipped automatically
  for `BaseEventType` (long where clauses break some servers); for other types the clause restricts the
  subscription to that type and its subtypes.
* The async handler implements `event_notification(event: Event)` (coroutine, awaited by asyncua); status
  changes still arrive at `status_change_notification(status)`.
* `asyncua.common.subscription.Event` exposes each selected field as an attribute named after the clause
  (`EventId`, `EventType`, `SourceNode`, `SourceName`, `Time`, `ReceiveTime`, `LocalTime`, `Message`,
  `Severity`, …); `event.data_types` lists the selected names and their `VariantType`. Direct-value clauses use
  the attribute name, browse-path clauses use the joined QualifiedName path (`Parent/Child`).
* `EventId` is a `ByteString` (bytes), `Message` a `LocalizedText`, `LocalTime` a `TimeZoneDataType`; the
  fields are raw values, not `Variant`s. `ua.ObjectIdNames` maps a namespace-0 numeric identifier to its
  standard name (for example `2041 -> "BaseEventType"`).
* Subscribing one source at a time is deliberate: the single-node path raises `UaStatusCodeError` on a bad
  StatusCode instead of returning it, so per-source isolation needs one call per source.
* Server side (demo/tests only): `await server.get_event_generator(etype, emitting_node)` returns an
  `EventGenerator`; `await generator.trigger(message="…")` publishes one event. The generator sets the
  source's `EventNotifier` and adds a `GeneratesEvent` reference. **The asyncua server (2.1.0) supports event
  monitored items only on the Server object** — any other source is answered with `BadServiceUnsupported`,
  which the client reports as a rejected source rather than as a failure of the subscription.
* Condition fields (verified 2026-11-17): subscribing to `ConditionType` (`i=2782`) or a subtype selects
  `AckedState`, `ConfirmedState`, `EnabledState`, `Retain`, `ConditionName`, `BranchId`, and appends a select
  clause with `AttributeId.NodeId` and `TypeDefinitionId = ConditionType` whose field name is `NodeId`.
  Condition events carry the condition node there; `SourceNode` is the emitting node.
* `Acknowledge` is an instance method with BrowseName `Acknowledge` in **namespace 0**;
  `Node.get_child(["Acknowledge"])` resolves it (a same-named method in another namespace is not matched).
  Its `InputArguments` are `ByteString` (EventId) and `LocalizedText` (Comment), and the call returns a list of
  output `Variant`s. asyncua's server has no built-in condition object; tests create one at runtime with
  `add_object` + `add_method(..., [VariantType.ByteString, VariantType.LocalizedText], [])`.

## Server status and diagnostics (verified 2026-11-17)

* Standard namespace-0 variables read by the Server tab: State 2259, CurrentTime 2258, StartTime 2257,
  SecondsTillShutdown 2992, ShutdownReason 2993, ServerDiagnostics/EnabledFlag 2294,
  ServerDiagnosticsSummary 2275 with counters 2276–2288/3705, ServerCapabilities limits
  MaxNodesPerRead 11705, MaxNodesPerBrowse 11710, MaxMonitoredItemsPerCall 11714,
  MaxBrowseContinuationPoints 2735, MaxSessions 24095, MaxSubscriptions 24096, MinSupportedSampleRate 2272.
* `State` is a `ServerState` enum; asyncua decodes it to a number (`0` = Running). Times are `DateTime` values;
  uptime is computed client-side from CurrentTime − StartTime.
* **asyncua 2.1.0's demo server returns Good StatusCodes with null values** for `SecondsTillShutdown`,
  `ShutdownReason`, `EnabledFlag`, every summary counter and several capability limits. Treat Good-null as
  "not provided", never as zero; only Bad StatusCodes are errors.
* The `ServerDiagnosticsSummary` object exists, so the reads succeed; the values are simply empty in this server.
  A client that needs `GetMonitoredItems` must call a method, which this project intentionally does not add
  (one guarded method-call path only).

## Raw history (verified 2026-11-18)

* `Session.history_read(params)` (`UaClient.history_read`, also on `AbstractSession`) takes
  `ua.HistoryReadParameters` and returns one `ua.HistoryReadResult` per `NodesToRead` entry. There is **no
  `Client.history_read`** in 2.1.0 — the call goes through the session the client owns.
* `ua.HistoryReadParameters` carries `HistoryReadDetails`, `TimestampsToReturn`, `ReleaseContinuationPoints`
  and `NodesToRead` (one `ua.HistoryReadValueId` with `NodeId`, `IndexRange`, `DataEncoding`,
  `ContinuationPoint`).
* `ua.ReadRawModifiedDetails` uses `IsReadModified` (bool), `StartTime`, `EndTime`, `NumValuesPerNode` and
  `ReturnBounds`. An open range is `ua.get_win_epoch()` for both ends (the asyncua helper exists and is used).
* Results arrive as `ua.HistoryReadResult` with `StatusCode`, `ContinuationPoint` (bytes, empty when there is no
  more data) and `HistoryData.DataValues`. asyncua's server returns **newest-first** for `NumValuesPerNode`
  caps; the client sorts ascending by source timestamp, then server timestamp.
* **asyncua 2.1.0 has no client helper for processed, aggregate or at-time history**: `read_processed_history`,
  `read_at_time` and `read_events` do not exist on `Client`/`Session`. `read_raw_history(node, start, end, num)` is
  a convenience wrapper only (it builds the same raw request); the project calls `history_read` directly because
  it needs continuation points and `ReleaseContinuationPoints`.
* A node that is not historized yields a **Good StatusCode with zero values**; the server logs an error
  internally. That is a server fact, so the client reports it together with the `Historizing` attribute
  (`AttributeIds.Historizing` = 20, Boolean) instead of raising.
* `asyncua.server.history.HistoryManager`/`HistoryDict` is the demo server's in-memory historian;
  `Server.historize_node_data_change(node, period, count, ...)` (and `historize_node_event`) register a node.
  The demo server uses `historize_node_data_change(..., count=2000)` behind `--history`.
* The demo server never returns a continuation point: a capped read simply returns the newest N values. The
  client's continuation loop is therefore exercised with a fake session in the unit tests.

## Server directory / FindServers (verified 2026-11-18)

* `Client.find_servers(uris=None)` builds `ua.FindServersParameters` (`EndpointUrl` = the client's server URL,
  `ServerUris` = the given list) and calls `uaclient.find_servers(params)`; it returns
  `list[ua.ApplicationDescription]`. There is no session involvement.
* `ua.ApplicationDescription` fields: `ApplicationUri`, `ProductUri`, `ApplicationName` (LocalizedText),
  `ApplicationType` (enum `ua.ApplicationType`), `GatewayServerUri`, `DiscoveryProfileUri`, `DiscoveryUrls`.
  **`ua.LocalizedText(Text, Locale)` — text first**; `LocalizedText(None, "x")` sets Text=None.
* `ServerUris` is a **prefix match on the application URI split by `:`** in asyncua's server implementation, so an
  entry like `urn:vendor` matches `urn:vendor:product:instance`.
* FindServers works on a plain `SecurityPolicyNone` secure channel: no session, no identity, no certificate
  approval. The project reuses its endpoint-discovery channel pattern for it.
* asyncua's demo server implements FindServers (`InternalServer.find_servers`) and returns itself
  (`urn:opcua-explorer:demo:server`, type `ClientAndServer`, one discovery URL).
* `Client.find_servers_on_network()` exists (signature `(self)`), but the asyncua server answers
  `BadUserAccessDenied`; there is no verified multicast/LDS client path here, so the feature is not offered.

## Connection recovery (used by the opt-in check and the Resilience toggle)

* `await client.close_secure_channel()` closes the channel. With `auto_reconnect=False`, the next request
  fails (`ConnectionError`) and `client.state` becomes `disconnected`. A plain channel close is not reopened
  transparently in that mode.
* With `auto_reconnect=True`, `client.connect()` starts a supervisor task. On transport loss the state becomes
  `reconnecting`; the supervisor re-opens socket, channel and session on the same endpoint with exponential
  backoff (`reconnect_max_delay`, 30 s in this project), re-creates live subscriptions, and returns the state to
  `connected`. Requests issued while it retries wait at most `reconnect_request_timeout`.
* `await client.connect()` on the *same* client also re-establishes socket, channel and session directly;
  `client.state` returns to `connected` and subsequent reads work. With the supervisor enabled the recovery
  check must **observe** the state instead of calling `connect()` a second time, or the two retry paths race.
* `client.uaclient.session` is reused across that reconnect, which is why the runner always accesses the session
  through a property instead of caching the object.

## Consequences for the design

1. `opcua/browse.py` implements Browse/BrowseNext itself, because the Node helper cannot release
   continuation points and cannot bound `RequestedMaxReferencesPerNode`.
2. `opcua/attributes.py` batches `ReadValueId`s instead of calling `Node.read_attributes` per node.
3. `client_service.connect()` passes an approved DER certificate to `set_security`, never `None`.
4. `serialize.py` derives the ExtensionObject encoding code from the body type instead of trusting
   `ExtensionObject.Encoding`.
5. The demo server sets its application URI to match its certificate, and its user manager treats an empty
   `certificate` value as "no certificate".
6. The subscription checks treat the mandatory initial value notification separately from a value *change*, and
   accept both the `int` and `list` return shapes of `subscribe_data_change`.
7. The recovery check uses the same client. It calls `connect()` after `close_secure_channel()` when the
   supervisor is off, and otherwise waits for the supervisor to publish `connected` plus a successful read
   instead of starting a competing retry.

## Value read options (verified 2026-11-18)

* `ua.ReadParameters` fields: `MaxAge`, `TimestampsToReturn`, `NodesToRead`. `ua.ReadValueId` fields:
  `NodeId`, `AttributeId`, `IndexRange`, `DataEncoding`.
* `Node.read_attribute(attr, indexrange=None, raise_on_bad_status=True)` shows the pattern: it sets
  `ReadValueId.IndexRange` and calls `self.session.read(params)`. It does **not** set `MaxAge` or
  `TimestampsToReturn`, so a plain asyncua read leaves both at their defaults (`MaxAge = 0`,
  `TimestampsToReturn = 0` = `Source`).
* `ua.TimestampsToReturn` members: `Source`, `Server`, `Both`, `Neither`, `Invalid`.
* `session.read(params)` returns one `ua.DataValue` per `NodesToRead` entry and does **not** raise on a Bad
  StatusCode — the caller must inspect `DataValue.StatusCode` (the project decodes it into a status name).
* **asyncua's demo server accepts `IndexRange` and `TimestampsToReturn` and ignores them**: asking for `1:3` on
  `ns=2;s=Samples` returns the full array, and `Neither` still returns both timestamps. That is why the client
  reports the requested options and lets the server be authoritative.
* `MaxAge` is `uint32` (0 = server cache allowed, max = come from the device); a `QSpinBox` is int32, so the GUI
  offers up to 2 147 483 647 while the model accepts the full protocol range.

## Interactive write and method call (verified 2026-11-18)

* `Node.write_value(ua.DataValue(ua.Variant(value, variant_type)))` sends one `WriteRequest`; a Bad result raises
  `ua.UaStatusCodeError` whose `.code` can be turned into a StatusCode name. Reading the previous value uses
  `Node.read_data_value()` (StatusCode + Variant + timestamps).
* `ObjectNode.call_method(method_node, *args)` returns the method result. **asyncua unwraps a single output
  argument** (a plain value, not a list) while multi-output methods return a list; the caller must normalise both
  (the project stores `CallOutcome.output_values`).
* `read_input_arguments()` returns `uaprotocol_auto.Argument` objects (not `ua.Argument`) with `Name`, `DataType`,
  `ValueRank` and `Description` (`LocalizedText` with `Text` first in its constructor).
* `Node.get_child(["InputArguments"])` raises when the property is absent — a method without arguments — so an
  empty signature is a normal result, not an error.
* A Good service result is not proof of effect: the project always reads the value back and compares it, and
  reports a differing read-back as a failure.

## NodeSet2 export

Verified against `asyncua/common/xmlexporter.py` and `asyncua/common/xmlparser.py`:

* `XmlExporter(server_or_client, export_values: bool = False)`; `build_etree(node_list,
  add_all_namespaces=False)` fills `exporter.etree`; `write_xml(path, pretty=True)` writes it.
* **Value export is a single global switch.** `add_etree_variable` passes `self._export_values` down to
  `add_variable_common(..., export_value)`, so there is no per-node flag. The exporter itself logs
  *"Exporting values of variables is limited and can result in invalid xmls."*
  To write method arguments without enabling value export everywhere, a second exporter is created with
  `export_values=True`, its `_addr_idx_to_xml_idx` mapping is replaced with the main exporter's (verified
  attribute name in 2.1.0), the node is rendered there, and its `<Value>` element is transplanted by `NodeId`.
* `_add_namespaces` builds `_addr_idx_to_xml_idx` from the **session** namespace array of the written nodes,
  so the file's namespace table is `ns=1..N` in sorted address-space index order — never assume that the
  session index equals the file index.
* Reference elements are written with the alias table (`<Aliases>`) built from `ExportModel.Aliases`
  (`ua.ObjectIds` names). A reference to a node that is not in the file is written anyway — the importer
  reports it as broken, which is why our own structural check reports dangling references.
* `Server.import_xml(xmlstring=...)` is the cheapest independent verifier: it fails with
  `BadParentNodeIdInvalid` when a `ParentNodeId` is missing from the file and warns
  *"The following references could not be imported and are probably broken"* for dangling target NodeIds.
* A `<UADataType>` without a `DataTypeDefinition` breaks asyncua's importer (`ua.BaseDataType` lookup), so such
  DataTypes must be excluded instead of written.
* The default XML namespace is `http://opcfoundation.org/UA/2011/03/UANodeSet.xsd`; parsing the output needs
  namespace-aware element names (`{...}UANodeSet`), and the schema does **not** validate a file that omits
  `<NamespaceUris>` for indexes it uses.

### Headless export (`cli.py`)

The command adds **no new asyncua API**: it calls `OpcUaService.discover()`, `connect()`, `download()` and
`export_nodeset2()`, and prints `NodeSet2Result.summary_text()`. Verified details it relies on:

* `Client.connect_socket/send_hello/open_secure_channel/get_endpoints` are used only inside the service's
  discovery helper; the CLI never opens a client itself.
* `ConnectionSettings.validate(require_server_certificate=False)` is the pre-flight check before a network call;
  `connect()` repeats the full validation including the approved DER.
* Certificate files are read with `cryptography.x509.load_pem_x509_certificate` /
  `load_der_x509_certificate` (same library `opcua/security.py` already uses) and converted with
  `public_bytes(Encoding.DER)`; the resulting SHA-256 is compared with the advertised
  `EndpointDescription.ServerCertificate` exactly as `client_service.connect()` does.
* `asyncio.run()` in the CLI owns its event loop, so the command is safe to call from a thread that has no
  running loop (which is how the integration tests drive it next to an in-process demo server).

## Guarded write and method call

* `Node.read_data_type()` returns a `ua.NodeId` whose `NodeIdType` is **not** necessarily `Numeric`: built-in
  DataTypes such as `Double` (`i=11`) come back as `NodeIdType.TwoByte`. Compare `NamespaceIndex == 0` and
  `isinstance(identifier, int)`, never the NodeIdType.
* `ua.datatype_to_varianttype(node_id)` accepts a `NodeId`, uses `int_type.Identifier` and returns
  `VariantType` for identifiers ≤ 25, `VariantTypeCustom` above that. Use it to pass an explicit variant type
  to `write_value` instead of letting `ua.Variant` infer (a Python `int` becomes `Int64`).
* `Node.write_value(ua.DataValue(ua.Variant(value, variant_type)))` raises `ua.UaStatusCodeError` on rejection
  (`BadNotWritable`, `BadTypeMismatch`, `BadUserAccessDenied`, …); the code is `exc.code`, not `exc.args[0]`.
* `Node.read_attribute(ua.AttributeIds.ValueRank)` returns the raw `Int32` **ValueRank** (verified against the
  demo server on 2026-11-15): `Temperature` reports `-1`, while the array variable `Samples` reports **`0`**,
  because asyncua's `add_variable` never sets the attribute. Per OPC UA Part 3 `0` means "one or more
  dimensions", so `0` and `1` are a one-dimensional array, `-1` is a scalar, and the ambiguous `-2`/`-3` must be
  decided from the entered text. Matrices (`>1`) are refused instead of being flattened.
* A decoded `InputArguments` value contains **`asyncua.ua.uaprotocol_auto.Argument`** objects, while
  `ua.Argument` is the hand-written `uaprotocol_hand.Argument` class — `isinstance(value[0], ua.Argument)` is
  `False`. Duck-type on `Name`/`DataType`/`ValueRank` instead.
* `object_node.call_method(method, *args)` expects a **Node** (or NodeId), not the NodeId *string*; passing a
  string yields `BadNoMatch` from the server.
* The server passes method arguments to the Python implementation as `ua.Variant` objects, so a demo/test
  implementation must unwrap `variant.Value` before using the value.
* `Node.read_attribute(ua.AttributeIds.UserAccessLevel)` / `Executable` return `DataValue`s whose
  `StatusCode` must be checked before trusting `Value.Value`.

## Server-driven write typing (`opcua/type_support.py`, verified 2026-11-15)

* `ua.AttributeIds.DataTypeDefinition` exists (value `23`) and is read with
  `read_attribute(..., raise_on_bad_status=False)`; the Variant value is already a **decoded**
  `ua.EnumDefinition` or `ua.StructureDefinition` (the same shape `nodeset2.py` checks for).
* `ua.EnumDefinition.Fields` is a `list[ua.EnumField]` (`Value`, `DisplayName`, `Description`), **not**
  `ua.EnumValueType`. `ua.EnumValueType` is what `EnumValues` properties contain (`Value`, `DisplayName`).
* `ua.StructureDefinition` = `DefaultEncodingId`, `BaseDataType`, `StructureType`, `Fields`;
  `ua.StructureField` = `Name`, `Description`, `DataType`, `ValueRank`, `ArrayDimensions`, `MaxStringLength`,
  `IsOptional` (**default `True`**). `ua.StructureType.Structure` is the only kind this project encodes.
* `asyncua.ua.ua_binary.struct_to_binary(instance)` serialises a dataclass in field order. Field annotations
  must resolve to a `ua` type (`ua.Double`, `ua.String`, `ua.LocalizedText`, …) or a `list[...]` of one;
  a bare Python `float`/`str` raises `UaError: No known way to pack value of type`.
* Encoding a structure for a write is `ua.ExtensionObject(TypeId=<DefaultEncodingId>, Body=<struct_to_binary(...)>)`
  inside `ua.Variant(value, ua.VariantType.ExtensionObject)`; `Encoding` stays at its default and the wire form is
  `NodeId + 0x01 + Int32 length + body`. `ua.register_extension_object` is only needed for *decoding*, not for
  writing.
* `ua.datatype_to_varianttype` raises for a custom DataType (identifier > 25), so enumerations are sent with an
  explicit `ua.VariantType.Int32` and structures with `ua.VariantType.ExtensionObject`.
* `ua.NodeId.is_null()` exists and is the check for a missing `DefaultEncodingId`.

## Browse-path resolution (`opcua/path_support.py`, verified 2026-11-15)

* `asyncua.Client` has **no** `translate_browsepaths_to_nodeids`; the service lives on
  `client.uaclient.translate_browsepaths_to_nodeids(paths)` and on
  `client.uaclient.session.translate_browsepaths_to_nodeids(paths)` (same signature
  `list[ua.BrowsePath] -> list[ua.BrowsePathResult]`).
* `ua.BrowsePath(StartingNode, RelativePath)` expects **`ua.RelativePath(Elements=[...])`**, not a plain list;
  passing a list fails while encoding with `AttributeError: 'list' object has no attribute 'Elements'`.
* `ua.RelativePathElement(ReferenceTypeId, IsInverse, IncludeSubtypes, TargetName)`; `TargetName` is a
  `ua.QualifiedName` (a bare name is namespace 0). `ua.ObjectIds.HierarchicalReferences` is `33`.
* `ua.BrowsePathResult(StatusCode, Targets)`; `ua.BrowsePathTarget(TargetId, RemainingPathIndex)`.
  A complete match reports `RemainingPathIndex = 0xFFFFFFFF`; a partial target must not be accepted as the
  answer.
* `TargetId` is an `ua.ExpandedNodeId` (`Identifier`, `NamespaceIndex`, `NodeIdType`, `NamespaceUri`,
  `ServerIndex`). Its `to_string()` yields `ns=…` form only while `NamespaceUri` is empty; a URI-bearing target
  must be refused or mapped explicitly.
* The asyncua demo server implements the service; the web of virtual nodes makes it a real end-to-end test
  (`tests/integration/test_path_resolution.py`).

## Qt stability: the PySide6 pin (verified 2026-11-15)

* **PySide6 6.12.0 aborts the interpreter** with
  `Fatal Python error: none_dealloc: deallocating None: bug likely caused by a refcount error in a C extension`
  after a few hundred `QTreeWidgetItem` rows are created, populated (`setText`, `setFont`, `setForeground`,
  `setToolTip`) and discarded. The traceback points at whichever Qt setter happens to run when the corrupted
  refcount is released; it is a Shiboken/QVariant regression, not application logic.
* Reproduced with public Qt API only: ~200 rounds of *create parent → create 20 children → set roles →
  `takeChildren()` → `takeTopLevelItem()`* already abort; 6.11.3 survives 100 000 items and 15 000 rounds.
  Event processing (`QApplication.processEvents`), constructor-with-parent and role-free items make no
  difference on 6.12.0, and no 6.12.x patch is published (6.12.0 is the newest release on the index).
* The project therefore pins **`PySide6==6.11.3`** (`pyproject.toml`). The same churn runs in a subprocess in
  `tests/integration/test_pyside_stability.py`, so a future upgrade fails a test instead of crashing the GUI
  in production.
