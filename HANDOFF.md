# Task Handoff — OPC UA Explorer (`ms-opcua-pyapp`)

Project repository `ms-opcua-pyapp`, distribution `opcua-explorer`, application version 0.1.0, package
`opcua_explorer`. This file records the current state, the rules a future change must respect, and what is still
unverified. It is not a change log. Do not commit without the operator's authorization; make out-of-tree
backups before bulk edits.

## Goal and acceptance criteria

`PROMPT.md` asks for a maintainable desktop OPC UA client on `asyncua` + PySide6, managed with `uv`, that browses
a server, downloads its address space, exports it (JSON/CSV/NodeSet2) and runs functional interoperability
checks — with verified asyncua APIs, pinned dependencies, native Windows/Linux support without Docker, a demo
server, unit/integration tests, example exports, sample reports and troubleshooting docs. Safety rules: never
downgrade security silently, explicit certificate approval, no credentials in profiles or logs, read-only by
default, guarded writes/method calls only after explicit confirmation.

Completion criteria:

1. Every capability claimed in `README.md` / `PLAN.md` exists in `src/opcua_explorer/` and is covered by a test.
2. `uv run pytest -q` and `uvx ruff check src tests scripts` are green.
3. Persisted formats (project file, report JSON, CSV columns, NodeSet2) stay versioned and change only with a
   deliberate version bump.
4. `README.md`, `PLAN.md`, `docs/architecture.md` and `AGENTS.md` agree with the code.

## Current status

**Complete.** No open implementation work, nothing blocked, no authorization pending.

325 tests pass (234 unit), ruff reports no findings, and the tree contains no debug leftovers. Remaining gaps
are verification-only (Windows, a manual desktop session, example regeneration) and are listed under *Known
issues and review findings* and *Next actions*.

`compare.py` (Qt/asyncua-free) compares two downloaded address-space snapshots; `cli_compare.py`
(`opcua-compare`) exposes it headlessly and the shared **Compare** tab exposes it in the window. The tab owns
no client and no subscription: it downloads through the two selected sessions' services. Either side may be a
saved JSON export (`exporters/json_loader.py`), so a device that is gone can still be the baseline. Three
bundles are built now (GUI, export, compare).

## Implemented changes

### Delivered capabilities

- **Connection & security** — endpoint discovery, policy/mode selection that never downgrades, explicit server
  certificate approval and trust store, timeouts, named profiles without secrets, automatic reconnect.
- **Address space** — lazy tree from Root/Objects or any NodeId, attribute and reference panels, explicit value
  read, copy NodeId, search in a downloaded snapshot, session-local live-monitor markers.
- **Download & export** — bounded graph traversal (namespace filter, depth/node budget, concurrency, batch
  size, continuation points, cancellation) to JSON, CSV and NodeSet2 XML with dependency closure and
  validation; the same NodeSet2 pipeline is available headlessly as `opcua-export`.
- **Headless export** — `opcua-export` drives the same download/NodeSet2 pipeline without Qt, with explicit
  certificate approval, JSON manifest output and exit codes suitable for scripting.
- **Live monitoring** — one data-change subscription per session plus a read-only “All monitors” view.
- **Functional tests** — eleven read-only checks, JSON/HTML reports, opt-in guarded single-target write and
  method call behind an explicit confirmation.
- **Window, projects, demo** — several independent sessions, versioned project files, demo server, example
  exports and sample reports.

## Decisions and rationale

- One asyncio loop in a worker thread; `gui/async_runner.py` is the only Qt↔asyncio bridge and widgets never
  `await`. Results reach the GUI as plain dataclasses through queued signals.
- One `asyncua.Client` per session, created only by `opcua/client_service.py`; `gui/session_workspace.py` owns a
  session's state and pages, `gui/main_window.py` stays a passive shell. Cross-tab dependencies are injected by
  the workspace (`selection_provider`), never fetched through `MainWindow` compatibility properties.
- Safety invariants stay intact: `opcua/security.py:select_endpoint()` raises instead of downgrading, only
  operator-approved DER reaches `set_security()`, profiles/projects hold no secrets, logs are redacted, checks
  are read-only by default, and guarded operations are single-target, confirmed and never retried or restored
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
  (`opcua-demo-server`), `opcua_explorer.cli:main` (`opcua-export`, Qt-free headless NodeSet2 export).
- `cli.py` is an adapter only: it maps arguments to `TraversalConfig` / `NodeSet2Config`, calls
  `OpcUaService.download()` and `export_nodeset2()`, prints `NodeSet2Result.summary_text()` and optionally writes
  `to_dict()` as a manifest. No traversal, dependency, writing, validation or certificate logic may be copied
  into it. Secrets are read from `--password-env` or an interactive prompt, never from argv.
- UI shell: `gui/main_window.py` (sessions, menus, status bar, shared “All monitors” and “Logs & reports”).
  One session = `gui/session_workspace.py` + its five pages under `gui/tabs/`.
- Async boundary: `gui/async_runner.py` only. OPC UA work: `opcua/client_service.py` is the only owner of an
  `asyncua.Client`.
- Pure layers (no asyncua, no Qt): `models.py`, `config.py`, `defaults.py`, `project.py`,
  `formatting.py`, `operator_text.py`, `exporters/`, `reporting.py`.
