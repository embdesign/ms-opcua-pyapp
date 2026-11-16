# PLAN — OPC UA Explorer

Status: **complete and maintained** (verified 2026-11-16). The scope described in §1 is implemented and
covered by tests, including the device-independent sequence runner for operator-written control workflows
(§6, delivered in this change with engine, GUI, persistence, CLI and examples).

## 1. Delivered scope

| Area | Delivered |
|---|---|
| Connection & security | Endpoint discovery, policy/mode selection without downgrade, anonymous/username/certificate identity, client certificate & key, explicit server-certificate approval, trust-store management, timeouts, connection profiles (no secrets) |
| Resilience | Automatic reconnect with exponential backoff (default on), explicit release on link loss when disabled, clear GUI states and diagnostics |
| Address space | Lazy tree from Root/Objects or any NodeId, attributes and references panel, explicit value read, copy NodeId, search in a downloaded snapshot, session-local live-monitor markers |
| Download & export | Bounded graph traversal with namespace filter, depth/node budget, concurrency/batch size, continuation-point handling, progress/cancel; JSON, CSV and NodeSet2 XML |
| NodeSet2 | Dependency closure (parents, supertypes, type definitions, encodings, type members, method argument properties), missing-dependency and unsupported-DataType reports, optional XSD validation |
| Headless export | `opcua-export` command (Qt-free): same download/NodeSet2 pipeline as the GUI, endpoint/scope/namespace/security options, JSON manifest, explicit certificate approval, exit codes for scripting |
| Live monitoring | One data-change subscription per session, in-place latest values, bounded notification log, pause/clear, add/remove while running, configurable intervals |
| Functional tests | 11 checks (discovery, session, namespaces, browse root/target, attributes, value read, subscription creation/notifications, latency, opt-in connection recovery), read-only by default |
| Guarded operations | Single-target write and method call, DataType/ValueRank and `InputArguments` validation, explicit confirmation, optional restore, read-back verification |
| Reports | Versioned JSON and self-contained HTML reports with security configuration, environment, StatusCodes, durations, coverage limitations |
| Model comparison | `compare.py` (Qt/asyncua-free), the shared **Compare** tab and the `opcua-compare` command: structural comparison of two address spaces — namespace-URI identity, browse-path fallback for renumbered NodeIds, functional/strict tolerance, value comparison for named nodes, ignore list, warnings for partial snapshots, versioned JSON/HTML reports, exit code 3 as a CI gate. Either side may be a saved JSON export (`exporters/json_loader.py`), so a retired device stays comparable |
| Sessions & projects | Several independent sessions per window, shared trust store and asyncio loop; versioned project files (version 2) with per-session configuration and editor state, never secrets |
| Sequences | Operator-written sequence documents (`opcua-explorer-sequence` v1): preconditions, user-defined gate (acquire / verify / keepalive / release) and body (`write`, `expect`, `assert`, `ensure`, `call`, `delay`, `wait_until`), `${name}` inputs, dry run, browse-path addressing, run timeline, and the Qt-free `opcua-run-sequence` command |
| Demo & examples | Local demo server with a graph-shaped model; example JSON/CSV/NodeSet2 exports and sample reports |

## 2. Module map

```
src/opcua_explorer/
├── gui/            window shell, one workspace per session, tabs, Qt↔asyncio bridge
├── cli.py          opcua-export: headless NodeSet2 export (same services, no Qt)
├── opcua/          client service, security, browse, attributes, traversal, serialize,
│                   monitor, NodeSet2 (+ validation), check support, test runner,
│                   guarded operations, write service, type support, path support,
│                   sequence runner, shared UA names
├── exporters/      JSON and CSV writers, JSON snapshot loader
├── models.py       plain dataclasses shared by every layer
├── config.py       settings, profiles, trust store
├── defaults.py     single definition of numeric defaults
├── project.py      versioned project documents
├── formatting.py   exporter formatting (Qt/asyncua-free)
├── operator_text.py safety confirmation and diagnostics text (Qt-free)
├── reporting.py    JSON/HTML reports
├── compare.py      structural comparison of two address-space snapshots (Qt/asyncua-free)
├── sequence_document.py  read/validate *.opcua-sequence.json (Qt/asyncua-free)
├── cli_compare.py  opcua-compare: headless comparison (same services, no Qt)
├── cli_sequence.py opcua-run-sequence: headless run of a sequence document (Qt-free)
├── logging_setup.py log bus and secret redaction
└── demo/           local demo server
```

