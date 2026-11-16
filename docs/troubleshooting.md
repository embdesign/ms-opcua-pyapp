# Troubleshooting

The application writes a redacted log you can save from **Logs & reports → Save log…**. Passwords and
private-key passwords never appear in it.

---

## 1. Endpoint URL

**Symptom:** "Could not read the endpoint list … Connection refused" or "Endpoint discovery timed out".

* The scheme must be `opc.tcp://` (the field adds it if you omit the scheme).
* Default OPC UA port is **4840**; some servers use 4841/4842 or a TLS port (`opc.https://`, port 443).
* Verify reachability outside the app:
  * Linux: `nc -vz 192.168.0.10 4840`
  * Windows: `Test-NetConnection 192.168.0.10 -Port 4840`
* A server behind NAT/firewall may advertise endpoints with a hostname or IP the client cannot reach. The
  advertised URL is shown in the endpoint table and in the diagnostics; if it differs from what you can reach,
  fix the server's endpoint configuration — the client deliberately connects to the URL you typed.
* `BadTimeout` during connect: raise **Request timeout** and **Connect timeout** on the Connection tab.

**Symptom:** the status bar still shows "Connected" after the server was shut down.

* The Connection tab polls the session state every 3 s. With **Keep reconnecting when the link drops** enabled
  (Connection tab → *Resilience*, on by default) the window switches to **Reconnecting…**, the session is kept
  and the client retries the same endpoint, security policy and identity with a backoff of 1 s … 30 s. While it
  retries, asyncua cycles through `disconnected`/`connecting`/`socket_open`/`channel_open`/`reconnecting` — all
  of these are treated as "still trying", not as a final loss. When the server is back, the chip returns to
  **Connected · session restored** and the event is written to **Logs & reports**. Press **Disconnect** to stop
  the retries.
* With the option off, the status bar shows **Link lost**, the other tabs are disabled, the dead session is
  released, and you reconnect from the Connection tab.
* Loss detection combines asyncua's connection supervisor (health probe every ~1 s) with that 3 s poll, so a
  dead-but-open TCP connection can take a few seconds to surface.
* While the client is retrying, an operation started from another tab fails after the **Request timeout**
  instead of hanging. An already-downloaded address space stays on screen but reflects the time before the loss.
* A secure server that changed its certificate while it was unreachable cannot be reconnected automatically:
  the retries keep failing until you reconnect manually and approve the new fingerprint.

## 2. Certificates and application URIs

This is the most common obstacle. Both sides need a certificate that contains their **application URI**.

**"certificate does not contain the application uri"** (asyncua warning) or
`set_security` fails immediately:

* The client certificate must contain `urn:<hostname>:OPCUAExplorer:Client` as a `subjectAltName` URI. The exact
  URI your client will use is displayed on the Connection tab (it is derived from the local hostname).
* Generate a matching certificate with asyncua:

```python
import asyncio, socket
from pathlib import Path
from asyncua import Client
from cryptography.x509 import ExtendedKeyUsageOID

async def main():
    client = Client("opc.tcp://127.0.0.1:4840")
    client.application_uri = f"urn:{socket.gethostname()}:OPCUAExplorer:Client"
    await client.setup_self_signed_certificate(
        Path("client.pem"), Path("client.der"),
        host_name=socket.gethostname(),
        extended=[ExtendedKeyUsageOID.CLIENT_AUTH],
    )

asyncio.run(main())
```

**"The server certificate must be reviewed and approved before connecting"** — this is by design: a secure
connection needs the fingerprint review dialog. If the dialog does not appear, the certificate review was
skipped because the endpoint was not recognised as secure; check the selected policy/mode.

**"The certificate advertised by the server changed"** — the endpoint advertises a different certificate than
the one you approved. Re-review it (**Connection tab → Trusted certificates…** → remove the old entry) only if
the change is expected (renewal/redeployment).

**Server rejects the client certificate** (`BadCertificateUntrusted`, `BadSecurityChecksFailed`):

* import the client certificate into the server's trust list (vendor tool, `trusted/certs` directory, …);
* make sure the server's clock is within the certificate validity period;
* check the certificate has `ClientAuth` extended key usage.

**Trust store location**

* Windows: `%APPDATA%\opcua-explorer\trusted_server_certificates.json`
* Linux: `~/.config/opcua-explorer/trusted_server_certificates.json`

Deleting an entry forces a new fingerprint review on the next connection.

## 3. Authentication

* **Anonymous** — the server decides; if it rejects anonymous access you get `BadIdentityTokenRejected` or
  `BadUserAccessDenied`.
