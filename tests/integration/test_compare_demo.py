"""Comparing two real downloads: the demo server against itself.

The unit tests build snapshots by hand. This one uses actual ``Read``/``Browse``
results from two independent sessions to the same server, so it covers what the
exchange produces: resolved reference names, namespace tables and browse paths.
"""

from __future__ import annotations

from opcua_explorer.compare import ComparisonConfig, compare_address_spaces
from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.models import TraversalConfig
from opcua_explorer.opcua.client_service import OpcUaService


async def test_two_downloads_of_the_same_server_are_equal(service, demo_model, tmp_path) -> None:
    config = TraversalConfig(max_nodes=None, batch_size=25)
    reference = await service.download(["i=85"], config)

    second = OpcUaService(trust_store=TrustStore(tmp_path / "second-trust.json"))
    await second.connect(ConnectionSettings(endpoint_url=demo_model.endpoint))
    try:
        candidate = await second.download(["i=85"], config)
    finally:
        await second.shutdown()

    result = compare_address_spaces(
        reference,
        candidate,
        ComparisonConfig(compare_references=True),
    )

    assert result.matched > 5, result.summary_text()
    assert result.missing_in_candidate == []
    assert result.extra_in_candidate == []
    assert result.changed == []
    assert result.not_comparable == []
    assert result.reference_differences == []
    assert result.namespace_differences == []
    assert result.warnings == [], result.summary_text()
