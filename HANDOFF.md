# Task Handoff — OPC UA Explorer (`ms-opcua-pyapp`)

Project repository `ms-opcua-pyapp`, distribution `opcua-explorer`, application version 0.1.1, package
`opcua_explorer`. This file records the current state, the rules a future change must respect, and what is still
unverified. It is not a change log. Do not commit without the operator's authorization; make out-of-tree
backups before bulk edits.

## Goal and acceptance criteria

`PROMPT.md` asks for a maintainable desktop OPC UA client on `asyncua` + PySide6, managed with `uv`, that browses
a server, downloads its address space, exports it (JSON/CSV/NodeSet2) and runs functional interoperability
checks — with verified asyncua APIs, pinned dependencies, native Windows/Linux support without Docker, a demo
server, unit/integration tests, example exports, sample reports and troubleshooting docs. Safety rules: never
downgrade security silently, explicit certificate approval, no credentials in profiles or logs, safe defaults (a
check run performs no write unless a target is enabled), guarded writes/method calls and sequences only after
explicit confirmation.

Completion criteria:

1. Every capability claimed in `README.md` / `PLAN.md` exists in `src/opcua_explorer/` and is covered by a test.
2. `uv run pytest -q` and `uvx ruff check src tests scripts` are green.
3. Persisted formats (project file, report JSON, CSV columns, NodeSet2) stay versioned and change only with a
   deliberate version bump.
4. `README.md`, `PLAN.md`, `docs/architecture.md` and `AGENTS.md` agree with the code.

## Current status

**Complete for the delivered scope.** Nothing is blocked and no authorization is pending.

626 tests pass (463 unit), ruff reports no findings, and the tree contains no debug leftovers. Remaining gaps
are verification-only (Windows and a manual desktop session) and are listed under *Known issues and review
findings* and *Next actions*.

**Qt dependency (2026-11-15).** `PySide6` is pinned to **6.11.3**. 6.12.0 aborts the process with
`Fatal Python error: none_dealloc: deallocating None` after a few hundred `QTreeWidgetItem` rows; the crash was
reproduced outside the application (public Qt API only) and is a Shiboken refcount regression. The reproducer
runs in `tests/integration/test_pyside_stability.py`; an upgrade must keep that test green. Details are in
`docs/api-verification.md` and `docs/troubleshooting.md` §7.

**Event subscriptions — delivered.** `opcua/event_service.py` owns one event subscription per session
(`Subscription.subscribe_events`, server-driven field selection); the Events tab reports a source that cannot
publish events per source with its StatusCode. Event monitoring publishes notifications; its one action is the confirmed
condition acknowledgement described below. `opcua-demo-server --event-interval SECONDS` emits demo events for
local runs and the tests. Event sources are not stored in project files. The Address space panel shows
`EventNotifier` for the selected node (details-only: it is not part of a download or JSON/CSV export).

**Live-monitor filters and runtime controls — delivered.** `opcua/monitor_service.py` builds a
`DataChangeFilter` and, because asyncua's `subscribe_data_change` has no filter parameter, creates filtered
monitored items through the public `create_monitored_items` path. While a subscription runs, the tab can apply
`SetMonitoringMode` (Reporting/Sampling/Disabled), modify the publishing interval (`ModifySubscription` via
`Subscription.update`) and every item's sampling interval (`ModifyMonitoredItems` via
`modify_monitored_item`). The demo server accepts the filter but does not implement deadband suppression, and
the server may revise any interval — tests must assert acceptance/flow, never deadband counts.

**Condition acknowledgement — delivered.** A `ConditionType` subscription makes `EventNotification` carry the
condition identity (`ConditionId`/`NodeId`, falling back to `SourceNode`) plus the EventId. The Events tab's one
action is `write_service.acknowledge_condition`: resolve the instance's namespace-0 `Acknowledge` method under
the condition node, require `InputArguments` (ByteString, LocalizedText), and send exactly one call with
`WriteAuthority("operator-dialog", …)` after the confirmation from
`operator_text.acknowledge_condition_confirmation`. No `Confirm`/`Enable`/`AddComment`, no retry, no automatic
acknowledgement. asyncua's demo server has no condition object; integration tests create a real object +
`Acknowledge` method at runtime, the GUI test drives the dialogs with a fake service call.