* **Username/password** — the endpoint table shows whether `UserName` is among the advertised identity tokens.
  Username tokens over an unencrypted channel are visible on the wire; the app warns about this in the
  Security box. Passwords are never saved and are cleared from the form after connecting (checkbox on by
  default).
* **Certificate** — requires both a client certificate and private key; the server must trust the certificate.
* Demo server credentials: user `demo`, password `demo-pass` (or start it with `--no-username` to disable).
* Wrong credentials produce a `ConnectionFailedError` that contains the OPC UA StatusCode reported by the
  server. The demo server answers `BadUserAccessDenied` for a wrong password.

## 4. Security policy / mode selection

* **"The server does not advertise Basic256Sha256 / Sign"** — exactly the point of the no-downgrade rule.
  Pick a combination that appears in the advertised endpoint table, or fix the server configuration.
  The application never falls back on its own.
* **SecurityPolicy None** means no signing and no encryption; the app always shows a warning. Use it only on an
  isolated test network.
* **Basic128Rsa15 / Basic256** are deprecated by the OPC Foundation; the policy list marks them and warns.

## 5. Browsing and downloads

* **Empty tree / no children** — the node may genuinely have no hierarchical children (an expandable node that
  turns out to be empty loses its expander), or your user identity may lack browse rights on it.
  `BadUserAccessDenied` on a node is reported in the status line and in the export's `inaccessible_nodes`.
* **"Traversal limits stopped the download"** — you hit **Maximum nodes** or **Maximum depth**. The export
  summary lists which limit was reached; the JSON contains `source.limitations`.
* **Many references point outside the snapshot** (`target_included: false`) — normal for a subtree download:
  type definitions, parent references and nodes beyond the depth/node budget are referenced but not collected.
* **Download seems to duplicate nodes** — the same node can appear under several parents in the tree because
  the address space is a graph. In the export each node appears once and every edge is listed separately.
* **The tree shows a child only once although the server returned it several times** — the tree keeps exactly
  one row per target NodeId. Servers often reference the same child through both `Organizes` and `HasComponent`
  (or return the same reference twice); the merged references are named in the tree row's tooltip and remain
  visible in the node's *References* panel. A server that returns the **same NodeId for two different names** is
  inconsistent: the tree keeps the first name and logs a warning, because both entries would open the same node.
* **Namespace filter** always keeps namespace 0: the standard type system and the Objects/Types folder
  structure live there. Select namespaces to restrict *which nodes are followed*, not to strip ns 0.
* **Cancelling** a download returns a partial snapshot marked `cancelled: true`; outstanding browse
  continuation points are released where the server permits it.

## 6. Custom data types and unsupported values

* A variable whose DataType is a server-defined structure may be exported as
  `{"$type": "ExtensionObject", "body_encoding": "binary-base64"}` with a flagged note — the body could not be
  decoded because the client does not have the server's type dictionary.
* `asyncua` can load type definitions (`Client.load_data_type_definitions()`), but that is not enabled here: it
  executes server-provided type descriptions and needs the server's data type nodes to be browsable. NodeSet2
  export instead *reports* DataTypes that have no `DataTypeDefinition` rather than writing a node other servers
  cannot import.
* Unknown Python objects never crash an export: they become `{"$type": "Unsupported", …}` and appear in
  `unsupported_encodings`.
* **`DecodeError: ValueError: 16777215 is not a valid MessageSecurityMode`** ("… is not a valid `<Enum>`") — the
  server put a value into a structure field that is not a member of the OPC UA enumeration, most commonly
  `0xFFFFFF` (16777215) used as an out-of-band "null" marker. The client's generated decoders reject it while
  decoding the *whole* Read response, so the value cannot be recovered. It is recorded per node in
  `unavailable_attributes.Value` and counted in the download notes; the remaining values of the same request are
  read normally (the client re-reads the request in halves to isolate the offending node). The alternative is
  `include values = off` if you do not need the values at all.

## 7. Qt / platform issues
* **Linux: "Could not load the Qt platform plugin xcb"** — missing system libraries. On Debian/Ubuntu:
  `sudo apt install libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxkbcommon-x11-0`.
  `libxcb-cursor0` is required since Qt 6.5; the others are loaded by the same plugin and are commonly absent on
  minimal/headless images. To see exactly what is missing for your install, run
  `ldd "$(python -c 'import PySide6, os; print(os.path.dirname(PySide6.__file__))')/Qt/plugins/platforms/libqxcb.so" | grep "not found"`.
  If the session is Wayland, `QT_QPA_PLATFORM=wayland` (or leaving it unset) avoids the xcb plugin; on X11
  (`XDG_SESSION_TYPE=x11`) the libraries above must be installed. Note that `QT_QPA_PLATFORM=offscreen` only
  suppresses the error — it creates no visible window, so it is for tests and CI, not for interactive use.
