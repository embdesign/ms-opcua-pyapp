Act as a senior Python developer and OPC UA engineer. Create a maintainable desktop application for browsing an OPC UA server, downloading its address space, and performing general OPC UA functional testing.

Use Python, asyncua for OPC UA communication, and PySide6 for the GUI. Manage the project with uv and pyproject.toml. Support native Windows and Linux installations without Docker.

Before implementation, verify the actual APIs of the selected asyncua version. Do not invent library methods. Pin dependencies and document supported versions.

1. Connection and security

Allow the user to:
- Enter an endpoint URL, such as opc.tcp://192.168.0.10:4840.
- Discover and select the server's advertised endpoints.
- Select an advertised security policy and message security mode.
- Authenticate anonymously, with username/password, or with a certificate where supported.
- Configure application certificates, private keys, and trusted server certificates.
- Configure connection and request timeouts.
- Save, reuse and delete named connection profiles. Profiles never contain passwords or private-key
  passwords.
- Optionally keep retrying automatically when the link drops (bounded backoff, same security and identity),
  and stop retrying explicitly.
- Connect, disconnect, and see connection status and useful diagnostics, including the server namespace table.

Requirements:
- Never silently downgrade security.
- Require explicit approval before trusting an unknown server certificate.
- Never log passwords or private keys.
- Keep credentials out of saved connection profiles by default.

2. Address-space browser

Display a lazy-loaded tree starting from Root or Objects, with an option to browse from a user-provided NodeId.

For each selected node, show applicable attributes:
- NodeId and namespace URI.
- BrowseName and DisplayName.
- NodeClass and Description.
- DataType, ValueRank, and ArrayDimensions.
- AccessLevel and UserAccessLevel.
- TypeDefinition.
- References, including reference type and direction.
- Value, StatusCode, SourceTimestamp, and ServerTimestamp when read.

Support search within downloaded nodes and copying NodeIds. Mark nodes that are queued for or actively
monitored by this session's live monitor, without changing the shared snapshot.

Important: OPC UA address spaces are graphs, not strict trees. Preserve references, handle shared children, and prevent infinite traversal using visited-node tracking.

3. Address-space download and export

Allow downloading the entire accessible address space or a selected subtree.

Provide:
- Namespace filters.
- Maximum depth and maximum node count.
- Bounded request concurrency and configurable batch sizes.
- Browse continuation-point handling.
- Progress, cancellation, and an export summary.
- Graceful handling of access-denied nodes, timeouts, and disconnects.

Release continuation points where possible, including after cancellation.

Export formats:
- JSON: canonical nodes indexed by identifier, with a separate reference list.
- CSV: a flat inventory of nodes and important attributes.
- NodeSet2 XML: use a verified supported exporter or a standards-compliant implementation.

For portability, export the namespace URI table alongside session NodeIds. Namespace indexes can change between sessions.

Variable values must be optional and excluded by default. Serialize supported values consistently, including arrays, timestamps, ByteStrings, and ExtensionObjects. Clearly flag unsupported encodings.

For NodeSet2 XML:
- Include required type and reference dependencies where accessible.
- Preserve namespace mappings and references.
- Report missing dependencies and unsupported custom DataTypes.
- Validate against the official schema when available.
- Do not claim that browsing a live server always reconstructs a complete, independently importable information model.
- Do not assume an XML exporter automatically browses the server: explicitly collect nodes and dependencies first.

4. General OPC UA testing

Implement a read-only test suite covering:
- Endpoint discovery and connection.
- Authentication outcomes.
- Browsing and attribute reads.
- Variable reads and returned StatusCodes.
- Subscription creation and monitored-item notifications.
- Configurable sampling and publishing intervals.
- Connection loss and recovery.
- Small, bounded latency measurements.

For subscriptions, distinguish successful creation from receiving a data change: a value that does not change must not automatically cause a test failure.

Provide optional, explicitly enabled tests for:
- Writing a user-selected variable.
- Calling a user-selected method with validated arguments.

Safety requirements:
- Default to read-only operation.
- Never automatically write to every writable node or invoke discovered methods.
- Require confirmation showing the exact target, operation, and arguments.
- Explain that writes and method calls may affect physical equipment.
- Never automatically restore a previous value unless the user explicitly requests it.
- Do not include unrestricted stress testing or automatic production-network scanning.

