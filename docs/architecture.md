# Architecture — OPC UA Explorer

Status: **complete and maintained** (verified 2026-11-16). Implemented and tested: connection/security with
background reconnect, browser, download, JSON/CSV/NodeSet2 export (GUI and headless `opcua-export`), live
monitoring, a functional test suite with JSON/HTML reports and guarded single-target
write and method call, address-space comparison (`opcua-compare` and the Compare tab) and operator-written
control sequences (`opcua-run-sequence` and the Sequences tab). See section 12 for what is deliberately out of
scope.

---

## 1. Goals and hard requirements

The application must let an engineer connect to an OPC UA server, understand what it contains, and produce a
portable inventory — without ever weakening security silently, without logging secrets, and without writing to
a process it was not explicitly told to write to.

Derived design rules:

| Requirement | Consequence in the code |
|---|---|
| Never silently downgrade security | `opcua/security.py:select_endpoint()` raises instead of choosing an alternative; the service validates the chosen pair against the live advertisement on every connect |
| Explicit approval before trusting a certificate | only `ConnectionSettings.server_certificate_der` (human-approved DER) reaches `set_security()`; asyncua's own implicit certificate fetch is never used |
| No credentials in profiles | `ConnectionProfile` has no password field; `assert_no_secrets()` checks every persisted payload |
| No secrets in logs | `logging_setup.SecretRedactor` filters formatted messages *and* exception text |
| Graph, not tree | traversal keeps a visited set, stores every observed reference, and descends only along forward hierarchical references |
| Responsive GUI | one asyncio loop in a worker thread; results cross to the GUI as queued Qt signals |

## 2. Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ gui/  (PySide6)                                                      │
│  app.py · main_window.py · session_workspace.py                      │
│  tabs/{connection,connection_dialogs,browser,export,monitor,events,  │
│        history,write,server,tests,sequences,logs,                    │
│        unified_monitor,compare,server_directory_dialog}             │
│  async_runner.py  ← only place Qt meets asyncio                      │
│  tabs/base.py · TabRuntime/SessionTab (shared tab plumbing)          │
└───────────────┬──────────────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────────────┐
│ cli.py · cli_read · cli_monitor · cli_events · cli_history           │
│ cli_write · cli_check · cli_status · cli_nodes · cli_directory      │
│ cli_compare · cli_sequence  (opcua-* console scripts, Qt-free)       │
└───────────────┬──────────────────────────────────────────────────────┘
                │ coroutines submitted / plain model objects returned
┌───────────────▼──────────────────────────────────────────────────────┐
│ opcua/  (asyncua)                                                    │
│  client_service.py  one Client, discovery, connect, browse, download │
│  security.py  browse.py  attributes.py  traversal.py  serialize.py   │
│  ua_names.py  value_parsing.py  monitor_service.py  nodeset2.py      │
│  event_service.py  nodeset2_validation.py  check_support.py          │
│  monitor_session.py  event_session.py  (subscription lifecycle)      │
│  node_reader.py  node_ids.py  (session-bound browse/read helpers)    │
│  session_setup.py  (discovery, client construction, connect flow)    │
│  server_diagnostics.py  history_service.py  test_runner.py           │
│  guarded_operations.py  write_service.py  type_support.py            │
│  path_support.py  sequence_runner.py  method_support.py              │
└───────────────┬──────────────────────────────────────────────────────┘
                │ AddressSpace snapshot / TestRun (plain dataclasses)
┌───────────────▼──────────────────────────────────────────────────────┐
│ models/ · config.py · defaults.py · project.py · logging_setup.py    │
│ formatting.py · operator_text.py · exporters/ · reporting.py         │
│ sequence_document.py                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

Rules that keep the layers honest:

* `models/` (one module per feature cluster, re-exported from `__init__.py`), `config.py`, `defaults.py`,
  `project.py`, `formatting.py`, `operator_text.py`, `sequence_document.py`,
  `exporters/`, `reporting.py`, `compare.py` import neither asyncua nor Qt. Each module is import-probed in
  its own subprocess by `tests/unit/test_layer_boundaries.py`, so a stray import names the offending module.
* `exporters/` uses `formatting.py`, never `opcua/` (a unit test enforces this boundary).
* Only `opcua/session_setup.py` constructs an `asyncua.Client` — one instance per session; `OpcUaService`
  owns the resulting state and delegates discovery/connect to that module.
* `gui/session_workspace.py` owns one service and its ten pages, orders them by the on-site workflow
  (`Connection`, `Server`, `Address space`, `Live monitor`, `Events`, `History`, `Write & call`, `Tests`,
  `Sequences`, `Export`) and injects that session's browser selection into the Export and Tests tabs;
  `gui/main_window.py` is only the tab bar, menus and status bar, and never touches a client.
* Only `gui/async_runner.py` touches asyncio from the GUI side; widgets never `await`.
* `cli.py`, `cli_read.py`, `cli_monitor.py`, `cli_events.py`, `cli_history.py`, `cli_write.py`,
  `cli_check.py`, `cli_status.py`, `cli_nodes.py`, `cli_directory.py`, `cli_compare.py` and
  `cli_sequence.py` are the Qt-free front ends:
  each owns its own event loop
  through `asyncio.run`, calls `OpcUaService` and the shared helpers, and never touches asyncua directly. They
  must not re-implement traversal, dependency collection, writing, validation, certificate approval, the
  comparison or the sequence engine — the GUI and the CLIs share those objects on purpose.

## 3. Threading model