* **Headless/CI** — use `QT_QPA_PLATFORM=offscreen`; the test suite does this automatically.
* **Windows: high-DPI blurry text** — Qt 6 handles scaling automatically; if the display scaling is unusual,
  set `QT_ENABLE_HIGHDPI_SCALING=1` before starting.
* **The window freezes** — it should not: all I/O runs in the asyncio worker thread. If it does, check the log
  for a Python exception in a callback; the UI stays usable and the operation reports an error.
* **Crash: `Fatal Python error: none_dealloc: deallocating None …` while browsing** — this is a **PySide6
  6.12.0** regression in `QTreeWidgetItem` (Shiboken refcount bug), not an application fault. It aborts the
  process after a few hundred tree rows, with the traceback pointing at a Qt setter such as `setForeground`.
  Check the version and re-sync:

  ```bash
  uv run python -c "import PySide6; print(PySide6.__version__)"   # must print 6.11.3
  uv sync                                                          # restores the pinned version
  ```

  `pyproject.toml` pins `PySide6==6.11.3` for this reason; `tests/integration/test_pyside_stability.py` runs the
  reproducer in a subprocess, so an upgrade that reintroduces the crash fails the suite instead of a session.
  Do not “fix” it by changing item painting in `browser_tab.py` — the same churn crashes a script that uses
  nothing but Qt API.

## 8. NodeSet2 export

* **`Validation: not-validated (schema: skipped)`** — a schema path was given but `lxml` is missing. Install the
  extra (`uv sync --extra validation`) or clear the schema field. The exporter never reports a validation it did
  not perform.
* **"custom DataType(s) without a DataTypeDefinition were not written"** — the server defines a structure type
  without describing its fields. Such a `<UADataType>` cannot be represented in NodeSet2; the type *and* its
  members are excluded so the file stays importable, and the affected variables are listed.
* **"reference(s) point to nodes that are not part of the file"** — the export covers a snapshot (often
  truncated by `max_nodes`) and the target lies outside it. Re-export from a larger scope if you need a
  self-contained file.
* **Importing the file into another server fails with `BadParentNodeIdInvalid`** — a parent could not be read
  (access denied) or dependency collection was disabled. Check `missing_dependencies` in the manifest.
* **Namespace indexes differ from the session** — expected: the file remaps the namespaces it writes to
  `ns=1..N`. Use the `NamespaceUris` table, not the session index.
* **SiOME/UaModeler refuses the file** — both tools require every `ParentNodeId` and type definition to resolve.
  Re-run the export without `--no-dependencies` and check the summary lines `Missing deps` / `References`; if
  dependencies are still listed, the server denied access to them and the exported region must be widened.
* **`opcua-export`: `Nothing to write` (exit code 1)** — every node of the downloaded region was in namespace 0.
  `--namespace 0` intentionally selects only the standard namespace, which is never written into the file; widen
  the scope with `--node` or drop the namespace filter.
* **`opcua-export`: `is not advertised by the server`** — the requested policy/mode pair is not offered. The
  message lists what the server does advertise; there is no silent downgrade.
* **`opcua-export`: `The server certificate is not approved`** — the fingerprint was printed; verify it
  out-of-band and re-run with `--trust-server-certificate` (recorded in the trust store) or `--server-certificate
  FILE` whose SHA-256 must match the advertised certificate.
* **`opcua-export`: `No password available`** — pass `--password-env VAR` or run in an interactive terminal; a
  password is never accepted as a command-line argument because argv is visible to other users.
* **`opcua-export` exit code 3** — the file was written but failed its own structural checks (for example a
  namespace that was not declared). The file exists; treat it as unusable until the summary explains why.

## 9. Guarded write and method call

* **The write check is *skipped* with "Writes and method calls are disabled"** — the consent checkbox is off.
  Configured targets are reported, never silently executed.
* **"… is not a built-in DataType and exposes no EnumDefinition, EnumStrings/EnumValues or
  StructureDefinition; it cannot be written safely"** — the variable's custom type has no readable definition.
  Built-in DataTypes, enumerations and structures with a `DataTypeDefinition` are supported; unions,
  optional-field structures, matrices and recursive definitions are refused locally, so nothing is sent.
* **"An array value is expected as a JSON array"** — a variable with ValueRank `0` or `1` needs `[1, 2, 3]`,
  not `1,2,3` (`0` means "one or more dimensions", and several servers report it for array variables). Ranks
  `-2`/`-3` are decided from the entered text; matrices and other ranks that a flat JSON list cannot represent
  are refused instead of being guessed.
