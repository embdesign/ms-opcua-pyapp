"""Unit tests for the on-demand condition-state read (no server)."""

from __future__ import annotations

from asyncua import ua

from opcua_explorer.models import AttributeResult
from opcua_explorer.opcua import condition_state
from opcua_explorer.opcua.browse import BrowseOutcome

CONDITION = "ns=2;s=PumpCondition"
ACKED = "ns=2;s=PumpCondition.AckedState"
CONFIRMED = "ns=2;s=PumpCondition.ConfirmedState"
ENABLED = "ns=2;s=PumpCondition.EnabledState"


def _reference(name: str, node_id: str) -> ua.ReferenceDescription:
    reference = ua.ReferenceDescription()
    reference.NodeId = ua.NodeId.from_string(node_id)
    reference.BrowseName = ua.QualifiedName(name, 0)
    reference.DisplayName = ua.LocalizedText(name)
    reference.NodeClass = ua.NodeClass.Variable
    reference.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HasComponent)
    reference.IsForward = True
    return reference


def _attribute(value) -> AttributeResult:
    return AttributeResult(attribute="Value", value=value, status="Good")


async def test_states_name_and_severity_are_read_from_the_condition(monkeypatch) -> None:
    condition_references = [
        _reference("AckedState", ACKED),
        _reference("ConfirmedState", CONFIRMED),
        _reference("EnabledState", ENABLED),
        _reference("ConditionName", "ns=2;s=PumpCondition.ConditionName"),
        _reference("LastSeverity", "ns=2;s=PumpCondition.LastSeverity"),
    ]
    id_references = {
        ACKED: [_reference("Id", "ns=2;s=PumpCondition.AckedState.Id")],
        CONFIRMED: [_reference("Id", "ns=2;s=PumpCondition.ConfirmedState.Id")],
        ENABLED: [_reference("Id", "ns=2;s=PumpCondition.EnabledState.Id")],
    }

    async def fake_browse(_session, node_ids, **_kwargs):
        ids = list(node_ids)
        if ids == [CONDITION]:
            return [BrowseOutcome(node_id=CONDITION, references=condition_references)]
        return [BrowseOutcome(node_id=node_id, references=id_references.get(node_id, [])) for node_id in ids]

    values = {
        ACKED: _attribute(ua.LocalizedText("Acknowledged")),
        "ns=2;s=PumpCondition.AckedState.Id": _attribute(True),
        CONFIRMED: _attribute(ua.LocalizedText("Unconfirmed")),
        "ns=2;s=PumpCondition.ConfirmedState.Id": _attribute(False),
        ENABLED: _attribute(ua.LocalizedText("Enabled")),
        "ns=2;s=PumpCondition.EnabledState.Id": _attribute(True),
        "ns=2;s=PumpCondition.ConditionName": _attribute("PumpCondition"),
        "ns=2;s=PumpCondition.LastSeverity": _attribute(700),
    }

    async def fake_read(_session, requests, **_kwargs):
        return {(node_id, spec.name): values[node_id] for node_id, spec in requests}

    monkeypatch.setattr(condition_state, "browse_many", fake_browse)
    monkeypatch.setattr(condition_state, "read_attributes_bulk", fake_read)

    snapshot = await condition_state.read_condition_states(object(), [CONDITION])

    assert len(snapshot.conditions) == 1
    state = snapshot.conditions[0]
    assert state.condition_name == "PumpCondition"
    assert state.acked_text() == "Acknowledged"
    assert state.acked_id is True
    assert state.confirmed_text() == "Unconfirmed"
    assert state.confirmed_id is False
    assert state.enabled_text() == "Enabled"
    assert state.enabled_id is True
    assert state.retain_text() == "not exposed"
    assert state.last_severity == 700
    assert state.missing == ["Retain"]
    assert "retain" not in snapshot.summary_text().lower()


async def test_a_missing_state_is_reported_as_not_exposed(monkeypatch) -> None:
    async def fake_browse(_session, node_ids, **_kwargs):
        ids = list(node_ids)
        if ids == [CONDITION]:
            return [BrowseOutcome(node_id=CONDITION, references=[_reference("AckedState", ACKED)])]
        return [BrowseOutcome(node_id=value, references=[]) for value in ids]

    async def fake_read(_session, requests, **_kwargs):
        return {
            (node_id, spec.name): _attribute(ua.LocalizedText("Unacknowledged"))
            for node_id, spec in requests
        }

    monkeypatch.setattr(condition_state, "browse_many", fake_browse)
    monkeypatch.setattr(condition_state, "read_attributes_bulk", fake_read)

    snapshot = await condition_state.read_condition_states(object(), [CONDITION])

    state = snapshot.conditions[0]
    assert state.acked_text() == "Unacknowledged"
    assert state.acked_id is None
    assert state.confirmed_text() == "not exposed"
    assert set(state.missing) == {"ConfirmedState", "EnabledState", "Retain"}


async def test_a_failed_browse_is_recorded_on_that_condition(monkeypatch) -> None:
    async def fake_browse(_session, node_ids, **_kwargs):
        return [BrowseOutcome(node_id=node_ids[0], status="BadNodeIdUnknown", ok=False)]

    async def fake_read(_session, requests, **_kwargs):
        return {}

    monkeypatch.setattr(condition_state, "browse_many", fake_browse)
    monkeypatch.setattr(condition_state, "read_attributes_bulk", fake_read)

    snapshot = await condition_state.read_condition_states(object(), [CONDITION])

    state = snapshot.conditions[0]
    assert state.status == "BadNodeIdUnknown"
    assert "Browsing the condition node failed" in state.detail
    assert set(state.missing) == {"AckedState", "ConfirmedState", "EnabledState", "Retain"}


async def test_no_condition_named_reads_nothing() -> None:
    snapshot = await condition_state.read_condition_states(object(), [])

    assert snapshot.conditions == []
    assert "No condition node was named" in snapshot.detail