```
GUI thread                       asyncio thread ("opcua-asyncio")
──────────                       ───────────────────────────────
user clicks Connect
  runner.submit(factory) ───────▶ loop.create_task(_track(...))
                                 await service.connect(...)
                                     sockets, sessions, reads …
QAbstractEventLoop               emit done(key, result)
  ◀── queued signal ─────────────┘
on_done(result) updates widgets
```

* One loop, created *inside* the worker thread (`new_event_loop` + `run_forever`).
* `submit()` accepts a **coroutine factory** so the coroutine is created in the loop thread; scheduling
  failures can never leave an un-awaited coroutine behind.
* Cancellation is performed inside the loop (`loop.call_soon_threadsafe(task.cancel)`), plus a
  "cancel before the task started" set to close the submission race.
* Blocking file work uses `AsyncRunner.submit_call()`: the callable runs on the loop's executor and the result is
  applied by a queued callback. Widget state is captured and applied on the GUI thread (project save/open, log
  export); the synchronous `save_project` / `load_project` methods stay available for tests.
* On shutdown the window cancels UI work, then `run_until_complete(service.shutdown)` releases the OPC UA
  session *before* the loop stops.

### Streaming results: live monitoring

A subscription outlives the coroutine that created it, so the monitor uses a second dispatch path:

* `OpcUaService.start_monitor(...)` creates the subscription and returns a `MonitorStart`; asyncua's publish loop
  keeps delivering notifications afterwards.
* Notifications are encoded to JSON-safe values **in the asyncio thread** (`opcua/monitor_service.py`) and
  truncated to 4000 characters, then emitted through `AsyncRunner.progress` under one key.
* `AsyncRunner.listen(key, slot)` keeps the slot registered after the submitting task's `done` callback has been
  consumed; the tab calls `unlisten()` when it stops monitoring or shuts down.
* `monitor_remove()` deletes single monitored items; stopping or disconnecting deletes the subscription, so no
  server-side state is left behind. The GUI keeps three views: a per-node row that is overwritten in place (latest
  value, StatusCode, source timestamp, count), a bounded append-only notification log, and a trend chart of the
  **selected** node. The chart is fed by `trend.append_live_point()` as each notification arrives, so it is a view
  of what this tab already received: no second read, no sorting, no interpolation, and the x axis is the client's
  receive time (a live notification may arrive without a source timestamp). Its buffer follows the *Keep rows*
  bound but never exceeds `defaults.MAX_LIVE_TREND_POINTS`, keeps the newest points, and the caption names how
  many older points are not drawn; non-numeric values are counted instead of plotted. Redraws are coalesced by a
  250 ms timer so the paint cost does not follow the notification rate, and *Clear notifications* drops the chart
  with the log because it is the same set of values.
* A `DataChangeFilter` (trigger and absolute/percent deadband) is built once in `build_data_change_filter()`.
  asyncua's `subscribe_data_change()` has no filter parameter, so a filtered subscription builds
  `ua.MonitoredItemCreateRequest` objects with its own client handles and calls the public
  `Subscription.create_monitored_items()`; an unfiltered subscription keeps the simpler path. The filter is fixed
  for the lifetime of a subscription.
* Runtime changes stay on the subscription object and never touch the session: `set_monitoring_mode()` applies
  `SetMonitoringMode` to every item, `set_publishing_interval()` sends `ModifySubscription` through
  `Subscription.update()`, and `set_sampling_interval()` sends one `ModifyMonitoredItems` per item via
  `modify_monitored_item()`. Refusals are returned to the tab, which keeps the previous mode or reports the
  per-item status.

### Streaming results: events

Event monitoring uses the same dispatch path with its own subscription (`opcua/event_service.py`):

* `OpcUaService.start_event_monitor(...)` creates one subscription and subscribes each source node's
  `EventNotifier` attribute through `Subscription.subscribe_events`, so the server's event-type definition
  decides which fields are requested (`BaseEventType` by default, a custom type on request).
* `event_service.build_event_filter()` runs the selection on the **server**: the standard fields plus any
  `select_fields` (an `index:Name` selects outside namespace 0) become the `SelectClauses`, and `min_severity`
  becomes a `ContentFilter` WhereClause (`Severity >= n`). The filter is built once when the subscription starts
  and passed to every source, so low-severity events never reach the client.
* asyncua delivers an `Event` per notification; `LiveEvents` reduces every selected field to bounded text in the
  asyncio thread and emits one `EventNotification`. The tab never sees an asyncua object.
* A source that cannot publish events (no `EventNotifier`, unknown NodeId, unsupported service) is recorded as a
  rejected `EventSourceInfo` with its StatusCode; one bad source never aborts the others or the subscription.
* Event monitoring publishes notifications only. Its **guarded** action is a condition acknowledgement:
  `write_service.acknowledge_condition()` resolves the instance's `Acknowledge` method below the condition node,
  requires `InputArguments` = (ByteString, LocalizedText), and sends exactly one call through
  `send_prepared_call` with `WriteAuthority("operator-dialog", …)`. The tab asks for an optional comment, shows
  the confirmation text from `operator_text.acknowledge_condition_confirmation`, and never calls
  `Confirm`/`Enable`/`AddComment` or retries.
* Its **unguarded** action is `ConditionRefresh`: `write_service.refresh_conditions()` sends one call of the
  standard method `i=3875` on the ConditionType object (`i=2782`) with the subscription id as its only `UInt32`
  input, and `LiveEvents.refresh_conditions()` composes a `ConditionRefreshResult` from that outcome plus a
  bounded wait for the server's `RefreshStart`/`RefreshEnd` events — `max(3 s, 3 × publishing interval)`, because
  a refresh event can only arrive with a publish response. The call stores nothing, so the tab
  shows no dialog; an authority is still recorded and a missing refresh event is reported rather than claimed.
  There is no `ConditionRefresh2`, no second parent object and no retry.