* **The write fails with `BadNotWritable` / `BadUserAccessDenied`** — the server refused it: the node's access
  level or the session's user rights do not allow writing. The StatusCode is recorded in the report.
* **"The method expects N argument(s) … but M were configured"** — the arguments are validated against the
  server's `InputArguments`; nothing was called. Add or remove arguments to match the signature.
* **`BadNoMatch` when calling a method** — the method node is not a child of the object node you named. Use the
  object that actually owns the method (`HasComponent` from the object).
* **The previous value was not restored** — restoration is opt-in per target ("Restore the previous value").
  When it is enabled, an unreadable previous value is reported instead of being invented.

## 10. Functional tests (Tests tab)

**A check is skipped** — that is information, not a failure. Common reasons:

* *Connection loss & recovery* is opt-in (it closes the secure channel and reconnects, discarding
  subscriptions). Enable the checkbox to run it.
* *Monitored-item notifications* reports "no notification arrived within N s". Increase the notification window
  (some servers publish only on keep-alive intervals) and check that the variable is actually updated.
* No variable node was configured **and** none was found near the browse root — enter a NodeId explicitly.

**The notification check passes but reports "no value change"** — correct behaviour for a constant variable.
OPC UA sends one initial value notification per monitored item; the report states explicitly that this is not a
failure. Point the check at a variable that changes if you want to observe a change.

**A check failed** — select the row to see the StatusCodes per operation and the recorded data. Typical causes:

* `BadUserAccessDenied` on Browse/Read: the identity lacks rights on that node; try another node or identity.
* `BadTimeout`: raise the request timeout / check the network.
* `BadNoResult`, `BadCommunicationError`: the session may have dropped; reconnect and re-run.
* `BadAttributeIdInvalid` on optional attributes is normal and does not fail the suite (only the core
  attributes are required).

**The run stops early / results are partial** — the run was cancelled (Cancel button or closing the app).
Partial results are labelled `cancelled` in the report and no subscription is left behind.

**The recovery check left the session disconnected** — it reconnects with the same client; if it failed, close
and re-open the session from the Connection tab. The report records `state_before`, `state_after_close`,
`read_after_loss`, `recovery_ms` and `read_after_recovery`.

**Reports** — the HTML file is self-contained (no external fonts or scripts), so it can be attached to a ticket.
Both formats include the disclaimer that these are functional/interoperability checks, not OPC UA certification.

## 11. Live monitor

**No notifications arrive.** A subscription publishes on change or on the keep-alive interval:

* Check the per-item status in the monitored-item list. `BadNodeIdUnknown`, `BadNodeClassInvalid` (the node is
  not a Variable), `BadNotReadable` or `BadUserAccessDenied` mean the server refused the item in
  `CreateMonitoredItems`; a rejected item never publishes.
* Servers revise too-small publishing/sampling intervals upward. Increase both values and retry.
* Many servers apply a default deadband, so a slowly drifting value may not notify at all. The initial value
  notification is still sent once; if even that is missing, verify the node with **Read value** on the Address
  space tab.

**Rows stop arriving although the status is still Good** — the monitor is paused (rows are counted as *not added
  while paused* in the counters line; the per-node latest values keep updating) or the row cap was reached; the
  oldest rows are removed first. Increase **Keep rows** if you need a longer history.

**Adding a node while running shows "rejected: …"** — that NodeId is not a readable Variable or the identity
  lacks rights. The rest of the subscription keeps running; remove the row and continue.

**No notifications after a reconnect** — with **Keep reconnecting when the link drops** enabled, a running
  live monitor is restarted automatically and the item table shows *resubscribing…*. If you reconnect manually or
  the server discarded the subscription, start monitoring again.

## 12. Tests of the application itself

```bash
uv run pytest -q                     # everything (starts local demo servers)
uv run pytest tests/unit -q          # fast, no network
QT_QPA_PLATFORM=offscreen uv run pytest tests/integration/test_gui_*.py -q
```

Integration tests bind to `127.0.0.1` on an ephemeral port. If they fail with "address already in use" or
"demo server did not start", another process may be blocking local sockets or port binding; the demo server
needs a free loopback port.

## 13. Reporting a problem

Attach:

1. the redacted log (**Logs & reports → Save log…**),
2. the endpoint row from the Connection tab (policy, mode, certificate SHA-256),
3. for export problems: the JSON export summary (`summary` block) — it lists limits, inaccessible nodes and
   unsupported encodings,
4. for test problems: the JSON test report (it contains the configuration, per-check StatusCodes and coverage
   limitations).

Never attach private keys or password-containing files.
