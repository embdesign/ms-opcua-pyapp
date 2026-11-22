# AGENTS.md — working rules for `ms-opcua-pyapp`

Scope: `/home/user/wdr/py/ms-opcua-pyapp` (distribution `opcua-explorer`, package `opcua_explorer`).
Read this before changing code. It records only facts verified in this tree.

## Project facts

- Python **3.11–3.13**, managed with **uv** + `pyproject.toml` (hatchling, `src/` layout).
- Runtime dependencies are pinned: `asyncua==2.1.0`, `PySide6==6.11.3`, `cryptography==50.0.2`. Optional extra `validation` = `lxml`.
  Dev group: `pytest`, `pytest-asyncio`, `pytest-xdist`, `lxml`.
- Desktop OPC UA client: discovery/security/identity, lazy browser, bounded address-space download,
  JSON/CSV/NodeSet2 export, headless NodeSet2 export (`opcua-export`), live monitoring, read-only functional
  checks, guarded single-target write/method call, JSON/HTML reports, address-space comparison (`opcua-compare`
  and the Compare tab), operator-written control sequences (`opcua-run-sequence` and the Sequences tab), demo
  server. One window may host several independent sessions.
- Validation document: `docs/api-verification.md`. Never invent an asyncua API or behaviour; verify it there
  or against the installed package first.

## Commands (verified)

```bash
uv sync                                   # create .venv + dev group (use --reinstall after moving the folder)
uv run pytest -q                           # full suite, 832 passed (≈8–15 min depending on host load)
uv run pytest -n auto --dist loadscope     # same suite in parallel (module-scoped qapp stays on one worker)
uv run pytest tests/unit -q                # fast subset, 609 passed, ~8 s
uvx ruff check src tests scripts           # clean; config lives in pyproject.toml
uv run opcua-explorer                      # launch the GUI
uv run opcua-export --help                 # headless NodeSet2 export (Qt-free)
uv run opcua-compare --help                # headless comparison of two servers (Qt-free, read-only)
uv run opcua-read --help                   # headless attribute read (Qt-free)
uv run opcua-monitor --help                # headless live data-change monitor (Qt-free)
uv run opcua-events --help                 # headless events + ConditionRefresh/confirmed Acknowledge (Qt-free)
uv run opcua-history --help                # headless raw-history read (Qt-free)
uv run opcua-write --help                  # headless guarded single write (Qt-free, needs --confirm)
uv run opcua-check --help                  # headless functional checks + Reports tab suite (Qt-free)
uv run opcua-status --help                 # headless ServerStatus/diagnostics read (Qt-free, read-only)
uv run opcua-nodes --help                  # headless single-node create/delete/attribute write (needs --confirm)
uv run opcua-directory --help              # headless FindServers directory lookup (Qt-free, no session)uv run opcua-certificate --help           # headless client certificate / CSR creation (Qt-free, local files)uv run opcua-run-sequence --help           # headless run of a sequence document (Qt-free)
uv run opcua-demo-server                   # demo server on a free/selected port
uv run python scripts/generate_example_exports.py
uv run python scripts/generate_example_report.py
uv run python scripts/generate_example_nodeset.py
uv run python scripts/generate_example_comparison.py   # examples/sample-comparison*
```

- **Standalone bundles** are built by PyInstaller from `packaging/*.spec`
  (one folder, `opcua-explorer` + the Qt-free `opcua-export` and `opcua-compare`). CI does it in
  `.github/workflows/package.yml` (`ubuntu-22.04` + `windows-latest`) and
  `release.yml` attaches the zips to a `v*` tag. The demo server and `opcua-run-sequence` are deliberately
  not bundled. `packaging/README.md` records the constraints.

- Integration tests start in-process demo servers on free loopback ports; they need no Docker and no sudo.
- GUI tests run headless (`QT_QPA_PLATFORM=offscreen`) and are split by concern (`test_gui_smoke`,
  `test_gui_workflows`, `test_gui_monitor`, `test_gui_events`, `test_gui_server`, `test_gui_history`,
  `test_gui_write`, `test_gui_directory`, `test_gui_read_options`, `test_gui_browse_filters`,
  `test_gui_connection_states`, `test_gui_project`, `test_gui_compare`, `test_gui_sequences`;
  `test_pyside_stability` guards the Qt pin). Shared helpers live in `tests/integration/gui_support.py`; the
  module-scoped `qapp` fixture lives in `tests/integration/conftest.py` and closes/deletes top-level widgets after
  each module. Do not remove that cleanup.
