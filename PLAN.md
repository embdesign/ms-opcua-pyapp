# PLAN — OPC UA Explorer

Status: **complete and maintained** (verified 2026-11-14). The scope described below — including the
features added after the original brief — is implemented and covered by tests. There is no open
implementation work.

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
| Sessions & projects | Several independent sessions per window, shared trust store and asyncio loop; versioned project files with per-session configuration and editor state, never secrets |
| Demo & examples | Local demo server with a graph-shaped model; example JSON/CSV/NodeSet2 exports and sample reports |

## 2. Module map

```
src/opcua_explorer/
├── gui/            window shell, one workspace per session, tabs, Qt↔asyncio bridge
├── cli.py          opcua-export: headless NodeSet2 export (same services, no Qt)
├── opcua/          client service, security, browse, attributes, traversal, serialize,
│                   monitor, NodeSet2 (+ validation), check support, test runner,
│                   guarded operations, shared UA names
├── exporters/      JSON and CSV writers, JSON snapshot loader
├── models.py       plain dataclasses shared by every layer
├── config.py       settings, profiles, trust store
├── defaults.py     single definition of numeric defaults
├── project.py      versioned project documents
├── formatting.py   exporter formatting (Qt/asyncua-free)
├── operator_text.py safety confirmation and diagnostics text (Qt-free)
├── reporting.py    JSON/HTML reports
├── compare.py      structural comparison of two address-space snapshots (Qt/asyncua-free)
├── cli_compare.py  opcua-compare: headless comparison (same services, no Qt)
├── logging_setup.py log bus and secret redaction
└── demo/           local demo server
```

Layer rules and the threading model are in `docs/architecture.md`. Working rules are in `AGENTS.md`.
Recent changes and verification history are in `HANDOFF.md`.

## 3. Verification

```bash
uv sync
uv run pytest -q                  # 325 passed, ~4:20
uv run pytest tests/unit -q       # 234 passed, ~2 s
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
- Only namespace-0 built-in DataTypes can be written; custom structures/enumerations are refused.
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
