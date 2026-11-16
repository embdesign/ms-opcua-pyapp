# Examples

Files in this folder are **synthetic**: they were produced against the bundled demo server or written by hand.
No capture of customer hardware, no vendor name, no serial number, no address of a real device is recorded here.

## Prerequisites for the sequence demos

The `demo-0*.opcua-sequence.json` files address the nodes of the bundled demo server and expect them to hold
their start values, so start the server **without** its value updates:

```bash
uv run opcua-demo-server --port 4840 --no-updates
```

`--no-updates` keeps the demo values static, which makes `expect` / `assert` / `wait_until` steps deterministic
(with updates running, `Timestamp`, `Samples` and the counters change under the sequence).

Run a document headless, or open it in the **Sequences** tab of `uv run opcua-explorer`:

```bash
uv run opcua-run-sequence examples/demo-01-write-readback.opcua-sequence.json \
    --endpoint opc.tcp://127.0.0.1:4840 --dry-run          # resolves everything, sends nothing
uv run opcua-run-sequence examples/demo-01-write-readback.opcua-sequence.json \
    --endpoint opc.tcp://127.0.0.1:4840                    # one send per step
```

`--input name=value` overrides an entry of the document's `inputs`; `--json run.json` writes the whole run
(gate outcome, every step status, StatusCodes, previous and new values) as JSON. Exit codes are `0` (all steps
passed), `1` (failed step) and `2` (document or connection error).

## Sequence demos

| Document | Shows | Expected exit code |
| --- | --- | --- |
| `demo-01-write-readback.opcua-sequence.json` | `write` with read-back verification for a Double, a String and a Double array; `expect` with a tolerance; a `delay` whose seconds come from an input | 0 |
| `demo-02-method-calls.opcua-sequence.json` | the four ways to address a method: `method_node_id`, `method_name` (`2:Start`), browse path + `method_name`, browse path + `method_node_id`; each call verified through the node the method writes | 0 |
| `demo-03-wait-and-optional.opcua-sequence.json` | an `assert` precondition on the server state, an `optional` `ensure` for a node that does not exist, plus two `wait_until` polls instead of fixed delays | 0 |
| `demo-04-gate-from-writes.opcua-sequence.json` | a full gate built from **ordinary write steps** — `on`, `verify`, `keepalive` (with `every_s` and `on_failure: warn`) and `off` — released in `finally`, also after a failure or **Stop** | 0 |
| `demo-05-failure-handling.opcua-sequence.json` | `abort_on_failure: false`: the first step writes to a non-existent node, the remaining steps still run, and the run is reported as failed | **1** (deliberate) |
| `demo-06-value-types.opcua-sequence.json` | how text maps onto the server's DataTypes: String, DateTime (ISO 8601), ByteString (base64) and a Double array, each read back | 0 |

`demo-setpoint.opcua-sequence.json` is the smallest possible document (one guarded write plus a comparison) and
`demo-method-call.opcua-sequence.json` is the short method-call example.

## Real-device templates

`device-gate.opcua-sequence.json` and `demo-psu.opcua-sequence.json` are **templates for a real device**: gate
acquire / verify / keepalive / release, an optional remote-mode step, and a PSU-style ordered workflow. Their
NodeIds and browse names must be adapted before use, and running them against hardware is a write operation — do
it only with the operator's authorization and with the device in a safe state. Nothing in the application names
a lock or a vendor: the gate is exactly the steps the document lists.

## Sequence document in one screen

```jsonc
{
  "format": "opcua-explorer-sequence",
  "version": 1,
  "name": "shown in the timeline and the report",
  "description": "optional, shown in the tab",
  "abort_on_failure": true,               // false: keep running, still report FAILED
  "inputs": { "speed": "900" },           // referenced as ${speed} anywhere in the steps
  "preconditions": [                      // run before the gate; failing here prevents the run
    { "kind": "assert", "id": "server-running", "node_id": "i=2259", "expected": "0" },
    { "kind": "ensure", "id": "mode", "path": ["2:Plant", "2:Line1", "2:OperationMode"],
      "value": "1", "optional": true }    // optional: a missing node is skipped, not failed
  ],
  "gate": {
    "on":  [ /* steps that acquire the right to change the device */ ],
    "verify": { "kind": "expect", "id": "verified", "node_id": "ns=2;s=LockHeld", "expected": "true" },
    "keepalive": { "kind": "write", "id": "renew", "node_id": "ns=2;s=LockHeld", "value": "true" },
    "every_s": 1,
    "on_failure": "abort",                // or "warn"
    "off": [ /* steps that release it; always run, in finally */ ]
  },
  "steps": [ /* the body, run only after gate.verify passed */ ]
}
```

Step kinds: `write` (`node_id`/`path`, `value`, `verify`), `expect` (`expected`, `tolerance`), `assert`
(read-only, stops the run when it does not hold), `ensure` (writes only when the value differs, `optional`),
`call` (`method_node_id` or `method_name`, `arguments`), `delay` (`seconds` or `value`) and `wait_until`
(`expected`, `tolerance`, `timeout_seconds`, `poll_seconds`).

Addressing: `node_id` is an OPC UA NodeId string (`ns=2;s=Motor`); `path` is a browse path resolved from
`path_root` (default `i=85`, the Objects folder) with namespace-qualified browse names (`2:Plant`). A path with
no match, several matches or an incomplete match fails the step — the engine never guesses.

Values are always typed from the **server** (DataType, ValueRank, `InputArguments`), never from a guess: arrays
are JSON lists (`[4.0, 5.5]`), enumerations take their name or number, structures take a JSON object with every
field. A value that does not fit is refused locally and nothing is sent.

## Other example files

| File | Produced by |
| --- | --- |
| `demo-address-space.json`, `demo-address-space.csv` | `uv run python scripts/generate_example_exports.py` |
| `demo-nodeset2.xml`, `demo-nodeset2-manifest.json` | `uv run python scripts/generate_example_nodeset.py` |
| `sample-comparison*.json`, `sample-comparison*.html` | `uv run python scripts/generate_example_comparison.py` |
| `sample-test-report*.json`, `sample-test-report*.html` | `uv run python scripts/generate_example_report.py` |
