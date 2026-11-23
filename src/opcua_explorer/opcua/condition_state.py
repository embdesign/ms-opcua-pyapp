"""On-demand read of an A&C condition object's own state variables.

This module is a **read**, not a condition model: it browses one condition node
for the standard state components (``AckedState``, ``ConfirmedState``,
``EnabledState``, ``Retain``), reads their Value in one bounded batch, and reads
the Boolean ``Id`` property of each TwoState/Condition variable when the server
exposes it. Nothing is inferred from an event, nothing is cached between reads,
and no method is called.

A condition that does not expose a state (a plain ConditionType has no
``AckedState``) is reported as *not exposed* - never as ``False``. The client
never invents the NodeId of a state variable: the browse names come from the
A&C information model, and a missing one stays missing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from asyncua import ua

from ..models import (
    CONDITION_STATE_PROPERTIES,
    CONDITION_SUMMARY_PROPERTIES,
    ConditionState,
    ConditionStateSnapshot,
    utc_now,
)
from .attributes import AttributeSpec, format_attribute_value, read_attributes_bulk
from .browse import browse_many, node_id_to_string

_logger = logging.getLogger(__name__)

_VALUE_SPEC = AttributeSpec(ua.AttributeIds.Value, "Value")


def _browse_name(reference: ua.ReferenceDescription) -> tuple[int, str]:
    name = reference.BrowseName
    if isinstance(name, ua.QualifiedName):
        return int(name.NamespaceIndex or 0), name.Name or ""
    return 0, str(name)


def _node_key(reference: ua.ReferenceDescription) -> str:
    return reference.NodeId.to_string()


async def _children_by_name(
    session: Any, node_ids: list[str]
) -> dict[str, tuple[dict[str, str], Any]]:
    """Browse children of each node; map standard browse name -> NodeId string.

    The browse outcome is kept next to the names so a refused browse is reported
    as a failed condition instead of looking like "no state variables".
    """
    if not node_ids:
        return {}
    outcomes = await browse_many(
        session,
        node_ids,
        reference_type=ua.ObjectIds.HierarchicalReferences,
        direction=ua.BrowseDirection.Forward,
        include_subtypes=True,
        batch_size=50,
    )
    by_node: dict[str, tuple[dict[str, str], Any]] = {}
    for outcome in outcomes:
        names: dict[str, str] = {}
        if outcome.ok:
            for reference in outcome.references:
                namespace, name = _browse_name(reference)
                # Standard A&C state/summary variables live in namespace 0; a
                # same-named custom node must not be mistaken for one of them.
                if namespace == 0 and name:
                    names.setdefault(name, _node_key(reference))
        by_node[outcome.node_id] = (names, outcome)
    return by_node


async def _id_nodes(session: Any, state_ids: list[str]) -> dict[str, str]:
    """Map a state variable NodeId to the NodeId of its ``Id`` property."""
    if not state_ids:
        return {}
    outcomes = await browse_many(
        session,
        state_ids,
        reference_type=ua.ObjectIds.HasProperty,
        direction=ua.BrowseDirection.Forward,
        include_subtypes=True,
        batch_size=50,
    )
    ids: dict[str, str] = {}
    for outcome in outcomes:
        if not outcome.ok:
            continue
        for reference in outcome.references:
            namespace, name = _browse_name(reference)
            if namespace == 0 and name == "Id":
                ids[outcome.node_id] = _node_key(reference)
                break
    return ids


async def read_condition_states(session: Any, condition_node_ids: list[Any]) -> ConditionStateSnapshot:
    """Read the state variables of the given condition objects, once.

    One browse per condition, one browse per state variable that has to be
    inspected for its ``Id``, and one batched Read for every Value. A browse
    refusal is recorded on that condition and does not stop the others.
    """
    node_ids = list(dict.fromkeys(node_id_to_string(node_id) for node_id in condition_node_ids))
    started = time.perf_counter()
    if not node_ids:
        return ConditionStateSnapshot(
            detail="No condition node was named.", duration_ms=0.0, received_at=utc_now()
        )

    by_node = await _children_by_name(session, node_ids)

    state_nodes: dict[str, dict[str, str]] = {}
    summary_nodes: dict[str, dict[str, str]] = {}
    for node_id in node_ids:
        names = by_node.get(node_id, ({}, None))[0]
        state_nodes[node_id] = {
            name: names[name] for name in CONDITION_STATE_PROPERTIES if name in names
        }
        summary_nodes[node_id] = {
            name: names[name] for name in CONDITION_SUMMARY_PROPERTIES if name in names
        }

    flat_state_ids = list(
        dict.fromkeys(state_id for states in state_nodes.values() for state_id in states.values())
    )
    id_nodes = await _id_nodes(session, flat_state_ids)

    read_ids: list[str] = []
    for states in state_nodes.values():
        read_ids.extend(states.values())
    for names in summary_nodes.values():
        read_ids.extend(names.values())
    read_ids.extend(id_nodes.values())
    read_ids = list(dict.fromkeys(read_ids))

    attributes = (
        await read_attributes_bulk(session, [(node_id, _VALUE_SPEC) for node_id in read_ids])
        if read_ids
        else {}
    )

    def read_value(node_id: str) -> Any:
        result = attributes.get((node_id, "Value"))
        return result if result is not None and result.ok else None

    conditions: list[ConditionState] = []
    for node_id in node_ids:
        entry = by_node.get(node_id)
        outcome = entry[1] if entry is not None else None
        if outcome is None or not outcome.ok:
            conditions.append(
                ConditionState(
                    node_id=node_id,
                    missing=list(CONDITION_STATE_PROPERTIES),
                    status=(
                        getattr(outcome, "status", "Failed") if outcome is not None else "Failed"
                    ),
                    detail="Browsing the condition node failed.",
                )
            )
            continue

        state = ConditionState(node_id=node_id)
        for name in CONDITION_STATE_PROPERTIES:
            variable_id = state_nodes[node_id].get(name)
            if variable_id is None:
                state.missing.append(name)
                continue
            value = read_value(variable_id)
            text = format_attribute_value(value.value) if value is not None else ""
            identifier = None
            id_node = id_nodes.get(variable_id)
            if id_node is not None:
                id_result = read_value(id_node)
                if id_result is not None and isinstance(id_result.value, bool):
                    identifier = bool(id_result.value)
            if name == "AckedState":
                state.acked_state, state.acked_id = text, identifier
            elif name == "ConfirmedState":
                state.confirmed_state, state.confirmed_id = text, identifier
            elif name == "EnabledState":
                state.enabled_state, state.enabled_id = text, identifier
            elif name == "Retain":
                if value is not None and isinstance(value.value, bool):
                    state.retain = bool(value.value)
                elif text:
                    state.retain = text.strip().lower() in ("true", "1")

        name_node = summary_nodes[node_id].get("ConditionName")
        if name_node is not None:
            result = read_value(name_node)
            if result is not None:
                state.condition_name = format_attribute_value(result.value)
        severity_node = summary_nodes[node_id].get("LastSeverity")
        if severity_node is not None:
            result = read_value(severity_node)
            if result is not None:
                try:
                    state.last_severity = int(result.value)
                except (TypeError, ValueError):
                    state.last_severity = None

        unreadable = [
            name
            for name in CONDITION_STATE_PROPERTIES
            if name not in state.missing
            and read_value(state_nodes[node_id][name]) is None
        ]
        if unreadable:
            state.detail = "Unreadable state variable(s): " + ", ".join(unreadable) + "."
        conditions.append(state)

    duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    detail = ""
    if not conditions:
        detail = "No condition object was read."
    return ConditionStateSnapshot(
        conditions=conditions,
        detail=detail,
        duration_ms=duration_ms,
        received_at=utc_now(),
    )


__all__ = ["read_condition_states"]
