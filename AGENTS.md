# AGENTS.md — working rules for `ms-opcua-pyapp`

Scope: `/home/user/wdr/py/ms-opcua-pyapp` (distribution `opcua-explorer`, package `opcua_explorer`).
Read this before changing code. It records only facts verified in this tree.

## Project facts

- Python **3.11–3.13**, managed with **uv** + `pyproject.toml` (hatchling, `src/` layout).
- Runtime dependencies are pinned: `asyncua==2.1.0`, `PySide6==6.12.0`. Optional extra `validation` = `lxml`.
  Dev group: `pytest`, `pytest-asyncio`, `lxml`, `cryptography`.
- Desktop OPC UA client: discovery/security/identity, lazy browser, bounded address-space download,
  JSON/CSV/NodeSet2 export, headless NodeSet2 export (`opcua-export`), live monitoring, read-only functional
  checks, guarded single-target write/method call, JSON/HTML reports, demo server. One window may host several
  independent sessions.
- Validation document: `docs/api-verification.md`. Never invent an asyncua API or behaviour; verify it there
  or against the installed package first.

## Commands (verified)

```bash
uv sync                                   # create .venv + dev group (use --reinstall after moving the folder)
uv run pytest -q                           # full suite, 260 passed (≈3.5–14 min depending on host load)
uv run pytest tests/unit -q                # fast subset, 182 passed, ~1 s
uvx ruff check src tests scripts           # clean; config lives in pyproject.toml
uv run opcua-explorer                      # launch the GUI
uv run opcua-export --help                 # headless NodeSet2 export (Qt-free)
uv run opcua-demo-server                   # demo server on a free/selected port
uv run python scripts/generate_example_exports.py
uv run python scripts/generate_example_report.py
uv run python scripts/generate_example_nodeset.py
```

- Integration tests start in-process demo servers on free loopback ports; they need no Docker and no sudo.
- GUI tests run headless (`QT_QPA_PLATFORM=offscreen`) and are split by concern (`test_gui_smoke`,
  `_workflows`, `_monitor`, `_connection_states`, `_project`). Shared helpers live in
  `tests/integration/gui_support.py`; the module-scoped `qapp` fixture lives in
  `tests/integration/conftest.py` and closes/deletes top-level widgets after each module. Do not remove that
  cleanup.
- `tests/support.py` creates throw-away certificates; `cryptography` is an explicit dev dependency.

## Architecture constraints — do not break

- Layering (enforced by `tests/unit/test_layer_boundaries.py` for exporters):
  `gui/` → `opcua/` → `models.py` / `config.py` / `defaults.py` / `project.py` / `formatting.py` /
  `operator_text.py`.
- `models.py`, `config.py`, `defaults.py`, `project.py`, `formatting.py`, `operator_text.py`, `exporters/`,
  `reporting.py` import neither asyncua nor Qt. `exporters/` uses `formatting.py`, never `opcua/`.
- Numeric defaults live once in `defaults.py`; do not re-introduce literals in models, project state or tabs.
- Only `opcua/client_service.py` constructs an `asyncua.Client`; one instance per session.
- Only `gui/async_runner.py` bridges asyncio and Qt. Widgets never `await`; all OPC UA work runs on the single
  worker loop and results are plain dataclasses delivered through queued signals.
- `gui/main_window.py` is a shell (session tabs, menus, status bar, shared tabs). A session's state and Qt
  wiring live in `gui/session_workspace.py`. New cross-tab dependencies are injected by the workspace
  (see `selection_provider`), never looked up through `MainWindow` compatibility properties.
- Shared value/name conversions live in `opcua/ua_names.py`; do not add another local copy of
  `node_class_name` / `status_name` / `variant_type_name`.
- Functional check primitives live in `opcua/check_support.py`; guarded write/method-call checks live in
  `opcua/guarded_operations.py`. `opcua/test_runner.py` keeps the read-only checks, orchestration and the
  public `FunctionalTestRunner` / `run_functional_tests` / `CheckSkipped` / `environment_info` names.
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
  CSV columns, NodeSet2 output.
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
- Do not log passwords, private-key passwords, certificate bytes or trust decisions.
- `opcua/nodeset2.py` (dependency collection + export) and `opcua/nodeset2_validation.py` are still sizeable;
  target their helpers directly instead of growing the modules further.
- `cli.py` (`opcua-export`) is a second front end, not a second implementation: it must keep calling
  `OpcUaService.download()` / `export_nodeset2()` and print `NodeSet2Result.summary_text()`. Do not copy
  traversal, dependency closure, writing, validation or certificate-approval rules into it. Passwords are only
  accepted through `--password-env` or an interactive prompt, never as an argument, and a secure connection
  still requires an explicit certificate decision (trust store, `--trust-server-certificate` or
  `--server-certificate` with a matching fingerprint).
- The GUI test modules share mutable process state (one QApplication, demo-server subprocesses). Keep the
  `qapp` teardown and always stop a demo server in `finally` (use `gui_support.stop_demo_server`).
