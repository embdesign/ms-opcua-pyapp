# Changelog

All notable changes to **OPC UA Explorer** (`opcua-explorer`) are documented in
this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-11-22

### Added

- **`device` comparison tolerance: compare what tells two devices apart.** A structural comparison
  could not see write masks, `EventNotifier`, `Historizing`, `MinimumSamplingInterval`, abstract/symmetric
  flags, `DataTypeDefinition`, access restrictions or role permissions, because no snapshot contained
  them. `TraversalConfig.include_device_attributes` (Export tab *Include device attributes*, 
  `opcua-export --device-attributes`, implied by `opcua-compare --tolerance device`) now reads them into
  `NodeRecord.device_attributes`, `opcua/attributes.py::device_specs_for` keeps every attribute read
  exactly once, and the JSON document stores them (`format_version` 2; version 1 files keep loading).
  The tolerance ladder is now `functional` → `strict` (display text) → `device` (device attributes), and
  a comparison whose snapshots were downloaded without them reports
  `device_attributes_compared: false` plus a warning instead of claiming equality.
- **The comparison reports reference edges it could not compare.** An edge whose source or target is
  not part of that download was dropped silently, so a reference that existed on only one device but
  pointed outside the downloaded subtree was invisible. `ComparisonResult.reference_edges_not_compared`
  now counts them (per side, in `summary()`), a warning names them and suggests downloading a wider
  scope, and the HTML report shows the count as a note rather than as a difference.

- **Headless `opcua-check` command.** The Tests tab's suite over the same `run_functional_tests()` call, with the
  same JSON/HTML report writers: read-only by default (`--include-recovery` adds the disruptive reconnection
  check), `--include-writes --confirm --write NODEID=VALUE [--restore]` / `--call OBJECT|METHOD|ARGS` add the
  guarded checks for explicitly named targets, and it exits `3` when a check failed so a CI job reads the verdict
  from the exit code. `--json` prints the run's canonical document (not a second format).
- **Keyboard navigation for the two navigation levels.** **View → Next page / Previous page**
  (`Ctrl+PgDown`/`Ctrl+PgUp`) walk the ten pages of the visible session, and **Next session / Previous session**
  (`Ctrl+Tab`/`Ctrl+Shift+Tab`) walk the open sessions while skipping the shared tabs; both wrap around.
- **Headless `opcua-status`, `opcua-nodes` and `opcua-directory`.** The last three GUI-only actions now have a
  Qt-free twin: `opcua-status` prints the Server tab's one-shot ServerStatus/diagnostics snapshot (`--limits-only`,
  `--json`); `opcua-nodes` performs one `AddNodes`, one `DeleteNodes` or one `DisplayName`/`Description` write per
  invocation and refuses without `--confirm` (one node, no batch, no undo, exit `3` when the server refuses);
  `opcua-directory` prints what a discovery endpoint advertises with one `FindServers` call, opening no session.
  With them, every capability of the GUI is reachable headlessly.
- **`AttributeWriteOutcome` gained `summary_text()`/`to_dict()`**, so the attribute-write result prints and
  serialises like every other outcome record instead of needing a special case.
- **Drift guard for the headless front ends** (`tests/unit/test_cli_registration.py`): every `cli_*.py` needs a
  `[project.scripts]` entry point and vice versa, every entry point must resolve to a callable `main(argv)`, and
  every command must be named in `README.md` and `AGENTS.md`. It found `opcua-check` undocumented while it was
  being written, which is exactly the drift it exists for.
- **Headless `opcua-events` command.** The Events tab's counterpart over the same services: one event
  subscription (default source: the Server object) with the same server-side filtering (`--event-type`,
  `--select`, `--min-severity`), events printed as text or as JSON lines until `--count`/`--seconds`, and both
  condition actions — `--refresh` sends one `ConditionRefresh` and reports whether `RefreshStart`/`RefreshEnd`
  arrived, `--acknowledge EVENTID --condition NODEID --confirm` sends one `Acknowledge` call (without `--confirm`
  it exits `2` before connecting and sends nothing). The JSON objects carry the EventId and condition NodeId, so a
  condition found by one run can be acknowledged by the next. Exit codes match the other read commands (`3` when
  no event arrived and no condition action was asked for).

- **The project file remembers which page a session was on.** `ProjectSession` g
ained an optional
  `active_page` (`format` 2, extra key), so reopening a session continues on the p
age the operator was
  working on instead of always starting at Connection. Files without the key keep
 loading unchanged, and an
  unknown page title falls back to the first page.
- **Recent-endpoint picker on the Connection page.** `AppSettings` keeps the last
 ten successfully connected
  endpoints (`recent_endpoints`, newest first, deduplicated) and the Connection p
age offers them in a combobox
  next to the URL field. Selecting one only fills the field - it never connects,
 never discovers and sends
  nothing - and the picker is disabled while the list is empty.
- **Local filter box for the loaded address-space rows.** A "Filter loaded rows…
"
  field next to the browse filter hides the rows already in the tree whose name d
oes not match and keeps
  their ancestors visible. It browses nothing, so the status line states that unl
oaded branches are not
  searched (Search/Find remains the whole-address-space path), a branch loaded wh
ile the filter is active
  obeys it immediately, and the start node itself is never hidden - otherwise the
 filtered tree would look
  like an unloaded or childless node.

