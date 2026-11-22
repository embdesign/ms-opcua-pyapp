"""The traversal must honour the bounds it documents.

The regression class this covers: a documented ``TraversalConfig`` field that the
downloader ignored. ``follow_subtypes`` was serialised into every export and
restored by the loader while ``_browse_level`` hardcoded ``include_subtypes=True``,
so a snapshot could claim a bound that never shaped a request.
"""

from __future__ import annotations

from typing import Any

import pytest

from opcua_explorer.models import TraversalConfig
from opcua_explorer.opcua import traversal as traversal_module
from opcua_explorer.opcua.browse import BrowseOutcome
from opcua_explorer.opcua.traversal import AddressSpaceDownloader


class _RecordingBrowse:
    """Stand-in for ``browse_many`` that records the bounds it was called with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, node_ids: list[str], **kwargs: Any) -> list[BrowseOutcome]:
        self.calls.append({"node_ids": list(node_ids), **kwargs})
        return [BrowseOutcome(node_id=node_id) for node_id in node_ids]


def _downloader(config: TraversalConfig) -> AddressSpaceDownloader:
    return AddressSpaceDownloader(session=None, namespaces=[], config=config)


@pytest.mark.parametrize("follow_subtypes", [True, False])
async def test_the_follow_subtypes_bound_reaches_the_browse_request(
    monkeypatch: pytest.MonkeyPatch, follow_subtypes: bool
) -> None:
    recorder = _RecordingBrowse()
    monkeypatch.setattr(traversal_module, "browse_many", recorder)
    downloader = _downloader(TraversalConfig(follow_subtypes=follow_subtypes, batch_size=2))

    level = [traversal_module._LevelItem(node_id=f"ns=2;i={index}", depth=1) for index in (1, 2, 3)]
    outcomes = await downloader._browse_level(level)

    assert [outcome.node_id for outcome in outcomes] == ["ns=2;i=1", "ns=2;i=2", "ns=2;i=3"]
    assert recorder.calls, "the level was never browsed"
    # One call per batch, each carrying the configured bound instead of a hardcoded True.
    assert [len(call["node_ids"]) for call in recorder.calls] == [2, 1]
    assert {call["include_subtypes"] for call in recorder.calls} == {follow_subtypes}


async def test_the_device_attribute_pass_runs_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extra attribute reads are opt-in: they multiply the volume of a download."""
    from opcua_explorer.models import NodeRecord
    from opcua_explorer.opcua.attributes import AttributeResult

    recorded: list[list[tuple[str, str]]] = []

    async def fake_read(session: Any, requests: list[Any]) -> dict[tuple[str, str], AttributeResult]:
        recorded.append([(node_id, spec.name) for node_id, spec in requests])
        return {}

    monkeypatch.setattr(traversal_module, "read_attributes_bulk", fake_read)

    plain = _downloader(TraversalConfig())
    plain._snapshot.nodes["ns=2;i=1"] = NodeRecord(node_id="ns=2;i=1", node_class="Variable")
    await plain._read_device_attributes()
    assert recorded == []

    with_device = _downloader(TraversalConfig(include_device_attributes=True))
    with_device._snapshot.nodes["ns=2;i=1"] = NodeRecord(node_id="ns=2;i=1", node_class="Variable")
    with_device._snapshot.nodes["ns=2;i=2"] = NodeRecord(node_id="ns=2;i=2", node_class="Object")
    await with_device._read_device_attributes()

    assert len(recorded) == 1
    per_node = [name for node_id, name in recorded[0] if node_id == "ns=2;i=1"]
    assert "Historizing" in per_node and "WriteMask" in per_node
    object_attrs = [name for node_id, name in recorded[0] if node_id == "ns=2;i=2"]
    assert "EventNotifier" in object_attrs and "Historizing" not in object_attrs


async def test_a_progress_note_names_the_device_attribute_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator sees why a device-attribute download takes longer."""
    from opcua_explorer.models import NodeRecord

    async def fake_read(session: Any, requests: list[Any]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(traversal_module, "read_attributes_bulk", fake_read)
    downloader = _downloader(TraversalConfig(include_device_attributes=True))
    downloader._snapshot.nodes["ns=2;i=1"] = NodeRecord(node_id="ns=2;i=1", node_class="Variable")
    phases: list[str] = []
    downloader._progress = lambda update: phases.append(update.phase)  # type: ignore[method-assign]

    await downloader._read_device_attributes()

    assert "device attributes" in phases
