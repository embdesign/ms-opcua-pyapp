# AGENTS.md — working rules for `ms-opcua-pyapp`

Scope: `/home/user/wdr/py/ms-opcua-pyapp` (distribution `opcua-explorer`, package `opcua_explorer`).
Read this before changing code. It records only facts verified in this tree.

## Project facts

- Python **3.11–3.13**, managed with **uv** + `pyproject.toml` (hatchling, `src/` layout).
- Runtime dependencies are pinned: `asyncua==2.1.0`, `PySide6==6.11.3`. Optional extra `validation` = `lxml`.
  Dev group: `pytest`, `pytest-asyncio`, `lxml`, `cryptography`.
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
uv run pytest -q                           # full suite, 489 passed (≈4–14 min depending on host load)
uv run pytest tests/unit -q                # fast subset, 369 passed, ~3 s
uvx ruff check src tests scripts           # clean; config lives in pyproject.toml
uv run opcua-explorer                      # launch the GUI
uv run opcua-export --help                 # headless NodeSet2 export (Qt-free)
uv run opcua-compare --help                # headless comparison of two servers (Qt-free, read-only)
uv run opcua-run-sequence --help           # headless run of a sequence document (Qt-free)
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
  `test_gui_workflows`, `test_gui_monitor`, `test_gui_connection_states`, `test_gui_project`,
  `test_gui_compare`, `test_gui_sequences`; `test_pyside_stability` guards the Qt pin). Shared helpers live in
  `tests/integration/gui_support.py`; the module-scoped `qapp` fixture lives in
  `tests/integration/conftest.py` and closes/deletes top-level widgets after each module. Do not remove that
  cleanup.
- `tests/support.py` creates throw-away certificates; `cryptography` is an explicit dev dependency.

## Architecture constraints — do not break

- Layering (enforced by `tests/unit/test_layer_boundaries.py` for exporters):
  `gui/` → `opcua/` → `models.py` / `config.py` / `defaults.py` / `project.py` / `formatting.py` /
  `operator_text.py`.
- `models.py`, `config.py`, `defaults.py`, `project.py`, `formatting.py`, `operator_text.py`,
  `sequence_document.py`, `exporters/`, `reporting.py`, `compare.py` import neither asyncua nor Qt.
  `exporters/` uses `formatting.py`, never `opcua/`.
- Numeric defaults live once in `defaults.py`; do not re-introduce literals in models, project state or tabs.
- Only `opcua/client_service.py` constructs an `asyncua.Client`; one instance per session.
- Only `gui/async_runner.py` bridges asyncio and Qt. Widgets never `await`; all OPC UA work runs on the single
  worker loop and results are plain dataclasses delivered through queued signals.
- `gui/main_window.py` is a shell (session tabs, menus, status bar, shared tabs). A session's state and Qt
  wiring live in `gui/session_workspace.py`. New cross-tab dependencies are injected by the workspace
  (see `selection_provider`), never looked up through `MainWindow` compatibility properties.
- The shared **Compare** tab (`gui/tabs/compare_tab.py`) owns no client and no subscription: it downloads
  through the two selected sessions' `OpcUaService` and calls `compare_address_spaces()`. Keep it read-only and
  do not add a second comparison implementation — GUI and `opcua-compare` share the config, the comparison and
  the report writers.
- Shared value/name conversions live in `opcua/ua_names.py`; do not add another local copy of
  `node_class_name` / `status_name` / `variant_type_name`.
- Functional check primitives live in `opcua/check_support.py`; guarded write/method-call checks live in
  `opcua/guarded_operations.py`. `opcua/test_runner.py` keeps the read-only checks, orchestration and the
  public `FunctionalTestRunner` / `run_functional_tests` / `CheckSkipped` / `environment_info` names.
- There is exactly **one** write/method-call/restore implementation: `opcua/write_service.py`
  (`prepare_write` → `send_prepared_write`, `prepare_call` → `send_prepared_call`, `restore_value`). The
  guarded checks and the sequence runner both use it; do not add another path that parses and sends a value.
  Every sending function requires a keyword-only `authorization: WriteAuthority` (no default), so a write can
  never be sent without naming the operator dialog or the sequence document that asked for it.
  `tests/unit/test_layer_boundaries.py` enforces both rules: no `write_value` / `call_method` outside
  `write_service.py` (the demo server excepted), and no default for `authorization`.
- Condition acknowledgement is the **only** action of the Events tab: one `Acknowledge` method call through
  `write_service.acknowledge_condition` with `WriteAuthority("operator-dialog", …)`, after the explicit
  confirmation text in `operator_text.acknowledge_condition_confirmation`. It is never retried, never calls
  `Confirm`/`Enable`/`AddComment`, and refuses a method whose `InputArguments` are not
  (ByteString, LocalizedText). Event notifications stay read-only; there is no automatic acknowledgement.
- Writes are typed from the server by `opcua/type_support.py` (built-ins, enumerations, structures) and targets
  may be addressed by browse path through `opcua/path_support.py` (complete, unambiguous matches only).
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
  now **version 2** (a session stores the sequence document path and inputs); version 1 files must keep loading.
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
- `cli.py` (`opcua-export`), `cli_compare.py` (`opcua-compare`) and `cli_sequence.py` (`opcua-run-sequence`)
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