- `tests/support.py` creates throw-away certificates; `cryptography` is an explicit dev dependency.

## Architecture constraints — do not break

- Layering (enforced by `tests/unit/test_layer_boundaries.py` for exporters):
  `gui/` → `opcua/` → `models/` (package) / `config.py` / `defaults.py` / `project.py` / `formatting.py` /
  `operator_text.py` / `trend.py`.
- `models/` is a package with one module per feature cluster (`common`, `discovery`, `graph`, `values`, `writes`,
  `functional_tests`, `monitor`, `events`, `history`, `server_status`, `sequences`); `__init__.py` re-exports every
  public name, so callers keep using `from opcua_explorer.models import ...`. The package, `config.py`,
  `defaults.py`, `project.py`, `formatting.py`, `operator_text.py`,
  `sequence_document.py`, `exporters/`, `reporting.py`, `compare.py`, `trend.py` import neither asyncua nor Qt.
  `exporters/` uses `formatting.py`, never `opcua/`.
- Numeric defaults live once in `defaults.py`; do not re-introduce literals in models, project state or tabs.
- Only `opcua/session_setup.py` constructs an `asyncua.Client`; one instance per session. The two
  session-less services (GetEndpoints discovery and the FindServers directory) use a short-lived client on a
  temporary `SecurityPolicyNone` channel **inside that module** and close it again — no other module may create
  a client. `OpcUaService` keeps the client/session state and delegates discovery/connect to it.
- Subscription lifecycles live in `opcua/monitor_session.py` and `opcua/event_session.py`; browse/read operations
  live in `opcua/node_reader.py`; NodeId normalisation lives in `opcua/node_ids.py`. `OpcUaService` keeps the
  public methods and delegates. Put new operations in the collaborator, not in the façade.
- Only `gui/async_runner.py` bridges asyncio and Qt. Widgets never `await`; all OPC UA work runs on the single
  worker loop and results are plain dataclasses delivered through queued signals. Blocking file work (project
  documents, log export) goes through `AsyncRunner.submit_call()`, never a widget slot.
- Session pages inherit `gui/tabs/base.py::TabRuntime` / `SessionTab` for `_set_status`, `_submit`,
  `_cancel_tasks` and `shutdown`. A page caches `_connected` / `_endpoint_url` from `set_connected(...)` and must
  not read `OpcUaService` state from the GUI thread; only `ConnectionTab` (which owns the lifecycle) does.
- `gui/main_window.py` is a shell (session tabs, menus, status bar, shared tabs). A session's state and Qt
  wiring live in `gui/session_workspace.py`. New cross-tab dependencies are injected by the workspace
  (see `selection_provider`), never looked up through `MainWindow` compatibility properties.
- **Layout is a contract too.** A session's pages are ordered by the on-site workflow (Connection, Server,
  Address space, Live monitor, Events, History, Write & call, Tests, Sequences, Export) — `test_gui_smoke`
  asserts that list, so a new page means updating it there. The description at the top of a page is a
  `gui/tab_notice.py::TabNotice` (one line; the full text is its tooltip and **View → Show tab descriptions**,
  persisted as `show_tab_descriptions`), never a raw paragraph `QLabel`. Secondary controls go into an
  `OptionsRow` whose toggle names every non-default value it is hiding (`Options (average) ▸`), so a collapsed
  row can never shape a request silently; a hidden filter must still be named in the status line of the action
  it affects. A page that acts on a node puts a `gui/selection_marker.py::SelectionMarker` next to its target
  field; it is visible **only** while that field differs from the browser selection (the workspace refreshes all
  markers from `BrowserTab.selection_changed`), so a stale target is never silent and no space is spent while
  they agree. `tests/integration/test_gui_layout.py` enforces the notice, options and marker rules.
  Two further honesty rules: the Address space **row filter** only hides rows that are already loaded,
  never browses, says so in its status line, keeps the start node visible and re-applies itself to a
  branch that is loaded later (`Search downloaded nodes…`/Find stays the whole-address-space path); and
  the Connection page's **recent-endpoint picker** (`AppSettings.recent_endpoints`/`remember_endpoint`)
  only fills the URL field — selecting an entry never discovers, connects or sends anything.
- The shared **Compare** tab (`gui/tabs/compare_tab.py`) owns no client and no subscription: it downloads
  through the two selected sessions' `OpcUaService` and calls `compare_address_spaces()`. Keep it read-only and
  do not add a second comparison implementation — GUI and `opcua-compare` share the config, the comparison and
  the report writers.