Layer rules and the threading model are in `docs/architecture.md`. Working rules are in `AGENTS.md`.
Recent changes and verification history are in `HANDOFF.md`.

## 3. Verification

```bash
uv sync
uv run pytest -q                  # 416 passed, ~5:30
uv run pytest tests/unit -q       # 315 passed, ~3 s
uvx ruff check src tests scripts  # clean
uv run opcua-explorer             # launch the GUI
uv run opcua-export --help        # headless NodeSet2 export
```

Integration tests start in-process demo servers on free loopback ports; GUI tests run headless
(`QT_QPA_PLATFORM=offscreen`). Windows paths are not executable on this Linux host.

## 4. Deliberate limitations

- Values are point-in-time; there are no history reads.
- The functional suite covers one session, one browse root and one variable.
- Writes and method calls are single-target, opt-in and never repeated; no batch write, restore-all,
  scan or stress mode.
- Sequences are declarative documents, not scripts: no embedded language, one run per session at a time, a
  keepalive failure aborts between steps, and matrices/unions/optional-field structures are refused.
- Writes are typed from the server: built-in DataTypes, enumerations (by name or number) and structures with a
  readable `DataTypeDefinition`. Matrices, unions, optional-field structures and types without a definition are
  refused rather than encoded on a guess.
- Downloads and NodeSet2 exports contain what the session was allowed to browse; access-denied nodes
  and missing dependencies are reported, not hidden.
- NodeSet2 is an export of an existing session, not a model-authoring or merging tool.
- Sessions share one window and one asyncio loop; the shared **Compare** tab and `opcua-compare` compare two
  of them, but nothing is merged automatically, and the comparison is structural evidence for one identity and
  one point in time rather than an equivalence guarantee.

## 5. Deferred maintenance

- `opcua/nodeset2.py` (771 lines) remains the largest source module; its collector is one coherent
  algorithm, so split further only if a new concern appears.
- Broad `except Exception` handlers are deliberate boundaries (teardown, diagnostics for arbitrary
  server values); review individually rather than removing wholesale.
- Desktop behaviour has only been verified headless; a manual two-session run and a Windows run remain
  useful before a release.

## 6. Delivered: universal sequences (device control)

Goal: let an operator control a device that needs *ordered* operations (e.g. acquire a lock, change a
setpoint, read it back, release the lock) **without the application knowing any device, vendor or lock
protocol**. Every device-specific action is a step in a sequence document written by the operator; the
code enforces only mechanical rules.

### 6.1 Universal document

`opcua-explorer-sequence`, version 1 (`SEQUENCE_FORMAT` / `SEQUENCE_VERSION` in `models.py`):

| Part | Content |
|---|---|
| `inputs` | Parameters substituted into step texts as `${name}`; values come from the document, the project or a CLI argument |
| `preconditions` | Steps that run before the gate; usually read-only checks such as `assert` / `wait_until`, plus an optional `ensure` that may write a prerequisite |
| `gate` | Operator-defined `on` steps, optional `verify` step, optional `keepalive` step with interval and failure policy, and `off` steps |
| `steps` | The body: `write`, `expect`, `assert`, `ensure`, `call`, `delay`, `wait_until` |

Step kinds are fixed (`SEQUENCE_STEP_KINDS`); anything else is refused before the first request. A
`gate` is *not* a built-in lock: on one device it holds `call InitLock` / `call ExitLock`, on another
`write Lock=true`, and on a third it is absent. `keepalive` is a generic "repeat this step every N s"
(renewal, heartbeat, watchdog kick).

### 6.2 Mechanical guarantees (the only rules in code)

- The gate's `off` steps run in `finally`: after success, after a failed step, after a cancellation and
  after a keepalive-triggered abort. A refused/failed release is reported, never hidden.