Describe these as functional and interoperability checks, not official OPC UA certification.

5. Reporting and usability

Provide a window that can host several independent sessions side by side. Each session has its own
OPC UA client, namespace table, address-space snapshot, live subscription and reconnect supervisor, and
contains these pages:
- Connection.
- Address-space browser.
- Export.
- Live monitor (data-change subscriptions independent of the test suite).
- Tests.

Two shared tabs sit outside the session pages:
- All monitors: a read-only merged view of every session's monitored items and notification log, with a
  session filter.
- Logs and reports: live, secret-redacted logs plus operational summaries and written report paths.

Save and reopen the configuration and editor state of every session in one versioned project file. A project
never stores passwords, private-key passwords, certificate bytes or trust decisions, and opening one never
connects, subscribes, downloads, writes or runs anything.

Export test reports as JSON and HTML with:
- Server endpoint and selected security configuration.
- Test configuration and bounded test environment versions.
- UTC timestamps and durations.
- Passed, failed, and skipped results.
- OPC UA StatusCodes and error details.
- Coverage limitations and inaccessible nodes.

Keep the GUI responsive during network operations. Integrate asyncio safely with Qt, and clean up sessions, subscriptions, and tasks on cancellation or shutdown.

6. Architecture and deliverables

Separate:
- GUI.
- OPC UA client service.
- Graph traversal and data model.
- Exporters.
- Test runner.
- Reporting and configuration.

Deliver:
- Complete runnable project, not pseudocode.
- pyproject.toml and uv setup commands.
- README with Windows and Linux instructions.
- A local demonstration server with representative nodes.
- Unit tests and integration tests using the local server.
- Example exports and sample reports.
- Versioned project files and connection profiles that contain no secrets.
- Troubleshooting for certificates, endpoint URLs, authentication, and custom data types.

Build in phases:
1. Connection, browsing, and JSON/CSV export.
2. Read-only tests, subscriptions, and reporting.
3. NodeSet2 export with dependency handling and validation.
4. Guarded write and method-call tests.

First present the architecture and implementation plan, then implement phase 1 completely. Clearly identify any limitations rather than presenting unfinished features as working.

## Additional scope implemented beyond the original brief (verified 2026-11-13)

These capabilities are implemented, tested and documented, although the brief above did not ask for them
explicitly:

- **Multiple independent sessions in one window.** `File → New connection` (`Ctrl+N`) adds a session tab;
  closing it releases only that client. All sessions share one asyncio loop and one trust store but no client,
  namespace table, snapshot, subscription or reconnect state.
- **Live monitor page.** One data-change subscription per session, with in-place latest-value rows, an
  append-only bounded notification log, pause/clear, add/remove while running, and configurable
  publishing/sampling intervals. It is separate from the functional test suite.
- **All monitors page.** Read-only aggregation of every session's monitored items and notifications, with a
  session filter; it never starts or stops a subscription.
- **Address-space monitor markers.** Monitored nodes are marked in the tree (● running, ○ queued) without
  modifying the downloaded snapshot.
- **Project files.** `File → Open/Save project…` (`Ctrl+O` / `Ctrl+S`) stores each session's connection
  profile, start node, export settings, pending monitor targets and tests configuration in a versioned
  `.opcua-project.json` document. Credentials, certificate bytes, trust decisions and `allow_writes` are never
  stored.
- **Automatic reconnect (Resilience).** A setting, on by default, makes the client retry a dropped link with
  exponential backoff up to 30 s, reusing the same endpoint, security policy, identity and approved server
  certificate. With it off, a loss releases the session and the GUI reports **Link lost**.
- **Named connection profiles and trust-store management.** Profiles are persisted without secrets; approved
  server certificates can be reviewed and removed from a dedicated dialog.
- **Richer NodeSet2 export.** Dependency closure covers parents, supertypes, type definitions, encodings,
  type members and method `InputArguments`/`OutputArguments`; unsupported custom DataTypes and missing
  dependencies are reported; optional XSD validation runs when a schema path and `lxml` are available.
- **Delivered test and report assets.** 226 automated tests (unit, demo-server integration and headless GUI),
  a local demo server with a graph-shaped model, example JSON/CSV/NodeSet2 files and sample JSON/HTML reports.

Deliberate non-goals remain as documented in `README.md` (no history reads, no batch/scan/stress writes, no
unattended production scanning, single-target guarded operations only).