- Shared value/name conversions live in `opcua/ua_names.py`; do not add another local copy of
  `node_class_name` / `status_name` / `variant_type_name`.
- Snapshots stay two-tiered: `NodeRecord` models the data model, and
  `NodeRecord.device_attributes` holds the details-panel attributes (write masks, `EventNotifier`,
  `Historizing`, sampling interval, type flags, `DataTypeDefinition`, access restrictions, role
  permissions) as text. They are read **only** when `TraversalConfig.include_device_attributes` is set
  (Export tab checkbox, `opcua-export --device-attributes`, implied by `--tolerance device`), because
  they multiply the reads of a download; `opcua/attributes.py::device_specs_for` guarantees every
  attribute is read exactly once. The comparison's `device` tolerance is the only consumer and must say
  when a side was downloaded without them (`ComparisonResult.device_attributes_compared`) instead of
  reporting equality.
- The reference comparison only compares edges whose two endpoints are nodes of that snapshot
  (`compare.py::_reference_differences`); the others are counted in
  `ComparisonResult.reference_edges_not_compared` and reported as a warning. Never drop them silently
  again — a reference to a node outside the download was previously invisible.
- Explicit Value reads with options live in `opcua/value_read.py`
  (`build_read_parameters` → `read_value_with_options`), reached through
  `OpcUaService.read_value_with_options`. The Address space panel must not build `ReadParameters` itself, and a
  NumericRange is validated there before it is sent.
- Browse filters live in `models.BrowseOptions` and `opcua/browse.py::browse_children(..., options=...)`;
  `OpcUaService.browse_children(node, options=...)` is the only path the tree uses. Keep the
  `BrowserTab._tree_generation` guard: a cleared `QTreeWidget` deletes its items, so a late browse result must be
  dropped instead of touched — and a dropped result must **not** clear `_loading`, because that marker belongs to
  the newer generation's browse. A GUI test that switches filters must wait for the root item's `ITEM_LOADED`
  flag (`tests/integration/test_gui_browse_filters.py::_root_loaded`), never for an empty tree: `load_node`
  clears the tree, so "no rows" also means "still browsing".
- Functional check primitives live in `opcua/check_support.py`; guarded write/method-call checks live in
  `opcua/guarded_operations.py`. `opcua/test_runner.py` keeps the read-only checks, orchestration and the
  public `FunctionalTestRunner` / `run_functional_tests` / `CheckSkipped` / `environment_info` names.
- History is read-only: `opcua/history_service.py` is the single `HistoryRead` implementation
  (`build_history_request` → `read_history`). It uses `session.history_read` (there is no `Client.history_read`
  in asyncua 2.1.0), releases continuation points, and never sends `HistoryUpdate`. Raw is the default; one
  aggregate (average/minimum/maximum/count) uses `ReadProcessedDetails` and requires both start and end
  (`AggregateType` is a NodeId list). Do not add at-time, event-history or other aggregate paths without a
  verified asyncua API; a server that does not support aggregates is reported with its StatusCode.
- There is exactly **one** write/method-call/restore implementation: `opcua/write_service.py`
  (`prepare_write` → `send_prepared_write`, `prepare_call` → `send_prepared_call`, `restore_value`, and the only
  non-Value path `prepare_attribute_write` → `send_prepared_attribute_write` for `DisplayName`/`Description`). The
  guarded checks, the sequence runner and the **Write & call** tab (`gui/tabs/write_tab.py`) all use it; do not
  add another path that parses and sends a value.
  Every sending function requires a keyword-only `authorization: WriteAuthority` (no default), so a write can
  never be sent without naming the operator dialog or the sequence document that asked for it.
- **AddNodes/DeleteNodes live only in `opcua/node_management.py`** (`build_add_node_item` → `create_node`,
  `delete_node`), reached through `OpcUaService.node_create_preview` / `create_node` / `delete_node`. No other
  module may call `add_nodes`/`delete_nodes`; the boundary test enforces it and requires the same keyword-only
  `authorization`. One node per request, a server-assigned NodeId, no child check, no batch, no undo, no retry.