### Server status

The **Server** tab (`opcua/server_diagnostics.py` + `gui/tabs/server_tab.py`) issues one batched `Read` of
namespace-0 variables and never writes:

* `read_server_status()` reads `ServerStatus` (state, times, shutdown fields),
  `ServerDiagnostics/EnabledFlag`, the `ServerDiagnosticsSummary` counters and selected
  `ServerCapabilities` limits through `read_attributes_bulk`, so one request covers all of them.
* A Good StatusCode with a null value is reported as *not provided* (many servers advertise optional
  diagnostics variables but do not fill them), and a Bad StatusCode is kept per value. Neither aborts the
  snapshot.
* `GetMonitoredItems` is not called: the project keeps one guarded method-call path. The snapshot carries
  `LiveMonitor.items_snapshot()` instead, labelled as the client's own view.
* The tab refreshes when the session connects and on **Refresh**; it owns no subscription and no timer.

### History (raw reads)

The **History** tab (`opcua/history_service.py` + `gui/tabs/history_tab.py`) performs one read-only
`HistoryRead` of the selected variable, raw or one aggregate; it never calls `HistoryUpdate` and never retries:

* `build_history_request()` produces `HistoryReadParameters` with `ReadRawModifiedDetails`, an open range
  (`ua.get_win_epoch()` on both ends) unless the operator sets From/To, `NumValuesPerNode` from the value cap,
  `ReturnBounds = False` and `TimestampsToReturn.Both` for exactly one node. With
  `HistoryConfig.aggregate` set (average/minimum/maximum/count) it produces `ReadProcessedDetails` instead:
  a required start/end, `ProcessingInterval`, the aggregate's standard `AggregateType` NodeId and
  `UseServerCapabilitiesDefaults = True`. A server without aggregate support is reported with its StatusCode
  (the demo historian answers `BadNotImplemented`), never silently treated as raw.
* `read_history()` validates the range, follows continuation points up to `_MAX_PAGES = 50`, decodes each
  `DataValue` through the shared `decode_data_value()`, releases the continuation point when the cap or the page
  limit ends the read, and makes a best-effort release in `finally` so no server-side cursor is left behind.
* Results are sorted by source (then server) timestamp and trimmed to the requested cap. The server decides
  which values it keeps and which ones a page contains; the client only orders what it received.
* A Good StatusCode with no values is normal: the result records the node's `Historizing` attribute and the
  status line says whether the node reports `Historizing = false` or simply has no values in the range. A Bad
  StatusCode is reported with that StatusCode, never as an exception or as row loss.
* The trend chart is a **view of that one result**: `trend.py` (`build_trend_series`) walks the returned
  snapshots, keeps the ones that are a single number (a bool becomes 0/1), counts the rest as skipped, and
  decides `time_based` from the timestamps; `gui/trend_chart.py` paints the series and nothing else. It never
  reads, subscribes, interpolates or resamples, so the picture cannot show values the server did not return.
  `opcua-history --sparkline` prints the same series as one line of block characters (bucketed by average when
  it is longer than the line). The same widget draws the Live monitor's series, which the same module builds
  from the notifications that tab received (`append_live_point`).

### Value read options

The Address space *Value* panel reads one attribute with explicit options through
`opcua/value_read.py` (`build_read_parameters` → `read_value_with_options`):

* One `Read` request for the **Value** attribute: `ua.ReadValueId.IndexRange` (NumericRange), `MaxAge` and
  `TimestampsToReturn` on `ua.ReadParameters`. The options are normalised in `models.ValueReadOptions`
  (clamped MaxAge, known timestamp name, trimmed range).
* The NumericRange is validated against the `n`/`n:m` grammar **before** the request, so a typo is a local
  error, not a server StatusCode.
* The answer is decoded with the shared `decode_data_value()` and wrapped in `ValueReadResult`, which carries the
  options, the duration and a detail line naming what was requested. A server may ignore the slice or return
  fewer timestamps; the panel therefore shows the request next to the value instead of claiming an effect.

### Browse filters (read-only)

The Address space tree browses through `models.BrowseOptions` and `opcua/browse.py::browse_children(...,
options=...)`:

* One `BrowseDescription` per node with `ReferenceTypeId`, `BrowseDirection`, `IncludeSubtypes` and
  `NodeClassMask`; the default (forward `HierarchicalReferences` with subtypes, every node class) is what the
  tree always used, so an unfiltered tree and a download stay identical.
* **Apply filter** replaces the tree. `BrowserTab._tree_generation` is bumped on every `load_node`, and a browse
  result that arrives afterwards is dropped (`_is_stale`) — plus a `RuntimeError` guard, because a cleared
  `QTreeWidget` deletes its items and a late callback must never touch them.
* The server decides whether it applies the mask; the status line names the filter and the tree stays a
  navigation aid, never an export scope.

## 4. Sessions, connection and trust flow

### Server directory (FindServers)

`OpcUaService.discover_servers()` in `opcua/client_service.py` (delegating to `opcua/session_setup.py`) sends one
`FindServers` request and returns plain
`DiscoveredServer`/`ServerDiscoveryResult` objects; the GUI dialog is
`gui/tabs/server_directory_dialog.py`, opened from the Connection tab.

