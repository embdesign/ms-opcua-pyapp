# OPC UA Explorer

Desktop application for **browsing an OPC UA server, downloading its address space, and exporting it** — with
deliberate, explicit security handling.

Python 3.11+ · [asyncua 2.1.0](https://pypi.org/project/asyncua/) · PySide6 6.12 · managed with `uv` · native
Windows and Linux, no Docker.

> **Status — complete and maintained (2026-11-14)**
> Implemented and tested: connection & security with automatic reconnect, multi-session window, address-space
> browser and bounded download, JSON/CSV/NodeSet2 export with validation, headless NodeSet2 export
> (`opcua-export`), live data-change monitoring with a merged view, functional tests (read-only by default)
> with JSON/HTML reports, guarded single-target write/method-call checks with explicit confirmation,
> connection profiles and versioned project files, and a headless comparison of two address spaces
> (`opcua-compare`, also available as a Compare tab and usable with saved JSON exports). The automated suite
> has 325 tests. See
> [Limitations](#10-limitations) for what is deliberately *not* claimed, [PLAN.md](PLAN.md) for the
> delivered scope and [HANDOFF.md](HANDOFF.md) for maintenance guidance.

![OPC UA Explorer main window with a session tab](docs/pics/opcua_explorer.png)

---

## 1. What you can do with it

Each session lives in its own tab (**File → New connection**, `Ctrl+N`) with its own OPC UA client, so you can
work with several devices — or the same server twice — at the same time. A session tab contains the first five
pages below. Three tabs are shared: **All monitors** (a read-only merged view of every session's monitored items
and notification log, with a session filter), **Compare** (a read-only structural comparison of two sessions) and
**Logs & reports** (operator summaries prefixed with the session name). Closing a session tab releases only that
session's client.

| Page | Capability |
|---|---|
| **Connection** | Enter an endpoint URL, discover the server's advertised endpoints, choose policy + message security mode, authenticate anonymously / username-password / certificate, configure client certificate & key, configure timeouts, keep reconnecting when the link drops (on by default), connect/disconnect, read diagnostics |
| **Address space** | Lazy tree from Root/Objects or any NodeId, attributes panel, references panel with direction, explicit value read, copy NodeId, search inside a downloaded snapshot; rows of nodes in this session's live monitor are marked with **●** while the subscription runs and **○** when added but not subscribed (bold, session-local) |
| **Export** | Download whole address space or a subtree with namespace filter, depth/node budget, request concurrency and batch size, progress and cancellation; write JSON, CSV or NodeSet2 XML (with dependency closure, missing-dependency report and optional XSD validation) |
| **Live monitor** | One data-change subscription on NodeIds you add explicitly (also via **Monitor node** in the Address space tab); per-node table with the **latest value updated in place** (value, StatusCode, source timestamp, notification count) plus an append-only notification log; pause/clear, add/remove monitored items while running, configurable publishing/sampling intervals and bounded row retention |
| **Tests** | Functional checks (read-only by default: discovery, session/identity, namespaces, browsing, attributes, value reads with StatusCodes, subscription creation and monitored-item notifications, bounded latency, opt-in connection recovery); optional **guarded** single-target write and method-call checks behind an explicit confirmation; JSON and HTML reports |
| **Logs & reports** | Live log with secret redaction, save/copy, operational summaries and written report paths |
| **Compare** | Read-only structural comparison of two sessions, or of a session and a saved JSON export: pick reference and candidate, download the live side(s) with one scope, then review differences (missing / extra / changed / not comparable) with a filter and a per-node attribute detail; export the JSON or HTML report |

**File → Save project… / Open project…** stores and restores every session's configuration and editor state in
one versioned JSON file — never credentials.

Everything the **Export** page writes can also be produced without the GUI: `uv run opcua-export …` exports a
NodeSet2 XML file headlessly (see [Headless export](#headless-export-the-opcua-export-command)).
Two devices can be compared headlessly with `uv run opcua-compare …` (see
[Headless comparison](#headless-comparison-the-opcua-compare-command)).

## 2. Requirements

* Python **3.11 – 3.13**
* Windows 10/11 or a Linux desktop (X11 or Wayland) with the usual Qt runtime libraries
* No Docker, no server installation required — a demo server is included

## 3. Install

The project is managed with [`uv`](https://docs.astral.sh/uv/). Install it once:

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then, from the project root:

```bash
uv sync                 # creates .venv and installs pinned dependencies
uv run opcua-explorer   # start the desktop application
```

Windows `cmd.exe` / PowerShell use the same commands. If you prefer plain `pip`:

```bash
python -m venv .venv
# Linux
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -e .
opcua-explorer
```

Pinned runtime dependencies (`pyproject.toml`): `asyncua==2.1.0`, `PySide6==6.12.0`; `uv.lock` pins the full
resolved set. The asyncua API used by this project was verified against the installed package — see
[docs/api-verification.md](docs/api-verification.md).

### Prebuilt bundles (no Python required)

GitHub Actions builds a Windows and a Linux bundle on every push to `main`; a `v*` tag attaches them to
the GitHub Release. Unpack an archive and run the executable inside — **Python and `uv` are not needed
on the target machine**:

| Archive | Contents |
|---|---|
| `opcua-explorer-windows-x64.zip`, `opcua-explorer-linux-x64.zip` | desktop application (`opcua-explorer[.exe]`) |
| `opcua-export-windows-x64.zip`, `opcua-export-linux-x64.zip` | headless NodeSet2 exporter (`opcua-export[.exe]`), no Qt |
| `opcua-compare-windows-x64.zip`, `opcua-compare-linux-x64.zip` | headless address-space comparison (`opcua-compare[.exe]`), no Qt |

A bundled install and a `uv`-run checkout on the same machine share their settings, trust store and
project files, because both use the same per-user locations. The Windows binaries are unsigned, so
SmartScreen warns on first launch. Build them yourself with the specs in [`packaging/`](packaging/README.md).

## 4. Five-minute walkthrough

Terminal 1 — start the demo server:

```bash
uv run opcua-demo-server            # opc.tcp://127.0.0.1:4840
```

Terminal 2 — start the GUI:

```bash
uv run opcua-explorer
```

1. **Connection** tab: endpoint `opc.tcp://127.0.0.1:4840` → **Discover endpoints**. The table lists what the
   server actually advertises (security policy, mode, identity tokens, certificate fingerprint).
2. Press **Connect**. For an unencrypted local demo no certificate review is needed; for secure endpoints a
   certificate dialog appears first.
   *Need a second device?* **File → New connection** opens another session tab with its own client and pages; the
   status bar then shows how many sessions are open and how many are not connected.
3. **Address space** tab opens on the Objects folder. Expand `Plant → Line1`. Select a node to see its
   attributes (NodeId, namespace URI, DataType with resolved name, ValueRank, AccessLevel, …), its references
   with direction, and — on the *Value* sub-tab — an explicit **Read value**. Rows of nodes in the live monitor
   are shown bold and prefixed with **●** while the subscription runs, or **○** when they are added but not yet
   subscribed (per session); the marker disappears when the node is removed from the monitor list.
4. **Live monitor** tab: add `ns=2;s=Temperature` (or press **Monitor node** in the Address space tab), set a
   publishing/sampling interval and press **Start monitoring**. The left table keeps **one row per node and
   overwrites it with the latest value, StatusCode, source timestamp and notification count**; the right table is
   an append-only notification log. Select a monitored item or a log row to see the full payload. **Add node**
   subscribes further nodes into the same subscription while it runs, **Pause** freezes the log but keeps the
   latest values updating, **Stop** deletes the subscription. With several sessions open, the shared **All
   monitors** tab shows every session's items in one table (session column, filter, merged log) — read-only;
   start/stop stays in each session's own tab.
5. **Export** tab: keep *whole address space*, set *Maximum nodes* to e.g. 500, press **Download**. Progress,
   counters and cancellation are available, then **Export JSON…**, **Export CSV…** or **Export NodeSet2…**.
6. **Tests** tab: optionally put a variable NodeId (e.g. `ns=2;s=Temperature`) in *Variable node*, press
   **Run tests**. Eleven read-only checks run against the existing session; select a row to see StatusCodes and
   measurements, then **Export JSON report…** or **Export HTML report…**. Leave the variable empty and one is
   discovered near the browse root.
7. **Optional guarded write** (demo only): in *Guarded write & method call* enter `ns=2;s=Temperature`, a value
   such as `42.5`, press **Add write target**, tick the consent box and **Run tests**. A dialog repeats the exact
   target, value and restore choice; confirm it and the write is read back (and restored only if you ticked the
   restore box). The demo's `Start` method can be called the same way: object `ns=2;s=Motor`, method
   `ns=2;s=Start`, argument `900`.
8. **Compare** (two sessions): open a second session with **File → New connection** and connect it to the other
   device, then switch to the shared **Compare** tab, choose *Reference* and *Candidate* and press **Compare**.
   Both address spaces are downloaded again with one scope; the table lists every difference (missing / extra /
   changed / not comparable) and the selected row shows its attribute detail. Use *Show* or the filter field to
   narrow the list, then **Export HTML report…** or **Export JSON…**. If the old device is already gone, pick
   *Saved JSON export…* for that side and select a document previously written with **Export JSON…**.

### Saving and reopening your work

**File → Save project…** (`Ctrl+S`) writes one versioned JSON file describing every open session;
**File → Open project…** (`Ctrl+O`) replaces the current session tabs with the file's sessions (you are asked
first when a session is connected).

A project stores, per session: endpoint URL, security policy and mode, identity mode, user name (never the
password), client certificate/key **paths** (never the key password), timeouts, the auto-reconnect choice, the
address-space start NodeId, the export settings (scope, limits, namespace filter, value/dependency options), the
live-monitor target NodeIds and intervals, and the Tests settings including configured write/method targets.

It deliberately does **not** store passwords, private-key passwords, server certificates, trust decisions,
downloaded address spaces, monitor logs or test reports. Opening a project never connects, subscribes,
downloads, writes or runs anything, and the guarded-write master switch always comes back unticked. The trust
store stays in the machine configuration, so a project file cannot make a certificate trusted.

The demo model intentionally contains a **shared child** (`SharedConfig` is referenced from `Objects`, `Plant`
and `Line1`), a **cycle** (`Motor` references `Plant`), a **second namespace**, a **custom DataType**, and
values of type Double, Boolean, String, array, DateTime, ByteString and ExtensionObject.

Demo identities (documented demo values, never logged): user `demo`, password `demo-pass`.
Start with `--no-username` to disable username authentication.

## 5. Security behaviour (by design)

* **No silent downgrade.** The policy/mode pair you select must be advertised by the server. If it is not, the
  connection fails with the list of advertised combinations. Discovery always uses `SecurityPolicy#None`
  because `GetEndpoints` is a channel-level service; the session itself then uses exactly the chosen endpoint.
* **Explicit certificate approval.** For a secure endpoint the server certificate is parsed locally and its
  SHA-256 fingerprint is shown. Nothing connects until you tick "I verified this fingerprint out-of-band".
  Approved fingerprints are stored in the trust store; a **changed** certificate triggers a new review.
  The approved DER bytes are handed to `set_security(..., server_certificate=...)`, which is the only path that
  skips asyncua's own implicit certificate fetch.
* **Credentials are not persisted.** Saved connection profiles have no password field at all (there is no
  attribute to write), and `assert_no_secrets()` re-checks the JSON before it is written. Passwords and
  private-key passwords live only in memory and are cleared from the form after connecting.
* **No secrets in logs.** A logging filter redacts registered secret values, `password=…`-style patterns, PEM
  private-key blocks — including formatted exception tracebacks.
* **Read-only unless explicitly confirmed.** Browsing, download, export, live monitoring and the default test
  suite send only `Read`, `Browse`, `CreateSubscription`/`CreateMonitoredItems`, `DeleteMonitoredItems` and
  `DeleteSubscriptions`. A write or method call is sent only for the single target the operator configured and
  confirmed (section 8); nothing is discovered, repeated or automated. The one disruptive check (connection
  recovery) is opt-in and runs last.

Trust store location: `%APPDATA%\opcua-explorer\trusted_server_certificates.json` on Windows,
`~/.config/opcua-explorer/trusted_server_certificates.json` on Linux. It is managed in the GUI via
**Trusted certificates…**.

### Optional: try a secure connection

```bash
# Generate a throw-away server certificate (valid 365 days, SAN = hostname)
uv run python - <<'PY'
import asyncio, socket
from pathlib import Path
from asyncua import Client
from cryptography.x509 import ExtendedKeyUsageOID

async def main():
    client = Client("opc.tcp://127.0.0.1:4840")
    client.application_uri = "urn:opcua-explorer:demo:server"
    await client.setup_self_signed_certificate(
        Path("server.pem"), Path("server.der"),
        host_name=socket.gethostname(),
        extended=[ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH],
    )
asyncio.run(main())
PY

uv run opcua-demo-server --cert server.der --key server.pem
```

The server then advertises only `Basic256Sha256 / SignAndEncrypt`. To connect you also need a **client**
certificate whose subjectAltName contains the application URI shown in the Connection tab
(`urn:<hostname>:OPCUAExplorer:Client`) — see [docs/troubleshooting.md](docs/troubleshooting.md).
Generate it the same way with that URI and pass the `.der`/`.pem` files in the *Client certificate & private key*
group.

## 6. Project layout

```
src/opcua_explorer/
├── config.py             settings, connection profiles (no secrets), trust store, profile→settings
├── models.py             NodeRecord, Edge, AddressSpace snapshot, progress, connection info
├── defaults.py           numeric defaults shared by model, project files and tabs
├── project.py            versioned project files (configuration and editor state only)
├── formatting.py         Qt/asyncua-free cell formatting shared by the exporters
├── operator_text.py      guarded-operation confirmation + connection diagnostics text (Qt-free)
├── logging_setup.py      log bus + mandatory secret redaction
├── reporting.py          JSON and self-contained HTML test reports
├── compare.py            structural comparison of two address-space snapshots (Qt/asyncua-free)
├── cli.py                opcua-export: headless NodeSet2 export (Qt-free, same services as the GUI)
├── cli_compare.py        opcua-compare: headless comparison of two servers (Qt-free, read-only)
├── opcua/
│   ├── ua_names.py       shared NodeClass / StatusCode / VariantType names
│   ├── check_support.py  shared check primitives (skipped signal, previews, environment)
│   ├── security.py       policy/URI mapping, certificate parsing, anti-downgrade endpoint selection
│   ├── browse.py         continuation-point aware Browse/BrowseNext (releases points on cancel)
│   ├── attributes.py     batched attribute reads and decoding
│   ├── traversal.py      graph traversal: visited set, depth/node budget, concurrency, limits report
│   ├── serialize.py      JSON-safe value encoding (ByteString, DateTime, arrays, ExtensionObject)
│   ├── nodeset2.py       NodeSet2 XML: dependency closure, unsupported DataType report
│   ├── nodeset2_validation.py  well-formedness, structural checks, optional XSD validation
│   ├── test_runner.py    read-only functional checks and run orchestration
│   ├── guarded_operations.py  opt-in single-target write / method-call checks
│   ├── monitor_service.py  live data-change subscription: monitored items, JSON-safe notifications
│   └── client_service.py the only owner of an asyncua Client
├── exporters/            json_exporter.py, csv_exporter.py
├── gui/
│   ├── app.py            entry point and orderly Qt/asyncio shutdown
│   ├── main_window.py    window shell: session tabs, menus, status bar, shared tabs
│   ├── session_workspace.py  one session: its service and five pages
│   ├── async_runner.py   the only bridge between Qt and asyncio
│   └── tabs/             connection (+ connection_dialogs), browser, export, monitor, tests, logs, unified monitor, compare
└── demo/demo_server.py   local demo server with a graph-shaped model
tests/unit, tests/integration
examples/                 generated sample exports and sample reports
docs/                     architecture, API verification, troubleshooting
PLAN.md, AGENTS.md, HANDOFF.md  delivered scope, working rules, maintenance guidance
```

Key design decision: **all asyncua work runs on one asyncio loop in a worker thread**; the GUI thread only
receives plain model objects through queued Qt signals. No `await` ever runs on the GUI thread. The CLI is a
second, Qt-free front end: it owns its own event loop and calls the very same `OpcUaService` and exporters.

## 7. Export formats

**JSON** (`format_version` 1) — canonical document:

* `nodes` is an object keyed by session NodeId string, one entry per node with node class, browse/display name,
  description, data type (NodeId **and** resolved name), value rank, array dimensions, access levels,
  type definition, optional value snapshot, and the StatusCode of every attribute the server refused;
* `references` is a separate de-duplicated list of **forward** edges (`source`, `target`, `reference_type`,
  `reference_type_name`, `target_included`);
* `namespaces` always carries the **URI table**, because namespace indexes are session-local;
* `source` documents endpoint, security configuration, roots, limits and `limitations`;
* `summary` holds counts, duration, limits hit, inaccessible nodes and unsupported encodings.

**CSV** — one row per node with the important attributes, reference in/out counts, and (only when enabled)
value, status and timestamps. UTF-8 with BOM so Excel on Windows detects the encoding.

Values are **excluded by default**. When enabled, supported values are encoded consistently:
arrays keep order, `DateTime` → `{"$type":"DateTime","iso":…}`, `ByteString` → base64,
`ExtensionObject` → `{"$type":"ExtensionObject","type_id":…,"body":…}`. Anything that cannot be represented
faithfully is flagged (`encoding: "unsupported"` plus a `kind`/`detail` entry) instead of being silently
stringified.

Example files: [examples/demo-address-space.json](examples/demo-address-space.json),
[examples/demo-address-space.csv](examples/demo-address-space.csv) (generated from the demo server with
`--max-nodes 300`, values off). Regenerate them with:

```bash
uv run python scripts/generate_example_exports.py
uv run python scripts/generate_example_report.py
uv run python scripts/generate_example_nodeset.py
```

### NodeSet2 XML

The **Export NodeSet2…** action writes a `UANodeSet` document for the downloaded snapshot:

* **Dependencies are collected first, not assumed.** Before writing anything the exporter walks the snapshot and
  pulls in what a consumer needs: parents (so `ParentNodeId` resolves), supertypes, `HasTypeDefinition` targets,
  `HasEncoding` nodes, type members of exported types and `InputArguments`/`OutputArguments` of exported methods.
  Namespace-0 nodes are **referenced, never duplicated**.
* **Method arguments are part of the file.** The argument property values are written even when value export is
  off, because a method without them is declared but not callable.
* **Missing dependencies are reported, not invented.** Every dependency that cannot be read (access denied,
  budget reached, collection disabled) is listed with the reason and the relationship that needed it.
* **Custom DataTypes without a `DataTypeDefinition`** cannot be described in NodeSet2. They are excluded together
  with their members, reported as unsupported (with the affected variables), and the file stays importable.
* **The file validates itself.** After writing, the exporter re-parses its own output, checks namespace
  declarations, `ParentNodeId` resolvability and dangling references, and optionally validates against the
  official NodeSet2 XSD (`uv sync --extra validation` installs `lxml`). Without `lxml` the result says
  *schema validation skipped* — it never claims a validation that did not happen.
* **No completeness claims.** The result document lists what the file contains, what is missing and why, and
  states that an export from a live session reflects only what that session was allowed to browse.

Example: [examples/demo-nodeset2.xml](examples/demo-nodeset2.xml) with the machine-readable result in
[examples/demo-nodeset2-manifest.json](examples/demo-nodeset2-manifest.json). The committed example exports the demo's
`Objects` folder, so the custom `MeasurementType` (a type node created under `Types`) is outside that snapshot. When
a snapshot includes a custom DataType without a `DataTypeDefinition`, it is listed in `unsupported_data_types`
instead of being written (covered by the integration tests).

### Headless export: the `opcua-export` command

The GUI is not required to write a NodeSet2 file. `opcua-export` is a Qt-free command that drives exactly the same
code as the **Export** tab — same session handling, same bounded traversal, same dependency closure, same writer
and same self-validation — so a scripted export and a GUI export cannot differ:

```bash
uv run opcua-export --endpoint opc.tcp://192.168.1.100:4840 --output PSU8600.NodeSet2.xml
```

The command word is optional (`opcua-export export …` works too) and `--endpoint` / `--help` list every option.
Frequently used ones:

| Option | Meaning |
|---|---|
| `--output FILE` | Destination; default `<host>-<port>.NodeSet2.xml` in the working directory |
| `--node NODEID` (repeatable) | Start node(s); default `i=85` (Objects). Parents are added as dependencies |
| `--namespace INDEX` (repeatable) | Keep only these namespaces; namespace 0 is always kept because the type system lives there |
| `--max-nodes N`, `--max-depth N` | Bounds, `0` = unlimited (default: 20 000 nodes) |
| `--values` | Also read variable values (off by default) |
| `--no-dependencies` | Skip parent/type collection — the file will usually not import |
| `--max-dependencies N`, `--schema XSD` | Dependency budget and optional XSD validation |
| `--manifest FILE` | JSON result document (counts, namespaces, missing dependencies, unsupported DataTypes, validation) |
| `--policy`, `--mode`, `--client-certificate`, `--client-key` | Same security selection as the GUI; no silent downgrade |
| `--user`, `--password-env VAR` | User-name identity; the password comes from an environment variable or an interactive prompt, never from argv |
| `--server-certificate FILE`, `--trust-server-certificate`, `--trust-store FILE` | Explicit server-certificate approval; the application trust store is used by default |
| `--quiet`, `--verbose` | Progress/result only, or debug logging on stderr |

Progress and the export summary go to **stdout/stderr**, so the command is scriptable:

```bash
# Export one device subtree from a secured endpoint; output stays parseable by CI
OPCUA_EXPLORER_PASSWORD=… uv run opcua-export \
  --endpoint opc.tcp://plc.example:4840 \
  --node "ns=2;s=MyDevice" --namespace 2 \
  --policy Basic256Sha256 --mode SignAndEncrypt \
  --client-certificate client.der --client-key client.pem \
  --trust-store /etc/opcua-explorer/trust.json \
  --quiet --output MyDevice.NodeSet2.xml --manifest MyDevice.json
```

Exit codes: `0` success, `1` connection or export failure, `2` unusable arguments/configuration,
`3` the file was written but failed its own structural checks (it may not import).

#### Scope, namespaces and permissions

* **Scope.** Default scope is the `Objects` folder. `--node` selects any subtree, `--namespace` restricts the
  download to your own namespaces. Namespace 0 is never written into the file: the standard type system is
  *referenced*, so the importing tool must know the standard NodeSet.
* **Namespace mapping.** The exporter re-maps the *session's* namespace indexes onto the file's own
  `NamespaceUris` table (index 1, 2, … in the order the file declares them). NodeId **identifiers** are preserved;
  the numeric prefix inside the file therefore need not match the index the server used in that session. Always
  resolve a NodeId through the file's `NamespaceUris` table, not through a remembered session index.
* **Permissions.** Only what the session could read ends up in the file. Access-denied or timed-out nodes,
  exhausted budgets and unreadable dependencies are listed in the summary and in `--manifest`, never invented.
  Servers that expose only partial metadata still produce a file; the affected nodes appear as missing
  dependencies or unsupported DataTypes.
* **Cycles and size.** The traversal tracks visited NodeIds, so a graph with cycles terminates; continuation
  points are released, including on cancellation. Memory use is bounded by `--max-nodes` / `--max-dependencies`.

#### Importing the file into SiOME or UaModeler

The generated file is a plain `UANodeSet` document (schema namespace
`http://opcfoundation.org/UA/2011/03/UANodeSet.xsd`), so both tools import it directly:

* **Siemens SiOME** — *File → Import → OPC UA Nodeset XML* (or *Nodeset → Import*), select the file. SiOME shows
  the nodes under *Model / Nodeset*, and reports any reference that could not be resolved. Because namespace 0 is
  referenced but not included, a missing custom dependency appears as a "reference to unknown node" message.
* **Unified Automation UaModeler** — *File → Import → NodeSet2 XML* (or *Model → Import*). UaModeler validates
  namespaces and parent relationships on import and refuses files whose nodes have no resolvable parent, which is
  why `ParentNodeId` dependencies are always collected. Use *Model → Validate* afterwards to see dangling
  references that point outside the file.

Practical guidance:

1. Export with dependencies enabled (default) and read the printed summary: `Missing deps`, `DataTypes` and
   `References` lines tell you what an importer will complain about.
2. A custom type defined *outside* the downloaded scope shows up as an unsupported DataType; export an additional
   root (`--node i=86` for the Types folder) or the type's own NodeId to include it.
3. `--values` is only needed when the consuming tool must see initial values; method `InputArguments` /
   `OutputArguments` are always written.

### Headless comparison: the `opcua-compare` command

When a device is replaced, the question is whether the new one exposes the same information model. `opcua-compare`
downloads both address spaces through the same service layer the GUI uses and compares the snapshots. It is
read-only: nothing is written, called or subscribed.

```bash
uv run opcua-compare --reference opc.tcp://old-device:4840 --candidate opc.tcp://new-device:4840 \
  --json diff.json --report diff.html

# The old device is already gone: compare its saved JSON export with the new one live
uv run opcua-compare --reference-file old-device.json --candidate opc.tcp://new-device:4840
```

The first server is the baseline. Differences are reported in four buckets — *missing on the candidate*,
*extra on the candidate*, *changed attributes* and *not comparable* — plus namespace and (optional) reference
differences. Exit code **3 means “compared, differences found”**, which makes the command usable as a CI gate.
The same comparison is available in the GUI as the shared **Compare** tab, which writes the same reports.

**Each side is either a live endpoint or a saved JSON export.** `--reference-file` / `--candidate-file` read a
document written by the Export tab (*Export JSON…*) or by `opcua-export`'s JSON writer; a side that comes from a
document is not downloaded and needs no connection, and two documents compare completely offline. A truncated
export is reported as such: the document keeps the `limits_hit` flags of its download, so the comparison warns
that one side was cut short instead of reporting the missing nodes as differences.

| Option | Meaning |
|---|---|
| `--reference ENDPOINT`, `--candidate ENDPOINT` | Baseline and replacement |
| `--reference-file FILE`, `--candidate-file FILE` | Use a saved JSON export for that side instead of a live endpoint |
| `--node NODEID` (repeatable) | Roots to download on **both** sides; default `i=85` (Objects) |
| `--tolerance functional\|strict` | `functional` (default): node class, data type, value rank, array dimensions, access levels, type definition. `strict`: also display name and description |
| `--compare-value NODEID` (repeatable) | Compare status, variant type and payload of exactly these nodes (values are read only for them) |
| `--ignore NODEID` (repeatable) | Nodes expected to differ (current time, counters, serial numbers) |
| `--float-tolerance X` | Absolute tolerance for compared numbers (default `0` = exact) |
| `--compare-reference-names` | Also compare the reference graph (type names, sources, targets) |
| `--no-browse-path-match` | Match only by NodeId; disable the browse-path fallback for renumbered nodes |
| `--max-nodes N`, `--max-depth N` | Download bounds per side; `0` = unlimited (default unlimited — a truncated side cannot be compared) |
| `--json FILE`, `--report FILE` | Versioned JSON document and/or self-contained HTML report |
| `--policy`, `--mode`, `--client-certificate`, `--client-key`, `--user`, `--password-env` | Same security and identity selection as `opcua-export`, applied to **both** sessions |
| `--candidate-policy`, `--candidate-mode` | Override the security settings for the candidate only |
| `--reference-server-certificate`, `--candidate-server-certificate` | Per-side approved certificate; `--trust-server-certificate` approves both |
| `--quiet`, `--verbose` | Progress/result only, or debug logging on stderr |

Rules that make the result trustworthy:

* **One identity for both sessions.** `UserAccessLevel` can only be compared when both servers are asked with the
  same user; the command therefore reads the password once and uses it for both. The report states the identity.
* **Namespace URIs, not indexes.** Nodes are identified by namespace URI + identifier, so differently numbered
  namespace tables compare correctly. A NodeId printed in the report is the one *that session* used.
* **Browse-path fallback.** When a NodeId changed but its position did not, the nodes are matched by qualified
  browse path — only when that path is unambiguous on both sides.
* **Partial snapshots are flagged, not hidden.** Cancelled downloads, hit limits, inaccessible nodes and a
  different authentication mode produce warnings; nodes that could be browsed on one side only are *not
  comparable*, never “equal”.
* **Values are a point-in-time read** and are compared only for the nodes you name — a serial number or a counter
  is not a difference in functionality.

Sample files: [examples/sample-comparison.json](examples/sample-comparison.json) /
[examples/sample-comparison.html](examples/sample-comparison.html) (two demo servers, no difference) and
[examples/sample-comparison-differences.json](examples/sample-comparison-differences.json) /
[examples/sample-comparison-differences.html](examples/sample-comparison-differences.html) (the difference tables,
produced from a documented synthetic variant). Regenerate them with:

```bash
uv run python scripts/generate_example_comparison.py
```

The comparison is evidence for the scope that was browsed with that identity. It is not a statement that both
servers behave identically under load, and it sends no write or method call.

## 8. Functional tests and reports

The **Tests** tab runs eleven bounded checks against the current session; the default suite is read-only:

| Check | What it establishes |
|---|---|
| Endpoint discovery | The endpoint list is readable and the active policy/mode is still advertised |
| Session state & authentication | The session is active and which identity the server accepted |
| Namespace array | Namespace 0 is present; count and URIs are recorded |
| Browse root / target node | Hierarchical browse returns Good StatusCodes |
| Attribute reads | NodeClass/BrowseName/DisplayName are readable; every status is recorded |
| Variable read | The Value attribute returns Good, with variant type and timestamps |
| Subscription creation | `CreateSubscription` succeeds; requested and effective publishing intervals are recorded |
| Monitored-item notifications | A data change is delivered (initial value always counts as a notification) |
| Bounded read latency | 1–200 sequential reads; min/median/max are measured, not asserted |
| Connection loss & recovery | Opt-in and disruptive: closes the secure channel, reconnects, verifies a read |

Semantics that matter:

* **Creation ≠ notification.** A subscription that is created but delivers nothing within the observation
  window is *skipped* with a reason — not a failure. If the initial value arrives but the value never changes,
  the check passes with an explicit "no value change within the observation window" note.
* **Skipped is not hidden.** Opt-in checks and checks without a target explain exactly why they did not run.
* **Read-only by default.** The default checks send only Read, Browse, CreateSubscription/CreateMonitoredItems and DeleteSubscriptions. Writes and method calls are sent only for explicitly confirmed guarded targets (below).
* **Not a certification.** Every report states: *functional and interoperability checks — not official OPC UA
  certification*.

Reports (JSON and self-contained HTML) contain the endpoint, security configuration and authentication mode,
UTC timestamps and durations, the test configuration, passed/failed/skipped results, OPC UA StatusCodes per
operation, measurements, coverage limitations, inaccessible nodes and the environment (app, asyncua, PySide6,
Python, platform). Sample files:
[examples/sample-test-report.json](examples/sample-test-report.json) and
[examples/sample-test-report.html](examples/sample-test-report.html); a report that also contains a guarded write
and a method call is available as
[examples/sample-test-report-guarded.json](examples/sample-test-report-guarded.json) /
[examples/sample-test-report-guarded.html](examples/sample-test-report-guarded.html).

### Guarded write and method call (optional, off by default)

The same **Tests** tab can perform one write and/or one method call — but only as a deliberate, confirmed
action:

* **Off by default.** Nothing is written and no method is called unless you tick *"I understand and allow these
  operations"* **and** add the target. A configured target with the switch off is reported as *skipped*, never
  sent.
* **One target per operation.** You enter the variable NodeId and the value, or the object + method NodeId and
  the arguments. The tool never lists writable nodes, never invokes discovered methods, never repeats a call and
  never runs a stress or scan mode.
* **Confirmation shows the exact operation.** Before the run a dialog repeats endpoint, target NodeId, value or
  method + argument list, whether the previous value will be restored, and a warning that a change on a server
  connected to real equipment can move machinery. Declining runs nothing.
* **Server-side validation first.** The value is parsed with the node's DataType and ValueRank; method arguments
  are parsed with the server's `InputArguments` and the count must match. Anything that cannot be typed safely
  (custom structures, enumerations, matrices, text that is not a number) is refused locally — the operation is
  *skipped* with the reason and nothing is sent.
* **Verified effect.** A write is read back and compared; a mismatch is a failure. A method call reports the
  StatusCode and output arguments.
* **No silent restore.** A previous value is restored only if you tick *"Restore the previous value"*, and the
  restore itself is verified. It is never automatic.

Every guarded operation appears in the JSON/HTML report with its target, the confirmed flag, the parsed value,
StatusCodes and the restore outcome, so a change can always be traced afterwards.

## 9. Tests of the application itself

```bash
uv sync
uv run pytest -q              # unit + integration (starts a local demo server)
uv run pytest tests/unit -q   # fast, no network
```

* **Unit**: value serialization/flagging, certificate summaries and endpoint selection (no downgrade),
  trust store + settings (no secrets on disk), log redaction, JSON/CSV document shape, NodeId parsing,
  reference classification, report documents and HTML escaping, NodeSet2 structural/schema validation,
  guarded-write value parsing (ranges, arrays, ISO dates, refused custom types) and the export CLI's argument
  mapping and certificate-approval decisions (no network).
* **Integration**: against the in-process demo server — discovery, anonymous and username authentication,
  browsing, attribute/reference details, bounded and depth-limited traversal on a cyclic graph, shared
  children, namespace filter, value encoding, cancellation, JSON/CSV files, NodeSet2 export with a round-trip
  import into a fresh `asyncua` server, guarded writes (success, read-back, refused value, non-writable node,
  restore only on request) and method calls (validated arguments, wrong argument count), secure connection with
  mutual certificates, certificate-change
  rejection, not-advertised security rejection, the functional check suite (including the constant-value case
  and the opt-in recovery check) and report files. The headless `opcua-export` command is exercised end to end
  (Objects scope, start-node scope with parent closure, namespace scope, manifest, unsupported custom DataType,
  nothing-to-write, unreachable endpoint, username authentication, missing schema file).
* **GUI** (headless, `-platform offscreen`): asyncio bridge result/cancellation, window wiring, and end-to-end
  runs driven through the real tabs (connect → browse → download → JSON export; connect → download → NodeSet2
  export; connect → run tests; connect → live monitor and merged monitor view; connect → guarded write with
  confirmation accepted/declined; connection loss and automatic reconnect; two independent sessions; project
  save/load; live-monitor markers in the address-space tree).

## 10. Limitations

The implemented scope is complete; the following are **not** claimed:

* **NodeSet2 export is not a model authoring tool.** It writes what the session could read and says so. It does
  not merge, patch or version models, and it does not attempt to make an arbitrary subtree independently
  importable: custom DataTypes without a `DataTypeDefinition` are excluded and reported.
* **Writes and method calls are single-target and opt-in.** One node, one value, one call, one confirmation.
  There is no batch write, no "write to all writable nodes", no discovery-driven method invocation, no
  automatic value restore and no write stress mode. Only namespace-0 built-in DataTypes can be written;
  custom structures and enumerations are refused rather than encoded on a guess.
* **No stress testing.** Latency is a bounded sample of ordinary reads, not a benchmark; there is no load or
  soak testing and no automatic network scanning.
* **Connection recovery is opt-in** in the functional-check suite and disruptive there: the *check* closes the
  secure channel and reconnects, which interrupts any active subscription. It runs last and is skipped by
  default. This is separate from the session-level **Keep reconnecting when the link drops** toggle (Connection
  tab → *Resilience*, on by default), which retries a dropped link in the background without operator action.
* **The suite covers one session, one browse root and one variable** — not every node or DataType.
* **Values are a point-in-time read**, not history; no HistoryRead support.
* **Values in NodeSet2 XML are limited** — asyncua's exporter describes value export as limited, so with
  *Include values* on, some fields (for example the `Name` of a method argument) can be lost on re-import. The
  method argument properties themselves are always written, which is what a consumer needs to call the method.
* **Custom structures** are exported as far as asyncua can decode them. A binary-encoded `ExtensionObject`
  body without a type dictionary is base64-encoded and flagged as undecoded.
* **Namespace filter** keeps namespace 0 (standard type system and folder structure) by design.
* **Concurrent sessions share one window and one asyncio loop.** Each session has its own client, namespace
  table, download snapshot, live subscription and reconnect supervisor. The shared **Compare** tab compares two
  of them (the same comparison the headless `opcua-compare` command runs); there is no automatic merge of
  sessions. A device that limits concurrent sessions can refuse the second one — the StatusCode is shown,
  nothing is retried silently.
* **A project is a configuration snapshot, not a session restore.** Opening one restores editors and pending
  monitor targets; it does not reconnect, re-subscribe, re-download or restore logs and reports.
* **A comparison is structural evidence, not an equivalence guarantee.** The **Compare** tab and
  `opcua-compare` compare what each session could browse and read with one identity, from one point in time.
  Neither compares behaviour under load, internal limits, or anything that requires a write.
* Downloads reflect what the session was allowed to browse: access-denied nodes, timeouts and disconnects are
  reported (`inaccessible_nodes`) instead of being hidden.

Not included by policy: unrestricted stress testing, automatic production-network scanning, automatic writes to
writable nodes, automatic method invocation, automatic restoration of previous values.

## 11. Troubleshooting

Start with [docs/troubleshooting.md](docs/troubleshooting.md): endpoint URLs, certificates and application URIs,
authentication, timeouts, custom data types, subscription behaviour and Qt platform issues on Linux. The in-app
**Logs & reports** tab contains a redacted log you can save and attach to a bug report.

## 12. License

MIT.