**Server status & diagnostics — delivered.** `opcua/server_diagnostics.py` reads the standard ServerStatus,
ServerDiagnosticsSummary and ServerCapabilities variables in one batched Read and surfaces them in the **Server**
tab. Good-but-null values are `not_provided` (asyncua's demo returns nulls for every counter), Bad values are
kept per name, and the monitored-item list is `LiveMonitor.items_snapshot()` — `GetMonitoredItems` is not called
because the project keeps exactly one guarded method-call path.

**Raw history — delivered.** `opcua/history_service.py` builds one `HistoryReadParameters`
(`ReadRawModifiedDetails`, `TimestampsToReturn.Both`, optional From/To, a value cap) and drives
`session.history_read` for one node. It follows continuation points up to 50 pages, releases them (also in
`finally`), decodes each `DataValue` with the shared `decode_data_value()`, sorts ascending and trims to the cap.
A Good-but-empty result is normal and is reported with the node's `Historizing` attribute. The **History** tab
(`gui/tabs/history_tab.py`) exposes the raw read; `opcua-demo-server --history` historizes Temperature and Speed
in asyncua's in-memory historian. There is **no** `Client.history_read` in asyncua 2.1.0 and no processed,
aggregate, at-time or event-history helper — see `docs/api-verification.md`.

**Server directory (FindServers) — delivered.** `OpcUaService.discover_servers()` sends one `FindServers` request
on a temporary `SecurityPolicyNone` channel (the same channel-level pattern as GetEndpoints, inside
`client_service.py` only) and maps the answer into `DiscoveredServer`/`ServerDiscoveryResult`. The Connection tab
opens `gui/tabs/server_directory_dialog.py` (**Find servers…**), which filters by application-URI prefix, lists
name/URI/type/discovery URL and puts the selected URL into the endpoint field. `FindServersOnNetwork` is
deliberately not offered (asyncua's server answers `BadUserAccessDenied`; no verified multicast path). Demo server
lists itself as `urn:opcua-explorer:demo:server`.

**Node management — delivered.** `opcua/node_management.py` is the only module that sends `AddNodes`/`DeleteNodes`:
`build_add_node_item` (null `RequestedNewNodeId` so the server assigns the id, `Organizes`/`HasComponent` per class,
variable value parsed with `type_support`), `create_node` (read-back of DisplayName) and `delete_node`
(`DeleteNodesItem` with `DeleteTargetReferences`, verified by a `BadNodeIdUnknown` read). The **Write & call** page's
*Node management* group previews, confirms and reports. asyncua's server permits both services for `UserRole.Admin`
only and answers a non-admin with a **ServiceFault**, so the demo server tests the refusal; the positive tests start
their own admin server. Do not set `AddNodesItem.TypeDefinition = None` — it breaks request encoding.

**Address-space browse filters — delivered.** `models.BrowseOptions` plus `browse_children(..., options=...)` carry
reference type, direction, `IncludeSubtypes` and `NodeClassMask` into `BrowseDescription`; the Address space tab's
filter row reloads the current root with **Apply filter** and names the selection in the status line. The default
browse is unchanged (forward hierarchical references with subtypes), a malformed reference-type NodeId is refused
locally, and `browse.to_node_id` now maps asyncua's `UaStringParsingError` to `InvalidNodeIdError`. The test that
clears the tree while a browse is in flight exposed a real crash path: a cleared `QTreeWidget` deletes its items,
so `BrowserTab` carries a `_tree_generation` guard (plus a `RuntimeError` guard) and drops stale results — keep it.

**Value read options — delivered.** `opcua/value_read.py` builds one `Read` request for the Value attribute with
`IndexRange`, `MaxAge` and `TimestampsToReturn` (`ValueReadOptions`/`ValueReadResult` in `models/`,
`DEFAULT_READ_MAX_AGE_MS`/`MAX_READ_MAX_AGE_MS` in `defaults.py`); the Address space *Value* panel exposes the
three controls and shows the request next to the decoded value. A malformed NumericRange is refused locally.
asyncua's demo server ignores `IndexRange` and `TimestampsToReturn`, so tests assert the request/report, never a
slice. Keep the panel on `OpcUaService.read_value_with_options` (no `ReadParameters` in widgets).

**Interactive write & call — delivered.** `gui/tabs/write_tab.py` is the third surface over
`opcua/write_service.py` (after the guarded checks and the sequence runner): `OpcUaService.write_preview` /
`write_node_value` / `method_signature` / `call_preview` / `invoke_method`, all under
`WriteAuthority("operator-dialog", …)`. **Read node** shows NodeClass/DataType/ValueRank/access level and the
current value; a write is previewed (so a refused value never reaches a dialog), confirmed with
`operator_text.write_value_confirmation`, sent once, read back and compared, and optionally restored with one
further verified write (`WriteResult`). A method call reads the server's `InputArguments` into editable rows,
confirms the exact object/method/arguments (`operator_text.method_call_confirmation`), checks `Executable`, sends
once and reports `output_values`. `CallOutcome.status_code` is `Good` on success. The tab is the tenth session
page; `ask_confirmation` is a module function so GUI tests stub the dialog.

**Metadata attribute writes — delivered.** `write_service.prepare_attribute_write` / `send_prepared_attribute_write`
are the only non-Value path and write just `DisplayName` and `Description` (LocalizedText, case-insensitive name,
empty text refused locally). The **Write & call** page exposes them via `OpcUaService.attribute_write_preview` /
`write_attribute_value` with the confirmation from `operator_text.attribute_write_confirmation`; one
`write_attribute`, then a read-back compare. asyncua's server allows non-Value writes for `UserRole.Admin` only,
so the demo server answers `BadUserAccessDenied` (reported, never retried) and the positive integration test starts
its own server with an admin user manager.

**Universal sequences (device control) — delivered.** `PLAN.md` §6 records the plan and all six items are done:
`models/` sequence dataclasses (`Sequence`, `SequenceGate`, `SequenceStep`, `SequenceRun`,
`SequenceStepResult`, step kinds/statuses), `opcua/method_support.py` (shared DataType/ValueRank,
`InputArguments`, `Executable` reads), `opcua/type_support.py` (server-driven typing: built-ins, enumerations by
name or number, structures encoded with their `DefaultEncodingId`; matrices/unions/optional fields refused),
`opcua/write_service.py` (prepare → one write → read-back; one method call with validated arguments; one
`restore_value` for an optional restore) and
`opcua/sequence_runner.py` (preconditions, user-defined gate with `finally` release, keepalive, dry run,
single-flight, `outcome_unknown` on cancelled in-flight writes). `opcua/path_support.py` adds
`TranslateBrowsePathsToNodeIds` addressing (`node_path` / `path_root` on a step, exposed as
`OpcUaService.resolve_browse_paths()`), accepting only a complete, unambiguous match. `guarded_operations.py` now
performs its write, method call **and restore** through `write_service`, so the application has exactly one
write/call implementation. Every sending function requires a keyword-only `WriteAuthority` (no default), and
`tests/unit/test_layer_boundaries.py` fails if a `write_value` / `call_method` call appears outside
`write_service.py` or if `authorization` ever gains a default. Covered by `tests/unit/test_sequence_runner.py`, `tests/unit/test_type_support.py`,
`tests/unit/test_path_support.py`, `tests/integration/test_path_resolution.py`, `tests/unit/test_sequence_document.py`,
`tests/integration/test_gui_sequences.py`, `tests/unit/test_cli_sequence.py`,
`tests/integration/test_cli_sequence.py` and the existing guarded-operation integration tests. The Sequences tab
is wired into every session, the project stores the document path and inputs (project version 2; version 1 still
loads), and `opcua-run-sequence` exposes the same run headlessly. The command is not part of a PyInstaller bundle
yet (`packaging/README.md`).

`compare.py` (Qt/asyncua-free) compares two downloaded address-space snapshots; `cli_compare.py`
(`opcua-compare`) exposes it headlessly and the shared **Compare** tab exposes it in the window. The tab owns
no client and no subscription: it downloads through the two selected sessions' services. Either side may be a
saved JSON export (`exporters/json_loader.py`), so a device that is gone can still be the baseline. Three
bundles are built now (GUI, export, compare).

## Decisions and rationale

- One asyncio loop in a worker thread; `gui/async_runner.py` is the only Qt↔asyncio bridge and widgets never
  `await`. Results reach the GUI as plain dataclasses through queued signals.
- One `asyncua.Client` per session, created only by `opcua/client_service.py`; `gui/session_workspace.py` owns a
  session's state and pages, `gui/main_window.py` stays a passive shell. Cross-tab dependencies are injected by
  the workspace (`selection_provider`), never fetched through `MainWindow` compatibility properties.
- Safety invariants stay intact: `opcua/security.py:select_endpoint()` raises instead of downgrading, only
  operator-approved DER reaches `set_security()`, profiles/projects hold no secrets, logs are redacted, checks
  perform no write unless a target is enabled, and guarded operations are single-target, confirmed and never retried or restored
  automatically.
- Safety-relevant wording lives in `operator_text.py` so it can be tested headless; the widgets only display it.
- `exporters/` must not import `opcua/` (uses `formatting.py`); a subprocess test enforces the boundary.
- `defaults.py` is the single definition of numeric defaults; `ConnectionProfile.to_settings()` is the only
  profile→runtime mapping, and project validation reuses it.
- `python_classes = ["*Suite"]` keeps the domain dataclasses `TestConfig` / `TestCheck` / `TestRun` /
  `TestProgress` from being collected as test classes — they must not be renamed casually.
- Persisted formats are versioned (project `format`/`version`, report `format_version`, CSV columns, NodeSet2
  output); a format change needs a version bump plus regenerated files under `examples/`.

### Module boundaries and entry points

- Entry points: `opcua_explorer.gui.app:main` (`opcua-explorer`), `opcua_explorer.demo.demo_server:main`
  (`opcua-demo-server`), `opcua_explorer.cli:main` (`opcua-export`, Qt-free headless NodeSet2 export),
  `opcua_explorer.cli_compare:main` (`opcua-compare`, Qt-free comparison) and
  `opcua_explorer.cli_sequence:main` (`opcua-run-sequence`, Qt-free sequence runner).
- `cli.py` is an adapter only: it maps arguments to `TraversalConfig` / `NodeSet2Config`, calls
  `OpcUaService.download()` and `export_nodeset2()`, prints `NodeSet2Result.summary_text()` and optionally writes
  `to_dict()` as a manifest. No traversal, dependency, writing, validation or certificate logic may be copied
  into it. Secrets are read from `--password-env` or an interactive prompt, never from argv.
- UI shell: `gui/main_window.py` (sessions, menus, status bar, shared “All monitors”, “Compare” and
  “Logs & reports”). One session = `gui/session_workspace.py` + its ten pages under `gui/tabs/`.
- Async boundary: `gui/async_runner.py` only. OPC UA work: `opcua/client_service.py` is the only owner of an
  `asyncua.Client`.
- Pure layers (no asyncua, no Qt): `models/`, `config.py`, `defaults.py`, `project.py`,
  `formatting.py`, `operator_text.py`, `sequence_document.py`, `compare.py`, `exporters/`, `reporting.py`.
- OPC UA helpers: `opcua/ua_names.py` (names), `opcua/check_support.py` (check primitives),
  `opcua/value_parsing.py` / `opcua/type_support.py` (typed values), `opcua/method_support.py`,
  `opcua/write_service.py` (the one write/call implementation), `opcua/path_support.py`,
  `opcua/guarded_operations.py` (opt-in writes/method calls), `opcua/sequence_runner.py`,
  `opcua/test_runner.py` (functional checks and run orchestration; re-exports `CheckSkipped`,
  `environment_info`, `FunctionalTestRunner`, `run_functional_tests`).
- Integration contract: `OpcUaService` methods and the model dataclasses returned to the GUI. Change these
  signatures only deliberately, together with every caller and the GUI tests.

## Verification

Latest run: 2026-11-16, Python 3.11.2, Linux, PySide6 6.11.3.

| Check | Command | Result |
|---|---|---|
| Environment | `uv sync --reinstall` | ok — also repairs a `.venv/bin/pytest` with a stale shebang (see `AGENTS.md`) |
| Unit tests | `uv run pytest tests/unit -q` | **463 passed** |
| Full suite | `uv run pytest -q` | **626 passed** (≈8 min on an idle host; up to ~15 min on a loaded one) |
| Lint | `uvx ruff check src tests scripts` | **All checks passed** |
| Repository hygiene | `git check-ignore -v` on secret/artefact paths | ignored: `.env`, `.env.local`, `*.pem`, `*.der`, `*.key`, `*.p12`, `*.log`, `*.pcap`, `dist/`, `build/`, `*.egg-info/`, `.mypy_cache/`, `htmlcov/`, `.coverage`, `.cache/`, `.vscode/settings.json`; trackable: `examples/*.json`, `uv.lock`, `.vscode/extensions.json` |
| Debug leftovers | `grep -rn -E "TODO\|FIXME\|XXX\|HACK\|breakpoint\(" src tests scripts` | no matches; `print()` in `src/` is only user-facing output (`cli.py`, `cli_compare.py`, `cli_sequence.py`) and the demo-server banner |
| Broad handlers | `grep -rn "except Exception" src \| wc -l` | 111 — deliberate boundaries, see *Known issues* |

The optional `lxml` extra (`uv sync --extra validation`) was not exercised separately (`lxml` is already
installed through the dev group); `examples/` matches the current formats and is regenerated whenever a format
changes.

## Known issues and review findings

Verified:

- **Full-suite runtime varies with host load**: ≈3.5 min idle, up to ~15 min on a loaded machine. All 626 tests
  pass; the variance is scheduling/IO contention, not failures.
- **111 broad `except Exception` handlers** in `src/` (`client_service.py` 10, `write_service.py` 8,
  `type_support.py` 8, `attributes.py` 8, `test_runner.py` 7, `nodeset2.py` 7, `event_service.py` 7, …).
  Most are deliberate boundaries (best-effort
  teardown, diagnostics for arbitrary server values); do not remove them wholesale without checking the failure
  semantics.
- **`opcua/nodeset2.py` is 771 lines** — the largest source module; its dependency collector is a single
  coherent algorithm, so split further only if a new concern appears.
- **Longest test modules**: `tests/unit/test_sequence_runner.py` (~684 lines) and `tests/unit/test_compare.py`
  (~520 lines), then `test_cli_compare.py` (~344) and `test_client_service.py` (~322); grouped by concern, not
  by accident.
- **A manual two-session run on a real desktop is still worthwhile**: the selection-isolation and monitor
  behaviour are covered headless only.

Not verifiable on this host:

- Windows-only paths (certificate handling, Qt platform integration) — only unit tests cover them.
- Real desktop rendering: GUI tests run with `QT_QPA_PLATFORM=offscreen`.

AI review finding — an observation, **not** an independently confirmed defect:

- `.gitignore` ignores `*.log` and `*.pcap` globally while noting that `examples/` is intentionally tracked. No
  current example uses those extensions, but a future one would need an explicit negation rule.

## Next actions

**No implementation work remains.** Everything below is optional or requires authorization.

Required: none.

Optional, in priority order:

1. **Commit the sequence work** — the comparison/sequence sources, docs and examples are modified or untracked
   on branch `devel_feature_rw_node`. Needs explicit operator authorization; nothing is staged today.
2. **Manual desktop run**: two sessions against `uv run opcua-demo-server`, live monitor plus “All monitors”,
   project save/open, guarded write with the confirmation accepted and declined.
3. **Windows run** of install → connect → export before any release claim.
4. **Regenerate `examples/`** only when an export or report format changes
   (`uv run python scripts/generate_example_exports.py`, `…_report.py`, `…_nodeset.py`).

## How to resume

Read in this order: `AGENTS.md` (working rules) → `PLAN.md` (delivered scope) → `README.md` §6 layout, §9
application tests, §10 limitations → `docs/architecture.md` §2 layers, §3 threading, §4 sessions →
`docs/api-verification.md` before using any new asyncua API.

Prerequisites: `uv` at `~/.local/bin/uv`. If `uv run pytest` fails with `Failed to spawn: pytest`, run
`uv sync --reinstall` (stale console-script shebang, see `AGENTS.md` pitfalls).

First useful action: `uv run pytest tests/unit -q` (~1 s) to confirm the environment; run the full suite when
changing `opcua/` or `gui/`. For any behaviour change, update `README.md`, `PLAN.md`,
`docs/architecture.md`, `AGENTS.md` and this file in the same change.
