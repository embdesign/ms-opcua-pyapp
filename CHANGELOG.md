# Changelog

All notable changes to **OPC UA Explorer** (`opcua-explorer`) are documented in
this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- A project file is a configuration snapshot, not a session restore.
- Not included by policy: unrestricted stress testing, automatic
  production-network scanning, automatic writes or method invocation, automatic
  value restoration, and any OPC UA certification claim.