- A gate `verify` step gates the body, so a refused acquisition aborts before the first write.
- Keeping a lease is the author's policy: `keepalive_on_failure` is `abort` (default) or `warn`.
- One run per runner at a time (`SequenceBusyError`); endpoint-wide exclusivity stays with the caller.
- No step is retried. A cancellation while a write or method call is in flight is reported as
  `outcome_unknown`, never as "failed".
- `dry_run` resolves every NodeId, DataType, ValueRank, `InputArguments` and `Executable` and sends
  nothing.

### 6.3 Work breakdown

1. **Engine (done)** — `models.py` sequence dataclasses and statuses;
   `opcua/method_support.py` (server DataType/ValueRank, `InputArguments`, `Executable` read once);
   `opcua/write_service.py` (`prepare_write` → one write → read-back, one method call with validated
   arguments); `opcua/sequence_runner.py`; unit tests with a fake client.
2. **Guarded checks delegate (done)** — `check_write_target` / `check_method_call` call `write_service`
   (`prepare_*` + `send_prepared_*`), so only one write/call implementation exists; the check-specific
   reporting, StatusCode entries and messages are unchanged.
3. **Value coverage (done)** — `value_parsing.py` keeps the built-in scalars; `opcua/type_support.py` adds
   enumerations (definition or `EnumStrings`/`EnumValues`, by name or number), arrays of enums, and structures
   with a `DataTypeDefinition` (nested structures, rank-1 arrays, encoded with `DefaultEncodingId`). Matrices,
   unions, optional-field structures and definitions that cannot be read are refused explicitly.
4. **Path addressing (done)** — `opcua/path_support.py` (`PathRequest` / `PathResolution`,
   `resolve_paths`, `parse_browse_name` for `Name` / `index:Name`) on top of
   `TranslateBrowsePathsToNodeIds`; `OpcUaService.resolve_browse_paths()` exposes it, sequence steps accept
   `node_path` + `path_root`, and only complete, unambiguous matches are accepted.
5. **GUI (done)** — `gui/tabs/sequences_tab.py`: document loader, `${name}` inputs, step preview, Run / Dry run /
   Stop, live run timeline, gate state and an operator-triggered **Release gate now**. Wired in
   `session_workspace.py`; **no armed state and no per-write dialog** — a write happens because a step asks for
   it and is read back. The project records the document path and inputs (project version 2, version 1 still
   loads).
6. **Persistence, CLI, docs (done)** — `sequence_document.py` (shared Qt-free parser), `cli_sequence.py`
   (`opcua-run-sequence`, exit codes 0/1/2, `--dry-run`, `--input`, `--json`), example documents, and updates to
   `README.md`, §1/§4 of this file, `docs/architecture.md`, `docs/api-verification.md`, `AGENTS.md`, `HANDOFF.md`.
   The command is not part of a PyInstaller bundle yet (`packaging/README.md`).
7. **Runnable demo suite (done)** — `examples/README.md` indexes six documents, each verified against
   `uv run opcua-demo-server --no-updates` on 2026-11-15: `demo-01-write-readback` (Double/String/array with
   read-back and tolerance), `demo-02-method-calls` (method NodeId, browse name, and both through a browse
   path), `demo-03-wait-and-optional` (`assert` precondition, `optional` `ensure`, two `wait_until` polls),
   `demo-04-gate-from-writes` (gate built from plain writes, `keepalive` with `on_failure: warn`),
   `demo-05-failure-handling` (`abort_on_failure: false`, exits 1 by design) and `demo-06-value-types`
   (String, DateTime, ByteString, array). Exit codes are 0 for all but demo-05.
   Verification found two real defects that are now fixed: comparisons bypassed the server-driven type
   resolution (an enum precondition failed with "DataType 852 is not supported"), and `ValueRank 0` was
   treated as a scalar although OPC UA defines it as "one or more dimensions" (asyncua-based servers report 0
   for array variables, so array writes were refused). The demo server's variables used by the demos are
   writable and `--no-updates` keeps their values static.

### 6.4 Deliberate limitations

- Sequences are declarative; there is no embedded scripting language and no arbitrary code execution.
- A keepalive failure aborts between steps, not inside a running `delay`.
- Values remain point-in-time; there is still no history read, no batch write and no automatic retry.
- Whether a device honours a gate is device behaviour; the tool reports StatusCodes and read-backs, it
  cannot prove mutual exclusion.