- OPC UA helpers: `opcua/ua_names.py` (names), `opcua/check_support.py` (check primitives),
  `opcua/guarded_operations.py` (opt-in writes/method calls), `opcua/test_runner.py` (read-only checks and run
  orchestration; re-exports `CheckSkipped`, `environment_info`, `FunctionalTestRunner`,
  `run_functional_tests`).
- Integration contract: `OpcUaService` methods and the model dataclasses returned to the GUI. Change these
  signatures only deliberately, together with every caller and the GUI tests.

## Verification

Latest run: 2026-11-14, Python 3.11, Linux.

| Check | Command | Result |
|---|---|---|
| Environment | `uv sync --reinstall` | ok — also repairs a `.venv/bin/pytest` with a stale shebang (see `AGENTS.md`) |
| Unit tests | `uv run pytest tests/unit -q` | **234 passed** |
| Full suite | `uv run pytest -q` | **325 passed** (≈3.5 min idle host, up to ~14 min on a loaded one) |
| Lint | `uvx ruff check src tests scripts` | **All checks passed** |
| Repository hygiene | `git check-ignore -v` on secret/artefact paths | ignored: `.env`, `.env.local`, `*.pem`, `*.der`, `*.key`, `*.p12`, `*.log`, `*.pcap`, `dist/`, `build/`, `*.egg-info/`, `.mypy_cache/`, `htmlcov/`, `.coverage`, `.cache/`, `.vscode/settings.json`; trackable: `examples/*.json`, `uv.lock`, `.vscode/extensions.json` |
| Debug leftovers | `grep -rn -E "TODO\|FIXME\|XXX\|HACK\|breakpoint\(" src tests scripts` | no matches; `print()` in `src/` is only user-facing output (`cli.py`) and the demo-server banner |
| Broad handlers | `grep -rn "except Exception" src \| wc -l` | 78 — deliberate boundaries, see *Known issues* |

Not verified here: native Windows paths, a manual desktop (non-headless) session, and `uv sync --extra
validation` with the optional `lxml` extra (`lxml` is already installed through the dev group). `examples/`
matches the current formats; regenerate it whenever a format changes.

## Known issues and review findings

Verified:

- **Full-suite runtime varies with host load**: ≈3.5 min idle, up to ~14 min on a loaded machine. All 325 tests
  pass; the variance is scheduling/IO contention, not failures.
- **78 broad `except Exception` handlers** in `src/` (`guarded_operations.py` 11, `client_service.py` 10,
  `test_runner.py` 7, `nodeset2.py` 7, `attributes.py` 6, `cli.py` 1, …). Most are deliberate boundaries (best-effort
  teardown, diagnostics for arbitrary server values); do not remove them wholesale without checking the failure
  semantics.
- **`opcua/nodeset2.py` is 771 lines** — the largest source module; its dependency collector is a single
  coherent algorithm, so split further only if a new concern appears.
- **Longest test modules**: `tests/integration/test_gui_workflows.py` (~258 lines) and
  `test_gui_connection_states.py` (~229 lines); grouped by concern, not by accident.
- **A manual two-session run on a real desktop is still worthwhile**: the selection-isolation and monitor
  behaviour are covered headless only.

Not verifiable on this host:

- Windows-only paths (certificate handling, Qt platform integration) — only unit tests cover them.
- Real desktop rendering: GUI tests run with `QT_QPA_PLATFORM=offscreen`.
- The optional NodeSet2 XSD validation path installed via `uv sync --extra validation` was not exercised
  separately (`lxml` is already present through the dev group).

AI review findings — observations, **not** independently confirmed defects:

- ~~`docs/architecture.md` opens with “phases 1–4 implemented and tested” while `README.md` and `PLAN.md` say
  “complete and maintained”.~~ **Resolved:** all three now say “complete and maintained”.
- `.gitignore` ignores `*.log` and `*.pcap` globally while noting that `examples/` is intentionally tracked. No
  current example uses those extensions, but a future one would need an explicit negation rule.

## Next actions

**No implementation work remains.** Everything below is optional or requires authorization.

Required: none.

Optional, in priority order:

1. **Commit the `opcua-export` changes** (modified docs/pyproject plus untracked `cli.py` and CLI tests). Needs
   explicit operator authorization; nothing is staged today.
2. **Manual desktop run**: two sessions against `uv run opcua-demo-server`, live monitor plus “All monitors”,
   project save/open, guarded write with the confirmation accepted and declined.
3. **Windows run** of install → connect → export before any release claim.
4. **Regenerate `examples/`** only when an export or report format changes
   (`uv run python scripts/generate_example_exports.py`, `…_report.py`, `…_nodeset.py`).
5. Only if a new concern appears: split `opcua/nodeset2.py` further, or review broad `except Exception` handlers
   one at a time.

## How to resume

Read in this order: `AGENTS.md` (working rules) → `PLAN.md` (delivered scope) → `README.md` §6 layout, §9
application tests, §10 limitations → `docs/architecture.md` §2 layers, §3 threading, §4 sessions →
`docs/api-verification.md` before using any new asyncua API.

Prerequisites: `uv` at `~/.local/bin/uv`. If `uv run pytest` fails with `Failed to spawn: pytest`, run
`uv sync --reinstall` (stale console-script shebang, see `AGENTS.md` pitfalls).

First useful action: `uv run pytest tests/unit -q` (~1 s) to confirm the environment; run the full suite when
changing `opcua/` or `gui/`. For any behaviour change, update `README.md`, `PLAN.md`,
`docs/architecture.md`, `AGENTS.md` and this file in the same change.
