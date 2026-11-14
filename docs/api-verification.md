# Verified asyncua API (as used by this project)

The brief requires verifying the real library API instead of inventing methods. Everything below was read from
the installed package (`asyncua 2.1.0`, Python 3.11) via `inspect`/source inspection, and is exercised by the
test suite.

Environment: Python 3.11.2 · asyncua 2.1.0 · PySide6 6.12.0 · cryptography 50.0.2 · uv 0.13.7.

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

## Subscription (used by the phase-2 test suite)

* `await client.create_subscription(period: float | ua.CreateSubscriptionParameters, handler, publishing=True,
  queue_maxsize=1000, overflow=OverflowPolicy.DROP_OLDEST)` returns a `Subscription`; internally it creates the
  subscription, applies the server's revised parameters and registers the subscription on the client.
* `Subscription.parameters` holds the **effective** parameters after the server's revision
  (`Subscription.update()` copies the revised values into it), so `parameters.RequestedPublishingInterval` is
  what the server actually uses.
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

## NodeSet2 export (phase 3)

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

## Guarded write and method call (phase 4)

* `Node.read_data_type()` returns a `ua.NodeId` whose `NodeIdType` is **not** necessarily `Numeric`: built-in
  DataTypes such as `Double` (`i=11`) come back as `NodeIdType.TwoByte`. Compare `NamespaceIndex == 0` and
  `isinstance(identifier, int)`, never the NodeIdType.
* `ua.datatype_to_varianttype(node_id)` accepts a `NodeId`, uses `int_type.Identifier` and returns
  `VariantType` for identifiers ≤ 25, `VariantTypeCustom` above that. Use it to pass an explicit variant type
  to `write_value` instead of letting `ua.Variant` infer (a Python `int` becomes `Int64`).
* `Node.write_value(ua.DataValue(ua.Variant(value, variant_type)))` raises `ua.UaStatusCodeError` on rejection
  (`BadNotWritable`, `BadTypeMismatch`, `BadUserAccessDenied`, …); the code is `exc.code`, not `exc.args[0]`.
* A decoded `InputArguments` value contains **`asyncua.ua.uaprotocol_auto.Argument`** objects, while
  `ua.Argument` is the hand-written `uaprotocol_hand.Argument` class — `isinstance(value[0], ua.Argument)` is
  `False`. Duck-type on `Name`/`DataType`/`ValueRank` instead.
* `object_node.call_method(method, *args)` expects a **Node** (or NodeId), not the NodeId *string*; passing a
  string yields `BadNoMatch` from the server.
* The server passes method arguments to the Python implementation as `ua.Variant` objects, so a demo/test
  implementation must unwrap `variant.Value` before using the value.
* `Node.read_attribute(ua.AttributeIds.UserAccessLevel)` / `Executable` return `DataValue`s whose
  `StatusCode` must be checked before trusting `Value.Value`.