* FindServers is a **channel-level** service: it needs no session, no identity and no trust decision. The call
therefore uses the same temporary pattern as endpoint discovery (socket → Hello → OpenSecureChannel → service →
close), so it works while the session is disconnected and leaves nothing behind.
* The request carries the discovery URL the operator typed and the optional application-URI prefixes
(`client.find_servers(uris)`); an empty list means "every application this directory knows".
* The answer is mapped field by field into `DiscoveredServer` (name, application URI, type, product/gateway/profile
URIs, discovery URLs). Entries are sorted by name for display; blank URLs are dropped. Nothing is verified,
cached in project state or connected to automatically — adoption only fills the endpoint field.
* A refusing endpoint, a protocol error and a timeout all become `ConnectionFailedError` with the URL in the text;
there is no retry. `FindServersOnNetwork` (multicast/GDS discovery) is not offered: asyncua's server answers it with
`BadUserAccessDenied` and the client has no verified multicast path.

`MainWindow` is a shell: it owns the top-level tab bar (one tab per `SessionWorkspace`), the menus, the status
bar and three shared tabs: `UnifiedMonitorTab` (a merge of all sessions' monitors), `CompareTab`
(a structural comparison of two sessions) and `Logs & reports`. A `SessionWorkspace` owns exactly one
`OpcUaService` and the ten pages, in workflow order (Connection, Server, Address space, Live monitor, Events,
History, Write & call, Tests, Sequences, Export). Sessions are fully independent — separate client,
discovery result, namespace table, address-space snapshot, live subscription and auto-reconnect supervisor — and
two sessions to the same endpoint are allowed. `File → New connection` (`Ctrl+N`) creates a new service on the
same asyncio runner and the same trust store; closing a session tab releases only that client. All sessions share
the single worker loop (task keys and listeners are unique per submission). Operator summaries in the shared log
are prefixed with the session name. A device that limits concurrent sessions rejects the second one with
`BadTooManySessions`, which surfaces as an ordinary connection error.

Subscription ownership stays with the session: only `MonitorTab` creates/changes/deletes a subscription. It emits
`notification_received` and `items_changed`; `UnifiedMonitorTab` merely observes them (plus
`MonitorTab.item_snapshot()`) and keeps a bounded, session-labelled item table and notification log. It never
starts or stops anything, so there is exactly one owner per subscription and closing a session simply unregisters
its rows.

### Project files

`project.py` (Qt-free) defines the versioned document (`format`, `version`, `saved_at`, `sessions[]`) and owns
all validation: endpoint/security rules through `ConnectionSettings.validate(require_server_certificate=False)`,
unknown-key filtering (a newer field cannot inject anything), refusal of newer project versions and a
secret-key guard — `assert_no_secrets` rejects a hand-edited file that contains a password field instead of
silently ignoring it. The GUI only maps widgets to dataclasses (`SessionWorkspace.project_state()` /
`apply_project_state()`); opening a project replaces the session tabs but never dials a server. Live-monitor
targets are restored as pending rows, and `TestsState` has no `allow_writes` field by design.

### Connect flow per session

```
1. Discover  → temporary SecurityPolicy#None channel: socket → Hello → OpenSecureChannel → GetEndpoints
               (channel-level service, no session, no trust decision involved)
2. Select    → select_endpoint(endpoints, policy_uri, mode)  ← raises if not advertised
3. Approve   → SHA-256 of endpoint.ServerCertificate shown to the operator
               (GUI) untrusted → review dialog; changed → new review; trusted → proceed
4. Connect   → Client(...) + set_security(policy, cert, key, key_pw, approved_der, mode)
               + identity (anonymous / username / certificate) + connect()
5. Verify    → the DER handed to set_security must hash to the currently advertised certificate
```

`set_security(..., server_certificate=None)` is deliberately avoided: asyncua would then fetch and install the
server certificate itself, bypassing the approval step. Passing the approved DER bytes is the only path that
skips that fetch (verified in `asyncua/client/client.py`).

### Link loss and resumption

`Client(..., auto_reconnect=settings.auto_reconnect)` enables asyncua's connection supervisor. What happens on
loss depends on the toggle on the Connection tab (**Resilience** → *Keep reconnecting when the link drops*, on by
default, persisted in `settings.json` and in connection profiles):

* **Enabled.** The supervisor probes the session about once a second; on loss it re-opens socket, channel and
  session on the same endpoint with the same approved security policy and identity (exponential backoff 1 s →
  30 s, no attempt limit). It re-activates the existing server-side session when the server still holds it,
  otherwise it creates a new one and restores subscriptions. The GUI keeps the session: `ConnectionTab` polls
  `service.connection_state` every 3 s and treats **every** non-connected state as transient while this mode is
  active (`disconnected` during transport teardown, `connecting`, `socket_open`, `channel_open`, `reconnecting`),
  shows "Link lost · reconnecting…" in the tab and **Reconnecting…** in the window, and leaves the other tabs
  enabled. When the state returns to `connected`, the tab re-reads the namespace array and the window refreshes
  the export namespaces and restarts a running live-monitor subscription (monitored-item handles belong to the
  old session).
* **Disabled.** The supervisor stops after the loss: the tab reports **Link lost**, the window disables the tabs
  and releases the dead client with `service.disconnect()`, as before.
* Calls issued while the transport is down fail after `request_timeout` (asyncua's reconnect gate), never hang
  until the session timeout. Nothing is downgraded on retry: the same policy, mode, identity and approved server
  certificate are used; if a secure server changed its certificate, the retries keep failing until the operator
  reconnects manually.

## 5. Address-space model

`AddressSpace` holds:

* `nodes: dict[node_id_string, NodeRecord]` — canonical records (attributes decoded, values optional)
* `references: list[Edge]` — de-duplicated, **normalised to forward direction**; an inverse reference
  `A ←ref— B` observed at `A` is stored as `B → A`
* `namespaces: list[NamespaceEntry]` — the URI table, always exported with the NodeIds
* `limits_hit`, `inaccessible`, `unsupported_encodings`, `errors`, `limitations`, timestamps, config

Values are `ValueSnapshot`s: JSON-safe encoded value, StatusCode, variant type, source/server timestamps,
and an `unsupported` list when an encoding could not be represented faithfully.

The browser tree is not rebuilt from the snapshot: it stays lazy. `BrowserTab.set_monitored_nodes()` receives
the NodeIds in the session's live-monitor list and decorates the matching rows (● prefix, bold, accent colour,
tooltip note), keeping the original text and tooltip in item data so the mark can be removed cleanly. The set is
session-local and is refreshed from `MonitorTab.items_changed`.

## 6. Traversal algorithm

```
hier_ids = enumerate subtypes of HierarchicalReferences (Browse HasSubtype, bounded)
frontier = roots (depth 0); visited = roots; pending = roots
while frontier:
    level, overflow = take_within_budget(frontier)     # max_nodes counts nodes + pending
    outcomes = browse_level(level)                     # batches of `batch_size`, `concurrency` in flight
    for each outcome:
        create record (class/display from the discovering reference as a hint)
        for each reference: store canonical edge; HasTypeDefinition → record
        descend only if: forward ∧ hierarchical ∧ not visited ∧ namespace allowed ∧ depth < max_depth
frontier = next level
then: base attributes (batch) → class-specific attributes (batch) → type names → optional values
```

Properties:

* **Cycles**: the visited set makes the demo's `Motor → Plant` cycle safe.
* **Shared children**: a node referenced from several parents is collected once but every edge is kept
  (`SharedConfig` in the demo).
* **Depth**: level-by-level traversal makes `max_depth` exact (nodes at depth *d* are read, their children are
  not followed).
* **Budget**: `max_nodes` counts collected *and* pending nodes, so the limit is respected before requests go
  out; hitting it is reported as `limits_hit: ["max_nodes"]`.
* **Concurrency/batching**: chunks of `batch_size` nodes per Browse request, at most `concurrency` requests in
  flight; per-chunk failures become per-node `inaccessible` entries instead of aborting the download.
* **Continuation points**: `browse_many()` follows `BrowseNext` and releases outstanding continuation points
  (`ReleaseContinuationPoints=True`) when a batch is cancelled. asyncua's own `get_children_descriptions()`
  never releases them, which is why this module talks to `UaSession.browse/browse_next` directly.
* **Cancellation** returns a *partial* snapshot marked `cancelled: true` instead of throwing the work away.

Attribute reads are batched as `ReadValueId` lists (base attributes for every node, then class-specific ones
after the NodeClass is known). Bad status codes (e.g. `DataType` on an Object) are recorded per attribute in
`unavailable_attributes`, never treated as a download failure.

## 7. Value encoding contract

| OPC UA value | JSON form |
|---|---|
| Boolean, numbers, String | native JSON |
| Double NaN / ±Infinity | `{"$type":"Float","value":"NaN"\|"Infinity"\|"-Infinity"}` |
| ByteString | `{"$type":"ByteString","base64":"…"}` |
| DateTime | `{"$type":"DateTime","iso":"…Z"}` |
| Guid | `{"$type":"Guid","value":"…"}` |
| NodeId / ExpandedNodeId | `{"$type":"NodeId","value":"ns=2;i=5"}` |
| QualifiedName / LocalizedText / StatusCode | tagged object with the relevant fields |
| arrays | JSON arrays, order preserved (dimensions kept in `Variant.Dimensions` form) |
| ExtensionObject (decoded) | `{"$type":"ExtensionObject","type_id":…,"body_encoding":"decoded","body":{…}}` |
| ExtensionObject (binary body) | base64 body + `"body_encoding":"binary-base64"` **and** a flagged note |
| unknown Python object | `{"$type":"Unsupported","python_type":…,"repr":…}` **and** a flagged note |
| cycle / too deep | `{"$type":"Cycle"}` / `{"$type":"MaxDepthExceeded"}` **and** a flagged note |

Flags are de-duplicated by (kind, detail) and summarised in `unsupported_encodings`; they are never silently
dropped and never abort an export.

## 8. Export documents

**JSON** (see `exporters/json_exporter.py` for the full schema): canonical nodes keyed by NodeId,
a separate reference list, the namespace URI table, source/security description, traversal config,
summary, inaccessible nodes, unsupported encodings, limitations. Values are stripped unless the snapshot was
downloaded with values enabled.

**CSV**: one row per node, reference in/out counts, value columns only when values were included,
`unavailable_attributes` as sorted `Attribute=StatusCode` pairs (same information as the JSON) and
`value_status`/`value_encoding` for a refused or undecodable Value, UTF-8 with BOM, quoting via the `csv`
module.

**NodeSet2 XML** (`opcua/nodeset2.py`): nodes *and* their dependencies are collected explicitly before
anything is written, then handed to the verified `asyncua.common.xmlexporter.XmlExporter`
(`build_etree` / `write_xml`). Namespace mappings come from the exporter, so the file's namespace table
is authoritative for every index it contains.

Dependency closure (`NodeSetDependencyCollector`) is a bounded, batched breadth-first search over the
snapshot edges plus lazily browsed unknown nodes. It follows:

* inverse hierarchical references → **parents** (so `ParentNodeId` always resolves),
* `HasSubtype` → **supertypes**, `HasTypeDefinition` → **type definitions**, `HasEncoding` → **encodings**,
* `HasProperty`/`HasComponent` forward for **type nodes** (members) and for **Method nodes**
  (`InputArguments`/`OutputArguments`), because a method without its argument metadata is not callable,
* the `DataType` attribute of every variable.

Namespace-0 nodes are recorded as `standard_dependencies` and never written. Everything that cannot be
resolved is reported with the relationship that needed it (`missing_dependencies`), and the budget is
reported as `budget_exhausted` instead of silently truncating.

Custom DataTypes are checked for a `DataTypeDefinition` before writing. Without one there is no way to
describe the structure in NodeSet2, so the DataType and its members are excluded — otherwise importers fail
with `BadParentNodeIdInvalid` — and reported as `unsupported_data_types` with the variables that use them.

Method argument values are transplanted into the main tree by a second exporter that reuses the main
namespace mapping, so they are present even when value export is off.

Validation is explicit about what ran: the file is re-parsed (well-formedness), namespaces and
`ParentNodeId` targets are checked, dangling references are listed, and XSD validation runs only when a schema
path is given *and* `lxml` is installed (`ValidationResult.status` = `not-validated` / `valid` / `invalid`).

The same pipeline is available without Qt as `opcua-export` (`cli.py`): argument parsing decides the
`TraversalConfig` and `NodeSet2Config`, `OpcUaService.download()` and `export_nodeset2()` do the work, and the
CLI only prints `NodeSet2Result.summary_text()` and optionally writes `to_dict()`. Certificate approval is the
same explicit decision as in the GUI, expressed as an existing trust-store entry, `--trust-server-certificate`
or a `--server-certificate` file whose SHA-256 must equal the advertised certificate.

`opcua-compare` (`cli_compare.py`) follows the same rule for a different question: it reuses the connection,
traversal and certificate-approval helpers of `cli.py`, downloads both snapshots through
`OpcUaService.download()` — one session per server, same identity — and only calls `compare_address_spaces()`
and prints `ComparisonResult.summary_text()`. `compare.py` itself is a pure function over two `AddressSpace`
objects: node identity is namespace URI + identifier, a renumbered NodeId is matched by its unambiguous
qualified browse path, values are compared only for nodes named in the config, and results land in the four
buckets `missing` / `extra` / `changed` / `not comparable`. Partial snapshots (cancelled, limits hit,
inaccessible nodes, a different identity) produce warnings instead of a false "equal".

The tolerance ladder is monotonic: `functional` (model attributes) ⊂ `strict` (+ display name and description)
⊂ `device` (+ the attributes that are not part of the data model — write masks, `EventNotifier`,
`Historizing`, `MinimumSamplingInterval`, abstract/symmetric flags, `DataTypeDefinition`, access
restrictions, role permissions, method executable flags). The device level is why
`TraversalConfig.include_device_attributes` exists: those attributes are read into
`NodeRecord.device_attributes` (as text) only when a download asks for them, so an ordinary export stays
small. `attributes.device_specs_for()` excludes everything `specs_for()` already reads, so every attribute
is read exactly once; the JSON document keeps the map (`format_version` 2, version 1 still loads) and the
comparison reports `device_attributes_compared: false` plus a warning when a side was downloaded without it —
"not looked at" is never reported as equality.

The reference comparison mirrors the same honesty rule: an edge is comparable only when both of its endpoints
are nodes of that snapshot, and the rest are counted in `reference_edges_not_compared` and named in a warning
instead of being dropped. A bounded download keeps many such edges (type definitions in namespace 0, nodes
under another root), so a comparison of two identical devices normally carries exactly that one warning. Either side can also come
from a JSON export (`exporters/json_loader.py`), which restores the download's `limits_hit` flags so a truncated
document is reported as partial rather than as "nodes missing on the other side".

## 9. Delivered scope and test-suite policy

Connection/security, event subscriptions with the one guarded condition acknowledgement and one unguarded
`ConditionRefresh`, server status and diagnostics, history reads (raw or one aggregate) with a trend chart drawn
from that result, the functional checks with reports, NodeSet2 export and the guarded write and method call are
delivered, together with address-space comparison and operator sequences; `PLAN.md` §1 is the authoritative
capability list.

Test-suite policy: a default run performs no write, a subscription that is created but reports no value change is not a
failure, no unrestricted stress testing, no automatic production-network scanning; results are described as
functional/interoperability checks, never as OPC UA certification.

## 10. Functional test design

`opcua/test_runner.py` runs eleven checks in order against the *existing* session, so no re-authentication and
no security change happens during a run:

```
endpoint_discovery → session_state → namespace_array → browse_root → browse_target
→ attribute_reads → variable_read → subscription_create → subscription_data_change
→ latency → connection_recovery (opt-in, last)
```

Each check is isolated: an exception marks that check failed with the exception type, a `CheckSkipped`
precondition marks it skipped with a reason, and the run continues. A cancelled run returns partial results
(`cancelled: true`) after releasing monitored items and the subscription.

Semantics worth calling out:

* **Creation vs. notification.** `subscription_create` proves `CreateSubscription` works and records the
requested publishing interval plus the effective value asyncua reports after a server revision.
`subscription_data_change` proves monitored items are delivered. OPC UA sends
one *initial* value notification when a monitored item is created, so a constant variable yields one
notification and zero changes: that is reported as passed with an explicit "no value change occurred within
the observation window (this is not a failure)" note. No notification at all is *skipped*, never failed.
* **Bounded latency.** 1–200 sequential reads measured with `perf_counter`; min/median/max are data, not gates
  (only non-Good StatusCodes fail the check).
* **Opt-in recovery.** `connection_recovery` closes the secure channel, records the detected loss, reconnects
  with the same client and verifies a read. It runs last because the interruption affects any active
  subscription. When automatic reconnect is enabled the supervisor performs the retry and restores client
  subscriptions; the check waits for state `connected` plus a successful read instead of calling `connect()` a
  second time.
* **Cleanup.** Monitored items are unsubscribed and the subscription is deleted at the end of the run (and on
  cancellation), so the session stays usable.

`reporting.py` renders the same facts twice: a versioned JSON document and a self-contained HTML page. Every
dynamic string is HTML-escaped (server-supplied names end up in reports), the page embeds no external assets,
and the status pills use contrast-checked colours.

## 11. Guarded write and method call

There are three operator-facing surfaces for a write or a call — the guarded checks in the **Tests** tab, the
interactive **Write & call** page (`gui/tabs/write_tab.py`) and a sequence document step — and all three call the
same `opcua/write_service.py` primitives. The interactive page adds one wrinkle: it previews (`write_preview` /
`call_preview` / `attribute_write_preview`), confirms, then sends, and reports the write, its read-back and the
optional restore as one `WriteResult`.

Both operations live in `opcua/guarded_operations.py` (the runner schedules them through the same plan as the
verification checks) and share three rules:

1. **Opt-in, per target.** `TestConfig.allow_writes` is a master switch that is off by default. Each entry in
   `write_targets` / `method_calls` is one node and one operation; the runner never enumerates writable nodes,
   never calls a discovered method and never repeats an operation.
2. **Confirm the exact operation.** The Tests tab builds the confirmation text from the *validated config* (not
   from the widgets) and shows endpoint, target NodeId, value or method + argument list, the restore decision
   and the physical-equipment warning. Declining the dialog runs nothing at all. The runner also records
   `confirmed_by_operator` in the check data, so a report can never look like an unconfirmed write.
3. **Validate against the server, then verify the effect.** Values are parsed with the node's **DataType** and
   **ValueRank** (`opcua/value_parsing.py`); method arguments are parsed with the server's `InputArguments`
   (`ua.Argument.DataType` / `ValueRank`). Nothing is sent when parsing fails — the check is *skipped* with the
   reason. A write is verified by reading the value back; a mismatch is a **failure**, not a pass.

### Metadata attribute writes

`prepare_attribute_write` / `send_prepared_attribute_write` are the **only** non-Value write path, reachable from
the **Write & call** page (`OpcUaService.attribute_write_preview` / `write_attribute_value`):

* Only `DisplayName` and `Description` are writable (`TEXT_ATTRIBUTES`), matched case-insensitively; the value is a
  `LocalizedText`, and the request is refused locally when the text is empty.
* One `write_attribute` call, then a read-back through `read_attribute(..., raise_on_bad_status=False)` and a text
  comparison — a differing read-back is a failure, an unreadable one `outcome_unknown`.
* Servers restrict non-Value writes: asyncua's attribute service answers `BadUserAccessDenied` for every non-Admin
  user, which is why the demo server refuses and an admin-role test server accepts. Structural attributes
  (`WriteMask`, `AccessLevel`, `DataType`, …) stay out of scope; node management
  (`opcua/node_management.py`: one `AddNodes`, one `DeleteNodes`) is a separate guarded path with its own
  authority, never a structural write.

Condition acknowledgement is the only guarded operation outside a run: the Events tab builds its confirmation
text from `operator_text.acknowledge_condition_confirmation()` and sends one `Acknowledge` call through
`write_service.acknowledge_condition()`, which validates the method's `InputArguments` first. It is not gated
by `TestConfig.allow_writes` because it is never part of a check run; the per-call dialog is the opt-in. It
never calls `Confirm`/`Enable`/`AddComment`, never retries, and an acknowledged event is not proof that the
process condition has cleared. The outcome is emitted as a session summary (`EventsTab.report_ready`), so the
shared log records what was acknowledged; a declined confirmation sends nothing and records nothing.

The Events tab's second condition action is deliberately **not** guarded: **Refresh conditions** sends one
`ConditionRefresh` call through `write_service.refresh_conditions()` and needs no dialog because it stores
nothing and interrupts nothing. It is still one call on one subscription, is never retried, records
`WriteAuthority("operator-dialog", …)`, and reports the server's `RefreshStart`/`RefreshEnd` events as the only
evidence that it arrived (`operator_text.condition_refresh_text()` explains this to the operator).

Restoring a previous value is a separate, explicit choice (`restore_previous_value`, off by default). When it is
on, the runner reads the previous value before writing, writes it back afterwards through
`write_service.restore_value()` — the same one write plus read-back, with the same `authorization` — and
verifies the restore; a failed or unverifiable restore is reported as a failed check even though the write
itself succeeded.

Supported write DataTypes are resolved from the server in `opcua/type_support.py`:

* namespace-0 built-ins (Boolean, integer types with range checks, Float/Double, String, DateTime, Guid,
  ByteString as base64, NodeId/ExpandedNodeId, StatusCode, QualifiedName, LocalizedText, XmlElement);
* **enumerations** by name or number, read from `DataTypeDefinition` (`EnumDefinition`) or, on older servers,
  from the `EnumStrings` / `EnumValues` property — sent as `Int32`;
* **structures** from `StructureDefinition`, encoded in field order and sent as an `ExtensionObject` whose
  `TypeId` is the structure's `DefaultEncodingId`; nested structures and rank-1 arrays are supported.

Arrays are entered as JSON lists (a structure as a JSON object). Matrices, unions, optional-field structures and
custom DataTypes without a readable definition are **refused** rather than encoded on a guess.

The single write/call/restore implementation is `opcua/write_service.py` (`prepare_write` →
`send_prepared_write`, `prepare_call` → `send_prepared_call`, `restore_value`), shared by
`guarded_operations.py` and the sequence runner, so a value is typed once and a cancellation can be reported
with `sent=True` instead of as a plain failure. Every sending function takes a keyword-only
`authorization: WriteAuthority` without a default: `guarded_operations.py` records `operator-dialog`
plus the check id in `check.data["authorization"]`, the sequence runner records `sequence-document` plus the
phase and step id on the step result. `tests/unit/test_layer_boundaries.py` scans the package for
`write_value` / `call_method` calls outside `write_service.py` (the demo server writes its own model) and
asserts that `authorization` has no default.

A target may also be addressed by browse path: `opcua/path_support.py` builds
`TranslateBrowsePathsToNodeIds` requests (bounded batches, `Name` or `index:Name` per element) and accepts only
a **complete, unambiguous** match; no match, a partial match or several targets are reported and must be
replaced by an explicit NodeId. `OpcUaService.resolve_browse_paths()` exposes the same helper, and sequence
steps use it through `node_path` / `path_root`.

### 11.1 Operator sequences

`sequence_document.py` parses and validates a `opcua-explorer-sequence` document once, before execution; the GUI
tab, the project file and the CLI all use it. The document owns everything device-specific: `preconditions`, the
gate (`on`, `verify`, `keepalive` + interval + `abort`/`warn`, `off`) and the body. `opcua/sequence_runner.py`
only enforces mechanics: `gate.off` runs in `finally` (success, failed step, cancellation, keepalive abort), a
failed `gate.verify` prevents the body, each step is sent once, a cancelled in-flight write/call is
`outcome_unknown`, and one sequence runs at a time per session (`OpcUaService` keeps one runner per client and
drops it on disconnect). `--dry run` resolves nodes, DataTypes, `InputArguments` and `Executable` and sends
nothing.

`gui/tabs/sequences_tab.py` loads the document, edits `${name}` inputs, previews the steps, and shows a live run
timeline plus the gate outcome; **there is no armed state and no per-write dialog** — a write happens because a
step asks for it and is read back. `cli_sequence.py` (`opcua-run-sequence`) is the Qt-free front end over the
same service call. The project stores the document path and inputs (project version 2; version 1 still loads);
opening a project never loads or runs a sequence.

### Node management

`opcua/node_management.py` is the only module that sends **AddNodes** or **DeleteNodes** (enforced by
`tests/unit/test_layer_boundaries.py`, like the write path):

* `build_add_node_item()` turns a `models.NodeCreateRequest` into one `AddNodesItem`: parent, reference type
  (`Organizes` for an Object, `HasComponent` for a Variable), `RequestedNewNodeId` with a **null identifier** (the
  server assigns it), and `Object`/`VariableAttributes`. A variable's initial value is parsed with the server's
  DataType/ValueRank through `type_support`, so a mismatch is refused before anything is sent.
* `create_node()` sends one request, then reads the new node's DisplayName and compares it with the request;
  `delete_node()` sends one `DeleteNodesItem` with `DeleteTargetReferences=True` and verifies the node is gone
  (a BadNodeIdUnknown on read is the proof of success).
* Every send requires a keyword-only `authorization: WriteAuthority`; the GUI previews, confirms and reports.
  There is no batch, no child check, no undo and no retry; a ServiceFault (`BadUserAccessDenied` for a non-admin
  identity) is surfaced with its StatusCode.

## 12. Deliberate limitations

* History is raw or one aggregate only: no read-at-time, no event history and no `HistoryUpdate`; a
  point-in-time read remains the value path everywhere else.
* Background reconnection is a session behaviour, not a test result: the `connection_recovery` check stays
  opt-in and disruptive, while the Connection tab's **Resilience** toggle retries a dropped link automatically.
* The test suite covers one session, one browse root and one variable.
* Writes and method calls are single-target and opt-in; there is no batch write, no restore-all and no
  stress/scan mode. A sequence may contain several steps, but each step is still one operation on one target and
  nothing is retried.
* Sequences are declarative, not scripts; a keepalive failure aborts between steps, matrices/unions are refused,
  and `opcua-run-sequence` is not part of a PyInstaller bundle yet.
* Sessions share one window and one asyncio loop. Each session has its own client, namespace table, address
  space, live subscription and reconnect supervisor; the shared **Compare** tab compares two of them without
  owning a client or a subscription (it downloads through the selected sessions' services), and a server may
  refuse a second session (`BadTooManySessions`).
* Custom structures are exported as far as asyncua decoded them; binary ExtensionObject bodies are flagged.
* Namespace filter keeps namespace 0 to preserve the standard type system and folder structure.
* Download completeness depends on the session's rights; access-denied nodes are reported, not hidden.
* The NodeSet2 exporter writes what the session could read; it is not a model authoring or merging tool.
  Attributes asyncua's exporter cannot write (write masks, access restrictions and role permissions,
  `Symmetric`/`IsAbstract` of custom reference types, `IsAbstract` of custom data types) and skipped `View`
  nodes are listed in the result's `limitations`.
