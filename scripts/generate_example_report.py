"""Generate the sample functional-test reports in ``examples/``.

    uv run python scripts/generate_example_report.py

Two pairs of reports are produced against the local demo server:

* ``sample-test-report.{json,html}`` - the default **read-only** suite. The demo
  changes its Temperature value every second, so the notification check observes a
  real data change.
* ``sample-test-report-guarded.{json,html}`` - the same suite plus **one confirmed
  write** (with the previous value restored) and **one confirmed method call**, so
  the report format for guarded operations is documented by example.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

from opcua_explorer.config import ConnectionSettings
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import MethodCallTarget, TestConfig, WriteTarget
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.reporting import write_html_report, write_json_report

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _print_run(title: str, run, paths: list[Path]) -> int:
    summary = run.summary()
    print(title)
    print(
        f"  result : {summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['skipped']} skipped ({summary['duration_seconds']} s)"
    )
    for check in run.checks:
        print(f"    {check.status:<8} {check.id:<26} {check.detail[:70]}")
    for path in paths:
        print(f"  file   : {path}")
    return summary["failed"]


async def _read_only_examples() -> int:
    port = 4840 if _port_is_free(4840) else 0
    model = await start_demo_server(port=port, update_values=True)
    service = OpcUaService()
    try:
        await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
        run = await service.run_functional_tests(
            TestConfig(
                variable_node_id=model.node_ids["temperature"],
                notification_wait_seconds=6.0,
                latency_samples=10,
            )
        )
        json_path = EXAMPLES_DIR / "sample-test-report.json"
        html_path = EXAMPLES_DIR / "sample-test-report.html"
        write_json_report(run, json_path)
        write_html_report(run, html_path)
        return _print_run(f"read-only suite ({model.endpoint})", run, [json_path, html_path])
    finally:
        await service.shutdown()
        await stop_demo_server(model)


async def _guarded_examples() -> int:
    # Values are not updated here, so restoring the previous value is meaningful.
    model = await start_demo_server(port=0, update_values=False)
    service = OpcUaService()
    try:
        await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
        run = await service.run_functional_tests(
            TestConfig(
                variable_node_id=model.node_ids["temperature"],
                notification_wait_seconds=2.0,
                latency_samples=5,
                allow_writes=True,
                write_targets=[
                    WriteTarget(
                        node_id=model.node_ids["temperature"],
                        value_text="42.5",
                        restore_previous_value=True,
                        note="example: written once, previous value restored",
                    )
                ],
                method_calls=[
                    MethodCallTarget(
                        object_node_id=model.node_ids["motor"],
                        method_node_id=model.node_ids["start_method"],
                        arguments=["900"],
                        note="example: demo method with one Double argument",
                    )
                ],
            )
        )
        json_path = EXAMPLES_DIR / "sample-test-report-guarded.json"
        html_path = EXAMPLES_DIR / "sample-test-report-guarded.html"
        write_json_report(run, json_path)
        write_html_report(run, html_path)
        return _print_run(f"guarded write + method call ({model.endpoint})", run, [json_path, html_path])
    finally:
        await service.shutdown()
        await stop_demo_server(model)


async def generate() -> int:
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    failures = await _read_only_examples()
    failures += await _guarded_examples()
    return 0 if failures == 0 else 1


def main() -> int:
    return asyncio.run(generate())


if __name__ == "__main__":
    sys.exit(main())