- `gui/tabs/write_tab.py` is a thin surface over `OpcUaService.write_preview` / `write_node_value` /
  `method_signature` / `call_preview` / `invoke_method` / `attribute_write_preview` /
  `write_attribute_value` / `node_create_preview` / `create_node` / `delete_node`. Its confirmation text lives in
  `operator_text.write_value_confirmation` / `method_call_confirmation` / `attribute_write_confirmation` /
  `node_create_confirmation` / `node_delete_confirmation`, and `ask_confirmation` stays a module-level function so
  tests can stub the dialog. Never bypass the preview: a refused value must not reach a confirmation dialog.
  `tests/unit/test_layer_boundaries.py` enforces both rules: no `write_value` / `call_method` outside
  `write_service.py` (the demo server excepted), and no default for `authorization`.
- The Events tab's condition methods are **guarded one-shot calls of the selected event**: `Acknowledge`,
  `Confirm`, `Enable`, `Disable` and `AddComment` all go through the single `write_service.condition_action()`
  with `WriteAuthority("operator-dialog", …)`, after the confirmation text in
  `operator_text.condition_action_confirmation` (which says what that one method does and does not do). The
  method is resolved below the condition node by its standard browse name (`models.CONDITION_ACTION_METHODS`),
  never by a NodeId the client invents; `InputArguments` must be (ByteString, LocalizedText) or nothing is sent;
  one action per call, never retried, and a method the condition does not expose is reported as missing. The
  client keeps **no condition state** (`AckedState`/`ConfirmedState`/`EnabledState` are never tracked or
  inferred), and there is no automatic acknowledgement. Event notifications stay read-only.
- The tab's **unguarded** action is **Refresh conditions**: it sends one
  `ConditionRefresh` (`i=3875`, one `UInt32` SubscriptionId) through `write_service.refresh_conditions`, with
  `WriteAuthority("operator-dialog", …)` recorded but **no dialog**, because it stores, acknowledges and changes
  nothing. `LiveEvents.refresh_conditions` composes a `ConditionRefreshResult` from the call outcome plus a
  bounded wait for the server's `RefreshStart`/`RefreshEnd` events; a missing event is reported, never claimed.
  There is no `ConditionRefresh2`, no retry and no second parent object: the parent is the ConditionType object
  (`i=2782`), and a refusal is reported with its StatusCode.
- Writes are typed from the server by `opcua/type_support.py` (built-ins, enumerations, structures) and targets
  may be addressed by browse path through `opcua/path_support.py` (complete, unambiguous matches only).
- Trend charts are **views of values the client already has**, never a second query: `trend.py`
  (`build_trend_series` for one `HistoryRead`, `append_live_point` for live notifications →
  `TrendSeries`/`TrendPoint`, `sparkline`) extracts the single numbers and counts what it skipped (arrays,
  structures, unparsable text), and `gui/trend_chart.py` only paints that series. It must never read, subscribe,
  interpolate or resample; `time_based` decides whether the x axis is time or row order, so a point without a
  timestamp falls back to row order instead of inventing a time. The Live monitor's chart plots the selected
  node on the client's **receive** time, keeps the `Keep rows` bound (capped by
  `defaults.MAX_LIVE_TREND_POINTS`), names the older points it dropped, and is cleared with the log because it
  is the same set of values; redraws are coalesced, never driven by the notification rate.
- `opcua/sequence_runner.py` is device-neutral: it never names a lock, a vendor or a method. A sequence document
  (`sequence_document.py`) owns the preconditions, the gate and the body. Mechanical rules that must stay:
  `gate.off` in `finally`, `gate.verify` before the body, one run per session, no retry, and
  `outcome_unknown` for a cancelled in-flight write/call. **There is no armed state and no per-write dialog** —
  a write happens because a step asks for it, is read back, and is recorded.
- `gui/tabs/sequences_tab.py` is the operator surface; it loads a document, edits `${name}` inputs, and runs or
  dry-runs it. `cli_sequence.py` (`opcua-run-sequence`) is a Qt-free adapter over the same service call.
- Safety invariants (do not weaken): no silent security downgrade (`select_endpoint` raises), explicit
  certificate approval (`set_security` always receives approved DER), no credentials in profiles/projects,
  secret redaction in logs, read-only checks by default, guarded operations only for explicitly configured and
  confirmed single targets, no automatic retry/restore of writes.
- Safety-relevant operator text (equipment warning, guarded confirmation, connection diagnostics) lives in
  `operator_text.py`, not in widgets. Edit it there and extend `tests/unit/test_operator_text.py`.
- `ConnectionProfile.to_settings()` is the only profile→runtime mapping; project validation and any new caller
  must use it instead of re-listing fields.

## Conventions

