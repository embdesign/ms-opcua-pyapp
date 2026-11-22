"""End-to-end ``opcua-compare`` run against two real demo servers.

The demo servers expose the same model on two different ports, so a clean run
must report no differences — this covers connect, download and compare through
the real service layer.
"""

from __future__ import annotations

import asyncio
import json

from opcua_explorer import cli_compare
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server


async def test_two_demo_servers_compare_without_differences(tmp_path) -> None:
    reference = await start_demo_server(update_values=False)
    candidate = await start_demo_server(update_values=False)
    report = tmp_path / "diff.json"
    try:
        code = await asyncio.to_thread(
            cli_compare.main,
            [
                "--reference",
                reference.endpoint,
                "--candidate",
                candidate.endpoint,
                "--compare-reference-names",
                "--json",
                str(report),
                "--quiet",
            ],
        )
    finally:
        await stop_demo_server(candidate)
        await stop_demo_server(reference)

    assert code == cli_compare.EXIT_OK
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["format"] == "opcua-comparison"
    assert document["matched"] > 5
    assert document["difference_count"] == 0
    # A bounded download keeps references to nodes it did not include (types in
    # namespace 0, nodes under another root). They cannot be compared, so the
    # comparison names them as a scope warning and counts them - that is the only
    # warning two identical servers may produce.
    assert document["reference_edges_not_compared"] > 0
    assert len(document["warnings"]) == 1, document["warnings"]
    assert "outside that download" in document["warnings"][0]
