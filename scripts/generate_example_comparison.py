"""Generate the sample address-space comparison reports in ``examples/``.

    uv run python scripts/generate_example_comparison.py

Two pairs of files are produced, both from the local demo server:

* ``sample-comparison.{json,html}`` — a real comparison of two demo servers with
  the same model. It shows the "nothing differs" report: counts, namespaces and
  the configuration that was used.
* ``sample-comparison-differences.{json,html}`` — the same server compared with a
  **synthetic variant** of its snapshot in which one node changed, one node is
  missing, one node is extra and one node is on the ignore list. It exists to
  document the difference tables; the variant is built in this script and is not
  a second server.

Everything is read-only: values are not read unless a node is listed for value
comparison, and no write, method call or subscription is ever created.
"""

from __future__ import annotations

import asyncio
import copy
import socket
from pathlib import Path

from opcua_explorer.compare import ComparisonConfig, compare_address_spaces
from opcua_explorer.config import ConnectionSettings
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import AddressSpace, TraversalConfig
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.reporting import write_comparison_html, write_comparison_json

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


async def _download(endpoint: str, traversal: TraversalConfig) -> AddressSpace:
    service = OpcUaService()
    try:
        await service.connect(ConnectionSettings(endpoint_url=endpoint))
        return await service.download(["i=85"], traversal)
    finally:
        await service.shutdown()


def _variation_targets(snapshot: AddressSpace) -> tuple[str, str, str]:
    """Three deterministic variables: one changed, one removed, one ignored."""
    variables = sorted(node_id for node_id, record in snapshot.nodes.items() if record.node_class == "Variable")
    if len(variables) < 3:
        raise SystemExit("The demo model needs at least three variables for the sample.")
    return variables[0], variables[1], variables[2]


def _synthetic_variant(
    snapshot: AddressSpace,
    *,
    changed_id: str,
    removed_id: str,
    ignored_id: str,
) -> AddressSpace:
    """Copy of the candidate with the differences the sample report must show."""
    variant = copy.deepcopy(snapshot)
    variant.endpoint_url = "synthetic variant of the candidate snapshot"

    changed = variant.nodes[changed_id]
    changed.node_class = "Object"
    changed.data_type = None
    changed.access_level_names = []
    changed.user_access_level_names = []

    variant.nodes.pop(removed_id, None)

    added = copy.deepcopy(changed)
    added.node_id = "ns=2;s=OnlyOnTheCandidate"
    added.browse_name = "OnlyOnTheCandidate"
    added.display_name = "OnlyOnTheCandidate"
    added.node_class = "Variable"
    variant.nodes[added.node_id] = added

    # Expected to differ: shown as "ignored" instead of as a difference.
    ignored = variant.nodes[ignored_id]
    ignored.display_name = f"{ignored.display_name} (variant)"
    ignored.access_level_names = [*ignored.access_level_names, "CurrentWrite"]
    return variant


def _write(result, stem: str) -> None:
    json_report = write_comparison_json(result, EXAMPLES_DIR / f"{stem}.json")
    html_report = write_comparison_html(result, EXAMPLES_DIR / f"{stem}.html")
    print(result.summary_text())
    print(f"  files  : {json_report.path}, {html_report.path}")


async def main() -> int:
    traversal = TraversalConfig(max_nodes=None, batch_size=25, include_values=True)
    reference_model = await start_demo_server(port=4840 if _port_is_free(4840) else 0, update_values=False)
    candidate_model = await start_demo_server(port=0, update_values=False)
    try:
        reference = await _download(reference_model.endpoint, traversal)
        candidate = await _download(candidate_model.endpoint, traversal)
    finally:
        await stop_demo_server(candidate_model)
        await stop_demo_server(reference_model)

    print("Same model on two demo servers:")
    clean = compare_address_spaces(reference, candidate, ComparisonConfig(compare_references=True))
    _write(clean, "sample-comparison")

    changed_id, removed_id, ignored_id = _variation_targets(candidate)
    variant = _synthetic_variant(
        candidate,
        changed_id=changed_id,
        removed_id=removed_id,
        ignored_id=ignored_id,
    )
    print()
    print("Demo server against a synthetic variant (documents the difference tables):")
    different = compare_address_spaces(
        reference,
        variant,
        ComparisonConfig(
            compare_references=True,
            ignore_node_ids=frozenset({ignored_id}),
            value_node_ids=frozenset({changed_id}),
        ),
    )
    _write(different, "sample-comparison-differences")
    return 0 if clean.is_equal else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