- Tests are plain functions. `python_classes = ["*Suite"]` exists so domain dataclasses named `Test*`
  (`TestConfig`, `TestCheck`, `TestRun`, `TestProgress`) are not collected — do not rename them casually.
- `tests/unit` must stay network-free and fast. Anything that starts the demo server or a GUI belongs in
  `tests/integration`.
- New pure helpers should accept plain dataclasses/enums and return strings or dataclasses, so they can be
  tested without Qt or a server.
- Public behaviour changes require updating, in the same change: `README.md`, `PLAN.md`,
  `docs/architecture.md`, `HANDOFF.md` when maintenance guidance changes, the relevant tests, and the sample
  files under `examples/` when their format changes.
- Do not change persisted formats without a version bump: project files (`format`/`version`), report JSON,
  CSV columns, NodeSet2 output, sequence documents (`opcua-explorer-sequence` + `version`). The project format is
  now **version 2** (a session stores the sequence document path, its inputs and `active_page`); version 1 files
  must keep loading, and a missing/unknown `active_page` falls back to the first page. Restoring a page is not a
  session restore: no project load may connect, subscribe or replay anything.
- Keep the PySide6 shutdown sequence in `gui/app.py` (`close`, `deleteLater`, `processEvents`); it prevents
  the interpreter-exit abort seen with live widgets.

## Pitfalls

- Do not commit without the operator's authorization; check `git status` and
  make a backup archive before bulk edits.
- `.venv` is required for `uv run`; `uv` lives at `~/.local/bin/uv` on this host.
- `uv run pytest` can fail with `error: Failed to spawn: pytest … No such file or directory` although pytest
  imports fine: the console script in `.venv/bin/` still carries the shebang of the folder it was installed
  from. Fix with `uv sync --reinstall`. Same symptom affects other console scripts.
- Windows-only paths (certificate handling, Qt platform integration) are not executable on this Linux host;
  only unit tests cover them.
- **Do not upgrade PySide6.** 6.12.0 aborts the process (`none_dealloc: deallocating None`) after a few hundred
  `QTreeWidgetItem` rows; the project pins `PySide6==6.11.3` and
  `tests/integration/test_pyside_stability.py` reproduces the churn in a subprocess. An upgrade must keep that
  test green — do not "fix" the symptom by removing item roles in `browser_tab.py`.
- Do not log passwords, private-key passwords, certificate bytes or trust decisions.
- **OPC UA `ValueRank` is not "0 = scalar"**: `-1` is a scalar, `0` means "one or more dimensions" and is what
  asyncua-based servers report for array variables (the demo server's `Samples`). `value_parsing.ARRAY_RANKS`
  and `looks_like_array` are the single place that decides this; comparisons and writes both go through
  `type_support`/`value_parsing` so a rank can never be re-interpreted per caller.
- `opcua/nodeset2.py` (dependency collection + export) and `opcua/nodeset2_validation.py` are still sizeable;
  target their helpers directly instead of growing the modules further.
- `compare.py` is a pure function over two `AddressSpace` snapshots: namespace **URI** + identifier identity,
  browse-path fallback only when unambiguous, values only for nodes named in the config, and a separate
  `not_comparable` bucket. Do not make it read, connect, or import asyncua/Qt; the front end does that.
- `cli.py` (`opcua-export`), `cli_compare.py` (`opcua-compare`), `cli_read.py`, `cli_monitor.py`,
  `cli_events.py`, `cli_history.py`, `cli_write.py`, `cli_check.py`, `cli_status.py`, `cli_nodes.py`,
  `cli_directory.py`, `cli_certificate.py` and `cli_sequence.py` (`opcua-run-sequence`)
  are Qt-free front ends, not second implementations: they must keep calling `OpcUaService.download()` /
  `export_nodeset2()` / `compare_address_spaces()` / `run_sequence()` and print the existing summary texts.
  `cli_compare.py` and `cli_sequence.py` import the connection, traversal and certificate-approval helpers from
  `cli.py`; do not copy them. Passwords are only accepted through `--password-env` or an interactive prompt,
  never as an argument, and a secure connection still requires an explicit certificate decision (trust store,
  `--trust-server-certificate` or a per-side `--reference-server-certificate` /
  `--candidate-server-certificate` with a matching fingerprint).
- The GUI test modules share mutable process state (one QApplication, demo-server subprocesses). Keep the
  `qapp` teardown and always stop a demo server in `finally` (use `gui_support.stop_demo_server`).
  `test_gui_smoke` asserts the workspace tab list; a new tab means updating it.