### Fixed

- **Attribute reads that were thrown away are kept.** `Executable`/`UserExecutable` were read for every
  method and then dropped because `NodeRecord` had no field for them; any device attribute read for the
  details panel had the same fate. `apply_attributes_to_record` now stores every attribute without a
  modelled field in `device_attributes`, and a refused one still lands in `unavailable` (test:
  `test_attributes.py::test_attributes_without_a_modelled_field_are_kept_as_device_attributes`).
- **A dropped live-monitor subscription is reported, not mis-encoded.** asyncua recreates a subscription the
  server has forgotten and briefly leaves its id as `None`; a following *Apply intervals* packed that `None` into
  `ModifySubscription` and raised `struct.error: required argument is not an integer` from inside the library.
  `LiveMonitor.set_publishing_interval` now refuses with `MonitorNotActiveError` ("the server no longer knows
  this subscription … start monitoring again") and sends nothing, which is what the operator needs to know.
- **The refresh observation window follows the publishing rate.** After a `ConditionRefresh` the client
  waited a fixed 2 s for `RefreshStart`/`RefreshEnd`, which is less than two publishing cycles when the
  subscription publishes slowly and was missed altogether on a loaded host. The window is now
  `max(3 s, 3 × publishing interval)` and the reported detail names the window and the interval, so a
  missing event stays a reported observation rather than a wrong verdict.
- **Address-space reload could look finished while it was still running.** A browse result
  belonging to a replaced tree cleared the in-flight marker of the *new* browse, so a filter
  change made right after another one could leave the tree unloaded and the page silent about
  it. A stale result now only drops itself, the completion status names the filter the result
  was produced with (`7 child node(s) read from ns=2;s=Line1 with Hierarchical references,
  Forward, Variable.`), and `tests/integration/test_gui_browse_filters.py` waits for the root's
  loaded flag instead of for an empty tree — an empty tree is also what a browse in flight
  looks like, which is what made that test report a wrong "Organizes returns nothing"
  expectation and fail intermittently under load.

### Changed

- **Layout: working area first.** The description at the top of every page is now one line (`TabNotice`); the
  full text is its tooltip and **View → Show tab descriptions** (persisted as `show_tab_descriptions`), which
  recovers 30–100 px of height on the read pages. The Events tab's server-side filter and the History tab's
  aggregate/interval/value cap moved into collapsible **Filter** / **Options** rows whose button names whatever
  is in effect (`Filter (severity ≥ 10) ▸`), and the action they belong to names them in its status line, so a
  hidden row can never be forgotten.
- **Tab order follows the on-site workflow**: Connection, Server, Address space, Live monitor, Events, History,
  Write & call, Tests, Sequences, Export. Server status (the first thing to check on a device) moved up from
  eighth, and Export moved last because it is a reporting step.
- **No silent stale target.** Every page that acts on a node (History, Events, Write & call, Export, Tests) shows
  a `SelectionMarker` next to its target field: hidden while the field matches the address-space selection, and a
  warning naming both NodeIds as soon as it differs. Each page keeps its own target — watching one node while
  writing another stays legitimate — and the marker never changes what is sent.
- **Server and Write & call use their space better.** The Server tab's status and monitored-item tables sit in a
  splitter instead of the second table having a fixed 150 px cap, and the Write & call page is laid out in two
  columns with its status line as a fixed footer, so all four confirmed actions are reachable at the minimum
  window size without scrolling.
- **`TraversalConfig.follow_subtypes` is now honoured.** The field was stored in every export document and
  restored by the snapshot loader while the downloader hardcoded `include_subtypes=True`, so a saved bound never
  shaped a request. The download passes the configured value to `browse_many`, and
  `tests/unit/test_traversal.py` asserts the bound reaches the browse call. The default stays `True`, so no
  existing download changes.

### Removed

- **Settings field `credentials_in_profiles`** — persisted as a constant `False` and never read; the rule it
  named is enforced where it matters, by `assert_no_secrets()` in `ConnectionProfile.to_dict()`. Older settings
  files keep loading (unknown keys are ignored).
- **Code nothing called**: `OptionsRow.add_layout` / `is_visible` / `active_summary`,
  `SecretRedactor.forget_all` / `secret_count`, `logging_setup.get_redactor()`,
  `theme.ACCENT_DARK` / `theme.SUCCESS` / `theme.WARNING` and the write-once `ProgressPrinter._last_written`.
  They documented a surface that had no caller; `uvx vulture src/opcua_explorer --min-confidence 70` now reports
  nothing (at 60 % only Qt overrides and the demo server's framework hooks remain).
- **Invalid documentation claims**: the maintenance notes claimed 626/633/472 tests (now 796 = 583 unit + 213
  integration), a fixed 2 s refresh window, `nodeset2.py` as the largest module (it is `write_service.py`) and a
  longest-test list naming a file that no longer exists; `docs/architecture.md` said node management was out of
  scope although `opcua/node_management.py` implements it.

### Added

- **History trend chart.** The History tab now draws the numeric values of one history read above the table
  (`gui/trend_chart.py` paints it, the new pure `trend.py` extracts it): a time axis when every value has a
  timestamp and row order when one does not, a hover readout, and a summary that names how many values were
  not plotted because they are not single numbers. It is a view of that one result — no second read, no
  interpolation and no resampling. `opcua-history --sparkline` prints the same series as one line of text.
- **`ConditionRefresh` on the running event subscription.** The Events tab gained **Refresh conditions**, which
  asks the server once to publish the conditions it already retains. `write_service.refresh_conditions()` sends
  the single standard method call (`i=3875` on the ConditionType object, subscription id as the only input) and
  `LiveEvents.refresh_conditions()` reports the call outcome plus whether the server's `RefreshStart`/
  `RefreshEnd` events arrived. No confirmation dialog (it stores and changes nothing), never retried, no
  `ConditionRefresh2`. The demo server now binds the standard condition methods.
- **Aggregate history reads.** `HistoryConfig` gained `aggregate` (average/minimum/maximum/count) and
  `processing_interval_ms`; `history_service.build_history_request()` then sends `ReadProcessedDetails` with the
  standard aggregate NodeId and `UseServerCapabilitiesDefaults = True`. Both From and To are required. The
  History tab gained an **Aggregate** selector and **Interval** field, and `opcua-history` gained `--aggregate`
  / `--interval`. A server without aggregate support (including the demo historian, which answers
  `BadNotImplemented`) is reported with its StatusCode and a hint.
- **Headless `opcua-write` command.** One variable, one value: preview (DataType/ValueRank + parsed text), then
  send only with `--confirm`, one write, read-back, optional `--restore` as one further verified write. Without
  `--confirm` nothing is sent (exit `2`); a failed or unverifiable write exits `3`; there is no retry or batch.
- **Headless `opcua-history` command.** One raw `HistoryRead` for one variable with `--from`/`--to` (ISO-8601),
  `--max-values`, and text, versioned JSON or CSV output. Exit codes match the other read commands (`3` for a Bad
  HistoryRead status).
- **Headless `opcua-read` and `opcua-monitor` commands.** `opcua-read` reads Value (or a named attribute) from one
  or more nodes with text or versioned JSON output; `opcua-monitor` runs one data-change subscription for a
  bounded time/count and prints each notification as text or JSON lines. Both reuse the shared connection,
  identity and certificate-approval helpers, never write, and are Qt-free. The shared connection/output argument
  groups moved into `cli.add_connection_arguments()` / `add_output_arguments()`.
- **Server-side event filtering.** `EventConfig` gained `select_fields` (extra EventType properties, with
  `index:Name` support) and `min_severity`; `event_service.build_event_filter()` builds an `ua.EventFilter` with
  the standard fields plus the extras and an optional `Severity >= n` WhereClause, and every source subscribes
  with it. The Events tab exposes **Extra fields** and **Min severity**; the filter is fixed when the subscription
  starts. Low-severity events are dropped by the server, not by the client.
- **Event subscriptions** (`opcua/event_service.py`) — one event subscription per session on explicitly added
  source nodes. The source's `EventNotifier` is monitored through `Subscription.subscribe_events`, so the
  server's event type decides which fields are requested (`BaseEventType` by default, a named custom type on
  request). Every selected field is reduced to bounded text in the asyncio thread and delivered as a plain
  `EventNotification`; a source that cannot publish events (no `EventNotifier`, unknown NodeId, unsupported
  service) is reported as a rejected `EventSourceInfo` with its StatusCode instead of aborting the subscription.
  Reconnect rebuilds the subscription, and stopping or disconnecting deletes it.
- **Events tab** — one page per session: add/remove event sources (the Server object `i=2253` is offered by
  default), choose the event type and publishing interval, then read a bounded event log (received time, event
  time, severity, type, source, message) with a per-field detail view for the selected event. Its only action is
  a confirmed condition acknowledgement (below); plain event monitoring publishes notifications only.
- **Demo server events** — `opcua-demo-server --event-interval SECONDS` publishes a demo event on the Server
  object at that rate (off by default), so an event subscription can be exercised locally and in tests.
- **Complete standard attributes in the address-space panel** — the details panel now reads and shows every
  standard attribute of the selected NodeClass: `EventNotifier` (decoded to `0 (none)` /
  `1 (SubscribeToEvents)`), `WriteMask`/`UserWriteMask`, `Historizing`, `MinimumSamplingInterval`,
  `IsAbstract`/`Symmetric`/`InverseName`, `ContainsNoLoops`, `DataTypeDefinition` and the optional
  `AccessRestrictions`/`RolePermissions`/`UserRolePermissions` (shown only when the server implements them).
  Bit fields display their member names, which also improves `AccessLevel`/`UserAccessLevel`. Downloads, JSON
  and CSV exports are deliberately unchanged: the extra attributes are read for the panel only.
- **Events tab: *Use browser selection*** — takes the source NodeId from this session's address-space tree,
  next to the manual entry and the default Server object.
- **Live monitor: `DataChangeFilter` and runtime subscription controls** — the Live monitor can request a
  trigger (`Status`, `StatusValue`, `StatusValueTimestamp`) and an absolute or percent deadband. Because
  asyncua's `subscribe_data_change` cannot carry a filter, a filtered subscription creates its monitored items
  through the public `create_monitored_items` path; a filter is fixed at start. While running, **Apply
  intervals** modifies the publishing interval (`ModifySubscription`) and every item's sampling interval
  (`ModifyMonitoredItems`), and **Server mode** applies `SetMonitoringMode`
  (`Reporting`/`Sampling`/`Disabled`) to all items. Refused modes and per-item refusals are reported; a server
  may revise the intervals, and the revised publishing interval is shown.
- **Condition acknowledgement** — a condition event from a `ConditionType`/`AcknowledgeableConditionType`
  subscription now carries its condition identity (`ConditionId`/`NodeId`, falling back to `SourceNode`) and
  EventId. Selecting it enables **Acknowledge condition…**: the operator may add a comment, sees the exact
  endpoint, condition node and EventId in the confirmation built by
  `operator_text.acknowledge_condition_confirmation`, and exactly one `Acknowledge` method call is sent through
  `write_service.acknowledge_condition` with `WriteAuthority("operator-dialog", …)`. The method's
  `InputArguments` must be (ByteString, LocalizedText) or nothing is sent; `Confirm`, `Enable` and `AddComment`
  are never called, nothing is retried, and a cancelled request sends nothing.
- **Server status & diagnostics tab** — one batched Read of the standard `ServerStatus` values
  (state, current/start time, uptime, shutdown fields), the `ServerDiagnostics` summary counters and the
  `ServerCapabilities` limits this client depends on (`MaxNodesPerRead`, `MaxNodesPerBrowse`,
  `MaxMonitoredItemsPerCall`, continuation points, sessions/subscriptions, sample rate). The tab also shows this
  session's live-monitor subscription and items as the client sees them. Optional values a server returns as
  Good-but-null are shown as *not provided*; bad StatusCodes are shown with their code. Nothing is written and
  no method is called — `GetMonitoredItems` is deliberately not invoked.
- **History tab / raw `HistoryRead`** (`opcua/history_service.py`) — reads the stored values of one variable
  through `HistoryRead` with `ReadRawModifiedDetails`: an optional From/To range (open range = `ua.get_win_epoch()`
  on both ends), a value cap (default 1000, maximum 10 000) and `TimestampsToReturn.Both`. Continuation points
  are followed up to 50 pages and released (also in `finally`), every `DataValue` is decoded through the shared
  `decode_data_value()`, and results are sorted oldest-first. A Good-but-empty answer is not an error: the
  result records the node's `Historizing` attribute and says whether the node does not historize or the range
  holds no values. Nothing is written and no `HistoryUpdate` is sent.
- **Write & call page** — an interactive page for one variable write and one method call, both through
  `opcua/write_service.py` with `WriteAuthority("operator-dialog", …)`. **Read node** shows NodeClass, DataType,
  ValueRank, access level and the current value; **Write value…** parses the text with the server's
  DataType/ValueRank, shows a confirmation built by `operator_text.write_value_confirmation` (endpoint, node,
  value, type, previous value, restore choice, equipment warning), sends exactly one write, reads it back and
  compares it, and — only when ticked — performs one further verified restore write. **Read signature** lists the
  method's `InputArguments` as editable rows; **Call method…** confirms the exact object/method/arguments, checks
  `Executable`, sends exactly one call and reports the output arguments. Nothing is retried and a refused value
  never reaches the confirmation dialog. `WriteResult` reports the write and the optional restore, and
  `CallOutcome.status_code` is now `Good` on a successful call.
- **Value read options: `IndexRange` / `MaxAge` / `TimestampsToReturn`** — the Address space *Value* panel now
  reads with options: a NumericRange slice (`1:3`, `0,2:4`; validated locally so a malformed range never reaches
  the server), a `MaxAge` in milliseconds (`0` lets the server answer from a cache) and a `TimestampsToReturn`
  choice (`Source`/`Server`/`Both`/`Neither`). `OpcUaService.read_value_with_options()` and
  `opcua/value_read.py` build one `Read` request for the Value attribute and report what was asked for next to
  the decoded value; whether the server honours a slice or the timestamp choice is stated as the server's
  decision, never assumed. The demo server accepts both and returns the whole value with both timestamps, which
  the client reports instead of pretending it sliced.
- **Write & call: metadata attribute writes** — the page can now change one node's `DisplayName` or
  `Description` (**Write attribute…**): the request is previewed, confirmed with the text and the current value
  (`operator_text.attribute_write_confirmation`), sent exactly once through
  `write_service.prepare_attribute_write` → `send_prepared_attribute_write` with
  `WriteAuthority("operator-dialog", …)`, then read back and compared. Only those two attributes are writable and
  the name is matched case-insensitively; an empty text or any other attribute is refused locally. Servers usually
  allow non-Value writes for administrators only, so asyncua's demo server answers `BadUserAccessDenied` —
  reported, never retried. `OpcUaService.attribute_write_preview` / `write_attribute_value` expose the path.
- **Address-space browse filters** — a filter row above the tree browses with an explicit reference type
  (`HierarchicalReferences`, `Organizes`, `HasComponent`, `HasProperty`, `HasChild`, all references), a
  `Forward`/`Inverse`/`Both` direction, optional subtype inclusion and a `NodeClassMask`. **Apply filter** reloads
  the current root with the selection and the status line names what was asked for; a late browse result that
  belongs to a replaced tree is discarded instead of touching a deleted item. `models.BrowseOptions` +
  `browse.browse_children(..., options=...)` carry the settings; the default browse (forward hierarchical
  references with subtypes) is unchanged, and an unparsable reference-type NodeId is refused locally.
- **Node management (AddNodes / DeleteNodes)** — the **Write & call** page gains a *Node management* group: create
  one `Object` or `Variable` below a parent (BrowseName, DisplayName, namespace, and for a variable a DataType plus
  an initial value parsed with the server's typing) or delete the target node. `opcua/node_management.py` is the
  only module that sends these services; every request is previewed, confirmed with
  `operator_text.node_create_confirmation` / `node_delete_confirmation`, sent exactly once under
  `WriteAuthority("operator-dialog", …)` and verified: a created node is read back (DisplayName) and appears in the
  parent's children, a deleted node is confirmed gone by reading it. The client never invents a NodeId (a null
  identifier asks the server to assign one), deletes target references with the node, checks nothing for children
  and offers no undo. asyncua's server permits both services for `UserRole.Admin` only, so the demo server answers
  `BadUserAccessDenied`.
- **Demo server history** — `opcua-demo-server --history` historizes Temperature and Speed in the server's
  in-memory historian, so the History tab can be exercised locally and in tests.
- **Find servers (FindServers directory)** — the Connection tab gains **Find servers…**: it sends one
  `FindServers` request to a discovery endpoint (an LDS, a GDS, or any server) on a temporary
  SecurityPolicyNone channel, so no session, identity or certificate decision is involved. The dialog lists each
  `ApplicationDescription` (name, application URI, type, first discovery URL, full tooltip), supports an optional
  application-URI prefix filter, and **Use selected URL** puts the chosen discovery URL into the endpoint field.
  An application without a discovery URL is listed but cannot be adopted; a refusing or silent endpoint becomes
  an actionable connection error. Nothing is registered, nothing is retried, and no application is connected to
  automatically.

### Changed

- **Shared bounds for intervals and row caps.** `defaults.py` now owns the publishing/sampling interval bounds, the
  notification window, the latency sample cap, the traversal concurrency/batch-size clamps and the keep-row limits
  used by both the models and their spin boxes, so a normalised config and its widget always agree.
- **Model and tab plumbing split for maintainability.** `models.py` is now the `models/` package (one module
  per feature cluster, with `__init__.py` re-exporting the same public names), the session pages share
  `gui/tabs/base.py`, and the content tabs cache their connection state from `set_connected(...)` instead of
  reading `OpcUaService` from the GUI thread. No public behaviour or persisted format changed.
- **Project and log file I/O runs on the worker loop.** `AsyncRunner.submit_call()` executes a blocking callable
  on the loop's executor. *File → Save/Open project* now captures or applies widget state on the GUI thread while
  the file read/write happens off it; the log export writes in the executor too. The synchronous
  `save_project` / `load_project` methods remain for tests and callers that need a completed write.
- **Connection establishment moved out of the façade.** `opcua/session_setup.py` now owns endpoint discovery,
  FindServers, client construction, security/identity setup and namespace loading; `OpcUaService.connect()` keeps
  validation, certificate checks and the disconnect-before-open ordering, then assigns the returned state. The
  single-client construction guard now points at `session_setup.py`.
- **`client_service` delegates subscription and read state.** The live-monitor and event-subscription lifecycles
  moved to `opcua/monitor_session.py` / `opcua/event_session.py`, browse/node-detail/value reads to
  `opcua/node_reader.py`, and NodeId normalisation to `opcua/node_ids.py`; `OpcUaService` keeps every public
  method and delegates. The three certificate pass-throughs were removed; the Connection tab uses the
  `TrustStore` and `summarize_certificate()` directly. No behaviour changed.
- **Test and error-handling hardening.** `AsyncRunner` no longer keeps a cancel marker for an already-finished
  task and now guards its cross-thread bookkeeping with a lock; `SequenceBusyError`, `SequenceAborted` and
  `MetadataUnavailable` derive from `OpcUaExplorerError`; boundary tests pin the single `Client(...)`
  construction site, the single GUI asyncio bridge and the asyncua-free CLI front ends. `pytest-xdist` is
  available for `-n auto --dist loadscope`; the default run is unchanged.
- **Documentation presents the client as a full OPC UA client.** `README.md`, `PLAN.md`,
  `docs/architecture.md` and `HANDOFF.md` no longer label the application or whole features "read-only": the
  wording describes what each pane does (a `FindServers` lookup, one batched `ServerStatus` read, raw
  `HistoryRead`, a structural comparison) and which operations a default test run sends. Write, method call and
  sequence execution are described as the full features they are. No behaviour changed.
- **NodeSet2 export reports what it cannot write.** `NodeSet2Result.limitations` now names `WriteMask` /
  `UserWriteMask`, `AccessRestrictions`, `RolePermissions` / `UserRolePermissions`, `Symmetric` / `IsAbstract`
  of custom ReferenceTypes, `IsAbstract` of custom DataTypes and skipped `View` nodes, because asyncua's XML
  exporter has no writer for them. The NodeSet2 summary text, the JSON manifest and the GUI/CLI output
  therefore state the omissions instead of presenting the file as lossless.
- **CSV export keeps value provenance.** The flat inventory gains `value_encoding` (`json` / `unsupported` /
  `unavailable`) and `unavailable_attributes` (sorted `Attribute=StatusCode` pairs, matching the JSON), and
  `value_status` now carries the StatusCode of a refused or undecodable Value instead of an empty cell. Readers
  that use the header row are unaffected; the JSON and NodeSet2 formats are unchanged. The example
  `examples/demo-address-space.csv` is regenerated.
- **JSON snapshots record the complete traversal configuration.** `source.traversal` now also stores
  `max_references_per_node`, `include_description` and `follow_subtypes`, and a loaded snapshot keeps those
  bounds. Existing version-1 documents keep loading unchanged (a missing key falls back to its default), so
  `format_version` stays 1.
- **Export tab reworked around completeness.** The new *Export content* group carries both switches — variable
  values and node descriptions (new, and persisted in the project's export state) — and a new **Export coverage**
  panel states what the downloaded snapshot contains, which options were off, the limits/inaccessible/refused
  counts, the attributes that are details-panel-only, and what each output format can carry. NodeSet2's own
  omissions remain in its export summary.

- **One write path, now enforced.** The optional "restore the previous value" write in the guarded checks runs
  through `opcua/write_service.py` (`restore_value`: one write + read-back, `outcome_unknown` when the effect
  cannot be checked) instead of sending on its own. Every sending function in that module takes a keyword-only
  `authorization: WriteAuthority` without a default — `operator-dialog` plus the check id for guarded checks,
  `sequence-document` plus the step for sequences — and records it on the outcome (and in the check data /
  step result). `tests/unit/test_layer_boundaries.py` now fails if a `write_value` / `call_method` call appears
  outside `write_service.py`, or if `authorization` gains a default.

### Fixed

- **Unreadable method metadata is no longer reported as "no arguments".** `read_input_arguments()` returns an
  empty list only when the `InputArguments` property is genuinely absent (`BadNoMatch`). A failed browse/read or a
  malformed argument entry now raises `MetadataUnavailable`, which the write/call, guarded-check and sequence
  surfaces already report as a refusal instead of sending a call with a wrong argument list.
- **Write & call keeps its layout when the window shrinks.** The page now lives in a scroll area, so its five
  groups keep their natural size and the page scrolls instead of squeezing labels, buttons and line edits.
- Labelled widgets show their literal ampersand again. Qt draws tab labels and group-box titles with
  `Qt::TextShowMnemonic`, so a single `&` was consumed as a mnemonic marker and the next letter was
  underlined: the `Write & call` and `Logs & reports` tabs rendered as `Write _call` / `Logs _reports`, and
  four group-box titles (`Client certificate & private key`, `Limits & batching`, `Timing & sampling`,
  `Guarded write & method call`) lost their `&`. These texts now use `&&`, and a session tab title taken from
  a project file or a server application name is escaped the same way. `QLabel` texts are unchanged because
  labels without a buddy render `&` literally.

## [0.1.1] - 2026-11-16

### Added

- **Address-space comparison core** (`opcua_explorer/compare.py`) — compares two
  downloaded snapshots structurally. Nodes are identified by namespace URI +
  identifier, so sessions with different namespace indexes compare correctly;
  renumbered NodeIds are matched by qualified browse path when that match is
  unambiguous; values are compared only for explicitly listed nodes (status,
  variant type, payload with optional float tolerance); an ignore list covers
  volatile nodes. Cancelled or limit-hit snapshots, inaccessible nodes and
  different identities produce warnings and a separate "not comparable" bucket
  instead of a false "equal" or a false difference.
- **`opcua-compare` command** (Qt-free, read-only) — downloads the reference and
  the candidate address space with the same identity, prints the comparison
  summary and writes a versioned JSON document and/or a self-contained HTML
  report. Exit code `3` means "compared, differences found", so the command works
  as a CI gate. It reuses the connection, traversal and certificate-approval
  helpers of `opcua-export`; `--candidate-policy` / `--candidate-mode` cover
  devices that advertise different endpoints.
- **Comparison sample reports** (`examples/sample-comparison.*` and
  `examples/sample-comparison-differences.*`) with
  `scripts/generate_example_comparison.py`.
- **Shared Compare tab** — pick a reference and a candidate session, download both with one scope and review the
  differences (missing / extra / changed / not comparable) in a filterable table with a per-node attribute
  detail, then export the same JSON/HTML reports the command writes. The tab is read-only, owns no client and no
  subscription, and inherits no session state from the other tabs.
- **Saved JSON exports as a comparison side** — `exporters/json_loader.py` reads the address-space document the
  Export tab (or `opcua-export`) writes back into a snapshot, preserving node attributes, references, values,
  the connection identity and the download's `limits_hit` flags. `opcua-compare` accepts `--reference-file` /
  `--candidate-file` (two documents compare completely offline), and the Compare tab lists *Saved JSON export…*
  for either side, so a retired device remains the baseline.
- **`opcua-compare` bundle** — `packaging/opcua-compare.spec` and a launcher build the Qt-free comparison command
  as a third standalone archive (`opcua-compare-<platform>.zip`); both workflows smoke-test it, including an
  offline comparison of the committed sample.
- **Operator sequences** (`opcua-explorer-sequence`, version 1) — an ordered device workflow (acquire, change,
  read back, release) is a document, not application code: `preconditions` (usually read-only checks, with an
  optional `ensure` that may set a prerequisite), an optional operator-defined
  `gate` (`on` / `verify` / `keepalive` / `off`, always released in `finally`) and a body of `write`, `expect`,
  `assert`, `ensure`, `call`, `delay` and `wait_until` steps, with `${name}` inputs. Values are typed from the
  server (DataType, ValueRank, `InputArguments`), including enumerations, structures and arrays; targets may be
  NodeIds or browse paths resolved through `TranslateBrowsePathsToNodeIds` (complete, unambiguous matches only).
  One send per step, no retry, `outcome_unknown` for a cancelled in-flight operation, no armed state and no
  per-write dialog. Surfaced by the **Sequences** tab (document, inputs, Dry run, Run, Stop, live timeline, gate
  state and an operator-triggered release), by `opcua-run-sequence` (`--dry-run`, `--input`, `--json`, exit codes
  0/1/2) and in the project file (version 2; version 1 files still load).
- **Sequence demo suite** — `examples/README.md` indexes six runnable documents (`demo-01-write-readback`,
  `demo-02-method-calls`, `demo-03-wait-and-optional`, `demo-04-gate-from-writes`, `demo-05-failure-handling`,
  `demo-06-value-types`) with their purpose and expected exit code; the demo server gained `--no-updates` and
  writable demo variables so those runs are deterministic.

### Fixed

- **`ValueRank 0` was treated as a scalar** — OPC UA defines `0` as "one or more dimensions", and asyncua-based
  servers report it for array variables, so writes to arrays were refused ("is not a number"). Ranks `0` and `1`
  are now a one-dimensional array, `-1` is a scalar, `-2`/`-3` are decided from the entered text and matrices are
  still refused.
- **Sequence comparisons bypassed the server-driven type resolution** — `expect`, `assert` and `wait_until`
  parsed the expected text with the built-in parser only, so an enum or structure node failed with
  "DataType … is not supported for guarded test writes". They now resolve the type exactly like a write does.
- **Qt runtime pinned to `PySide6==6.11.3`** — 6.12.0 aborts the process with
  `Fatal Python error: none_dealloc: deallocating None` after a few hundred `QTreeWidgetItem` rows (Shiboken
  refcount regression). `tests/integration/test_pyside_stability.py` reproduces the churn in a subprocess; an
  upgrade must keep that test green.

## [0.1.0] - 2026-11-14

First released version. Desktop OPC UA client for browsing, downloading and
exporting an address space, with deliberate security handling, live monitoring,
functional checks and a Qt-free export command.

### Added

- **Connection & security** — endpoint URL entry, endpoint discovery, selection
  of security policy and message security mode, anonymous / username-password /
  certificate identity, client certificate and key configuration, timeouts, and
  connection diagnostics.
- **Multi-session window** — every session lives in its own tab with its own
  `OpcUaService` and client (`File → New connection`, `Ctrl+N`); closing a tab
  releases only that session. Shared tabs: **All monitors** (read-only merged
  view of every session's items and notification log, with a session filter) and
  **Logs & reports** (summaries prefixed with the session name).
- **Automatic reconnect** — "Keep reconnecting when the link drops" (on by
  default) with a Resilience toggle, transient reconnect states shown as
  "Link lost · reconnecting…", monitor resubscription and namespace re-read after
  recovery; with reconnect off the link is reported lost once and the dead
  client released.
- **Address space browser** — lazy tree from Root/Objects or any NodeId,
  attributes panel (NodeId, namespace URI, resolved DataType name, ValueRank,
  AccessLevel, …), references with direction, explicit value read, copy NodeId,
  search inside a downloaded snapshot, and session-local **●**/**○** markers for
  nodes that are running or queued in the live monitor.
- **Bounded download** — graph-correct traversal (visited set, depth and node
  budget, concurrency, batch size), namespace filter, progress, cancellation
  with continuation-point release, and a limits report.
- **Export** — JSON (`format_version` 1, canonical document with separate
  forward-reference list and always-present namespace URI table), CSV (UTF-8 with
  BOM) and NodeSet2 XML. Values are excluded by default; when enabled they are
  encoded faithfully (arrays, `DateTime`, `ByteString`, `ExtensionObject`) or
  explicitly flagged as unsupported instead of stringified.
- **NodeSet2 export** — dependency closure (parents, supertypes,
  `HasTypeDefinition`, `HasEncoding`, type members, method argument properties)
  with namespace 0 referenced and never duplicated, missing dependencies
  reported with their reason, custom DataTypes without a `DataTypeDefinition`
  excluded and reported, self-validation after writing (namespace declarations,
  `ParentNodeId` resolvability, dangling references) and optional XSD validation
  (`uv sync --extra validation` installs `lxml`).
- **Headless export CLI** (`opcua-export`, Qt-free) — the same services,
  traversal, dependency closure, writer and self-validation as the Export tab;
  `--endpoint`, `--output`, repeatable `--node`/`--namespace`, `--max-nodes`,
  `--max-depth`, `--values`, `--no-dependencies`, `--max-dependencies`,
  `--schema`, `--manifest`, security options, `--quiet`/`--verbose`. Exit codes
  `0` success / `1` connection or export failure / `2` unusable arguments /
  `3` written but structurally invalid. Passwords are accepted only via
  `--password-env` or an interactive prompt, never as an argument.
- **Live monitor** — one data-change subscription over explicitly added NodeIds
  (also via **Monitor node** in the Address space tab); per-node table with the
  latest value updated in place plus an append-only, bounded notification log;
  pause/clear, add/remove while running, configurable publishing/sampling
  intervals.
- **Functional tests** — eleven bounded checks: endpoint discovery, session
  state and authentication, namespace array, browse root/target, attribute
  reads, variable read, subscription creation, monitored-item notifications,
  bounded read latency, and an opt-in connection-loss/recovery check that runs
  last.
- **Guarded write & method call** (off by default) — one target per operation,
  consent switch plus exact-target confirmation dialog, server-side DataType and
  ValueRank validation, method argument validation against `InputArguments`,
  read-back verification, and restore of the previous value only when
  explicitly requested and itself verified.
- **Reports** — JSON and self-contained, escaped HTML with endpoint, security
  configuration, authentication mode, UTC timestamps, durations, per-check
  StatusCodes, measurements, coverage limitations and environment details
  (application, asyncua, PySide6, Python, platform).
- **Project files** — versioned JSON (`format`, `version` 1, suffix
  `.opcua-project.json`) storing every session's connection profile, start node,
  export, monitor and test settings; never passwords, key passwords, certificate
  bytes or trust decisions. Opening a project never connects, subscribes,
  downloads, writes or runs anything.
- **Demo server** (`opcua-demo-server`) — graph-shaped model with a shared child,
  a cycle, a second namespace, a custom DataType and values of type Double,
  Boolean, String, array, DateTime, ByteString and ExtensionObject; anonymous,
  username and certificate-based setups.

### Changed

- All asyncua work runs on a single asyncio loop in a worker thread; the GUI
  thread never awaits and receives plain dataclasses through queued signals.
- Version is resolved from installed distribution metadata
  (`importlib.metadata`); `pyproject.toml` remains the single source of truth
  and is covered by `tests/test_version.py`.
- Numeric defaults live once in `defaults.py`; shared name/format helpers live in
  `opcua/ua_names.py`, `formatting.py` and `check_support.py`; safety-relevant
  operator text lives in `operator_text.py`.

### Security

- No silent downgrade: the selected policy/mode pair must be advertised by the
  server, otherwise the connection fails with the list of advertised
  combinations.
- Explicit certificate approval: the server certificate is parsed locally, its
  SHA-256 fingerprint shown, and nothing connects until the operator confirms
  it out-of-band; a changed certificate triggers a new review. Approved DER is
  the only input that bypasses asyncua's implicit certificate fetch.
- Credentials are never persisted (connection profiles have no password field
  and `assert_no_secrets()` re-checks the JSON before writing); passwords and
  key passwords are cleared from the form after connecting.
- Secret redaction in logs, including `password=…` patterns, PEM private-key
  blocks and formatted exception tracebacks.
- Read-only by default: browsing, download, export, live monitoring and the
  default test suite send only `Read`, `Browse`,
  `CreateSubscription`/`CreateMonitoredItems`, `DeleteMonitoredItems` and
  `DeleteSubscriptions`. Writes and method calls are single-target,
  confirmation-gated and never repeated or restored automatically.

### Packaging

- PyInstaller one-folder bundles from `packaging/*.spec` for Linux x64 and
  Windows x64: `opcua-explorer` (GUI) and `opcua-export` (headless, no Qt),
  built by `.github/workflows/package.yml` and attached to a GitHub Release by
  `release.yml` on a `v*` tag. The demo server is deliberately not bundled.
- Bundled and `uv`-run installations share settings, trust store and project
  files because both use the same per-user locations. Windows bundles are
  unsigned.

### Tests

- 260 tests: fast network-free unit tests (serialization, certificate summaries,
  endpoint selection, trust store, redaction, JSON/CSV shape, report documents,
  NodeSet2 validation, CLI argument mapping) and integration tests against
  in-process demo servers (discovery, authentication, browsing, bounded
  traversal on a cyclic graph, exports and NodeSet2 round-trip import, guarded
  writes and method calls, secure connections, certificate rejection, the
  functional suite, `opcua-export` end to end).
- Headless GUI tests (`QT_QPA_PLATFORM=offscreen`) cover the asyncio bridge,
  window wiring and real tab workflows including connection loss and automatic
  reconnect, two independent sessions and project save/load.
- `ruff` lint gate clean.

### Known limitations

- NodeSet2 export is not a model authoring tool: it writes what the session
  could read and reports what it could not (no merge, patch or model
  versioning).
- Writes and method calls are single-target and opt-in; only namespace-0
  built-in DataTypes can be written, custom structures and enumerations are
  refused rather than encoded on a guess.
- No stress or soak testing, no network scanning; latency is a bounded sample
  of ordinary reads.
- The connection-recovery check is opt-in and disruptive (it closes the secure
  channel and reconnects); it is separate from the session-level auto-reconnect
  toggle.
- The functional suite covers one session, one browse root and one variable
  NodeId; values are point-in-time reads (no HistoryRead).
- NodeSet2 value fidelity is limited by asyncua (some method-argument fields can
  be lost on re-import); argument properties themselves are always written.
- Concurrent sessions share one window and one asyncio loop and do not compare
  or merge data; a server that limits sessions can refuse a second one, in which
  case the StatusCode is shown and nothing is retried silently.
- A project file is a configuration snapshot, not a session restore: it records the
  endpoints, the sequence document and the page each session was on, but it never
  re-opens a connection, restores a subscription or replays a write.
- Not included by policy: unrestricted stress testing, automatic
  production-network scanning, automatic writes or method invocation, automatic
  value restoration, and any OPC UA certification claim.
