"""Browse primitives with explicit continuation-point handling.

Why this module exists
----------------------
``Node.get_children_descriptions`` in asyncua 2.1.0 follows continuation points
internally but never releases them (``ReleaseContinuationPoints = False`` in its
``_browse_next`` loop), and ``RequestedMaxReferencesPerNode`` is not
configurable. Requirements in this project demand both: bounded batch sizes and
best-effort release of continuation points, including after cancellation.

The functions below therefore talk to ``UaSession.browse`` / ``browse_next``
directly (verified signatures: ``browse(BrowseParameters) -> list[BrowseResult]``
and ``browse_next(BrowseNextParameters) -> list[BrowseResult]``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from asyncua import ua

from ..errors import InvalidNodeIdError
from ..models import ChildEntry
from .ua_names import node_class_name, status_name

_logger = logging.getLogger(__name__)

#: Subtypes of HierarchicalReferences used when the server type tree cannot be
#: enumerated (defensive fallback, not the primary mechanism).
HIERARCHICAL_FALLBACK_IDS: frozenset[int] = frozenset(
    {
        ua.ObjectIds.HierarchicalReferences,
        ua.ObjectIds.HasChild,
        ua.ObjectIds.Organizes,
        ua.ObjectIds.HasComponent,
        ua.ObjectIds.HasProperty,
        ua.ObjectIds.HasSubtype,
    }
)

MAX_REFERENCE_TYPE_DEPTH = 32
MAX_REFERENCE_TYPES = 500


@dataclass(slots=True)
class BrowseOutcome:
    """Browse result for one node including its per-node status code."""

    node_id: str
    status: str = "Good"
    ok: bool = True
    references: list[ua.ReferenceDescription] = field(default_factory=list)


def to_node_id(value: Any) -> ua.NodeId:
    """Convert a NodeId, string or int into an asyncua NodeId."""
    if isinstance(value, ua.NodeId):
        return value
    if isinstance(value, int):
        return ua.NodeId(value)
    if isinstance(value, str):
        return ua.NodeId.from_string(value)
    raise InvalidNodeIdError(f"Cannot interpret {type(value).__name__} as a NodeId")


def node_id_to_string(value: Any) -> str:
    return to_node_id(value).to_string()


def parse_node_id(text: str) -> ua.NodeId:
    """Parse user input such as ``ns=2;i=85`` or ``ns=1;s=MyVar``."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise InvalidNodeIdError("NodeId must not be empty.")
    try:
        return ua.NodeId.from_string(cleaned)
    except Exception as exc:
        raise InvalidNodeIdError(
            f"{cleaned!r} is not a valid NodeId. Expected forms like 'ns=2;i=42', "
            "'ns=2;s=Temperature' or 'i=85'."
        ) from exc


async def _release_continuation_points(session: Any, points: Iterable[bytes]) -> bool:
    """Best-effort BrowseNext(ReleaseContinuationPoints=True)."""
    remaining = [point for point in points if point]
    if not remaining:
        return True
    try:
        params = ua.BrowseNextParameters()
        params.ReleaseContinuationPoints = True
        params.ContinuationPoints = remaining
        await session.browse_next(params)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _logger.debug("Could not release %d continuation point(s): %s", len(remaining), exc)
        return False


async def browse_many(
    session: Any,
    node_ids: Iterable[Any],
    *,
    reference_type: Any = ua.ObjectIds.References,
    direction: ua.BrowseDirection = ua.BrowseDirection.Both,
    include_subtypes: bool = True,
    node_class_mask: int = 0,
    max_references_per_node: int = 0,
    result_mask: ua.BrowseResultMask = ua.BrowseResultMask.All,
    batch_size: int = 50,
    release_on_cancel: bool = True,
) -> list[BrowseOutcome]:
    """Browse several nodes, following and releasing continuation points.

    ``max_references_per_node`` maps to ``RequestedMaxReferencesPerNode``; ``0``
    lets the server decide. When more references exist, the remaining ones are
    fetched through ``BrowseNext`` in bounded batches.

    On cancellation, outstanding continuation points are released before the
    ``CancelledError`` is re-raised.
    """
    identifiers = [node_id_to_string(node_id) for node_id in node_ids]
    outcomes: list[BrowseOutcome] = []
    size = max(1, int(batch_size))
    for start in range(0, len(identifiers), size):
        chunk = identifiers[start : start + size]
        outcomes.extend(
            await _browse_batch(
                session,
                chunk,
                reference_type=reference_type,
                direction=direction,
                include_subtypes=include_subtypes,
                node_class_mask=node_class_mask,
                max_references_per_node=max_references_per_node,
                result_mask=result_mask,
                release_on_cancel=release_on_cancel,
            )
        )
    return outcomes


async def _browse_batch(
    session: Any,
    node_ids: list[str],
    *,
    reference_type: Any,
    direction: ua.BrowseDirection,
    include_subtypes: bool,
    node_class_mask: int,
    max_references_per_node: int,
    result_mask: ua.BrowseResultMask,
    release_on_cancel: bool,
) -> list[BrowseOutcome]:
    params = ua.BrowseParameters()
    params.View.Timestamp = ua.get_win_epoch()
    params.RequestedMaxReferencesPerNode = int(max_references_per_node)
    reference_type_id = to_node_id(reference_type)

    for node_id in node_ids:
        description = ua.BrowseDescription()
        description.NodeId = to_node_id(node_id)
        description.BrowseDirection = direction
        description.ReferenceTypeId = reference_type_id
        description.IncludeSubtypes = include_subtypes
        description.NodeClassMask = node_class_mask
        description.ResultMask = result_mask
        params.NodesToBrowse.append(description)

    results = await session.browse(params)
    if len(results) != len(node_ids):
        _logger.warning(
            "Browse returned %d result(s) for %d node(s); missing results are marked failed",
            len(results),
            len(node_ids),
        )

    outcomes: list[BrowseOutcome] = []
    continuation: list[tuple[int, bytes]] = []
    for index, node_id in enumerate(node_ids):
        if index < len(results):
            result = results[index]
            status = status_name(result.StatusCode)
            outcome = BrowseOutcome(
                node_id=node_id,
                status=status,
                ok=bool(result.StatusCode.is_good()),
                references=list(result.References),
            )
            if result.ContinuationPoint:
                continuation.append((index, result.ContinuationPoint))
        else:
            outcome = BrowseOutcome(node_id=node_id, status="BadNoResult", ok=False)
        outcomes.append(outcome)

    try:
        while continuation:
            next_params = ua.BrowseNextParameters()
            next_params.ReleaseContinuationPoints = False
            next_params.ContinuationPoints = [point for _, point in continuation]
            next_results = await session.browse_next(next_params)

            still_open: list[tuple[int, bytes]] = []
            for (index, _previous), next_result in zip(continuation, next_results, strict=False):
                outcomes[index].references.extend(next_result.References)
                if next_result.ContinuationPoint:
                    still_open.append((index, next_result.ContinuationPoint))
            continuation = still_open
    except asyncio.CancelledError:
        if release_on_cancel:
            await _release_continuation_points(session, [point for _, point in continuation])
        raise

    return outcomes


async def collect_hierarchical_reference_type_ids(
    session: Any,
    *,
    max_types: int = MAX_REFERENCE_TYPES,
    batch_size: int = 50,
) -> tuple[frozenset[str], str]:
    """Enumerate the subtype tree below ``HierarchicalReferences``.

    Returns ``(ids, source)`` where ``source`` is ``"server"`` when the type tree
    was enumerated and ``"fallback"`` when a static set had to be used.

    This makes child classification correct for server-defined reference types
    that are proper subtypes of HierarchicalReferences, instead of guessing from
    browse names.
    """
    discovered: set[str] = {node_id_to_string(ua.ObjectIds.HierarchicalReferences)}
    frontier = [ua.ObjectIds.HierarchicalReferences]

    try:
        for _ in range(MAX_REFERENCE_TYPE_DEPTH):
            if not frontier or len(discovered) >= max_types:
                break
            outcomes = await browse_many(
                session,
                frontier,
                reference_type=ua.ObjectIds.HasSubtype,
                direction=ua.BrowseDirection.Forward,
                include_subtypes=False,
                batch_size=batch_size,
            )
            next_frontier: list[str] = []
            for outcome in outcomes:
                for reference in outcome.references:
                    identifier = reference.NodeId.to_string()
                    if identifier not in discovered:
                        discovered.add(identifier)
                        next_frontier.append(identifier)
                        if len(discovered) >= max_types:
                            break
            frontier = next_frontier
        return frozenset(discovered), "server"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _logger.info("Could not enumerate hierarchical reference types (%s); using fallback set", exc)
        fallback = frozenset(node_id_to_string(identifier) for identifier in HIERARCHICAL_FALLBACK_IDS)
        return fallback, "fallback"


async def resolve_browse_names(session: Any, node_ids: Iterable[Any], *, chunk_size: int = 400) -> dict[str, str]:
    """Resolve BrowseName for a set of NodeIds with batched Read requests.

    Used for type-definition and data-type names, and for reference-type names.
    Failures are skipped silently: display names are cosmetic.
    """
    unique = list(dict.fromkeys(node_id_to_string(node_id) for node_id in node_ids))
    names: dict[str, str] = {}
    for start in range(0, len(unique), chunk_size):
        chunk = unique[start : start + chunk_size]
        params = ua.ReadParameters()
        for node_id in chunk:
            read_value_id = ua.ReadValueId()
            read_value_id.NodeId = to_node_id(node_id)
            read_value_id.AttributeId = ua.AttributeIds.BrowseName
            params.NodesToRead.append(read_value_id)
        try:
            results = await session.read(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.debug("BrowseName resolution failed for a batch: %s", exc)
            continue
        for node_id, data_value in zip(chunk, results, strict=False):
            if not data_value.StatusCode.is_good() or data_value.Value is None:
                continue
            value = data_value.Value.Value
            if isinstance(value, ua.QualifiedName):
                names[node_id] = value.Name
    return names


def reference_to_child(
    reference: ua.ReferenceDescription,
    *,
    reference_type_name: str | None = None,
    type_definition_name: str | None = None,
) -> ChildEntry:
    """Convert a ReferenceDescription into a GUI tree child."""
    browse_name = reference.BrowseName
    return ChildEntry(
        node_id=reference.NodeId.to_string(),
        browse_name=browse_name.Name if isinstance(browse_name, ua.QualifiedName) else str(browse_name),
        browse_name_namespace_index=int(browse_name.NamespaceIndex) if isinstance(browse_name, ua.QualifiedName) else 0,
        display_name=reference.DisplayName.Text if isinstance(reference.DisplayName, ua.LocalizedText) else "",
        node_class=node_class_name(reference.NodeClass),
        reference_type=reference.ReferenceTypeId.to_string(),
        reference_type_name=reference_type_name,
        is_forward=bool(reference.IsForward),
        type_definition=reference.TypeDefinition.to_string() if reference.TypeDefinition else None,
        type_definition_name=type_definition_name,
    )


async def browse_children(
    session: Any,
    node_id: Any,
    *,
    follow_subtypes: bool = True,
    batch_size: int = 50,
) -> list[ChildEntry]:
    """Children for the lazy GUI tree (hierarchical references, forward only).

    Exactly one entry per target NodeId is returned: when a server references the
    same child from this parent more than once, the extra references are merged
    (they stay visible in the node details' reference table).
    """
    outcomes = await browse_many(
        session,
        [node_id],
        reference_type=ua.ObjectIds.HierarchicalReferences,
        direction=ua.BrowseDirection.Forward,
        include_subtypes=follow_subtypes,
        batch_size=batch_size,
    )
    if not outcomes:
        return []
    outcome = outcomes[0]
    if not outcome.ok:
        raise RuntimeError(f"Browsing {outcome.node_id} failed with {outcome.status}")

    reference_type_ids = {reference.ReferenceTypeId.to_string() for reference in outcome.references}
    names = await resolve_browse_names(session, reference_type_ids) if reference_type_ids else {}
    children = [
        reference_to_child(reference, reference_type_name=names.get(reference.ReferenceTypeId.to_string()))
        for reference in outcome.references
    ]
    children = _dedupe_children(children)
    children.sort(key=lambda child: (child.node_class != "Object", child.display_name.lower(), child.node_id))
    return children


def _dedupe_children(children: list[ChildEntry]) -> list[ChildEntry]:
    """Keep one tree row per target NodeId.

    A server may reference the same target from one parent through several
    hierarchical reference types (for example ``Organizes`` *and*
    ``HasComponent``), or even return the identical reference twice. The tree is
    a navigation aid, not a reference dump - the details panel shows every
    reference - so each NodeId appears exactly once. The first reference in
    server order wins.

    A *different* BrowseName for the same NodeId is a server inconsistency, not
    a duplicate: the second name is dropped from the tree but logged. Reference
    types that were merged away are kept on the surviving entry so the GUI can
    explain the merge in a tooltip.
    """
    unique: dict[str, ChildEntry] = {}
    for child in children:
        existing = unique.get(child.node_id)
        if existing is None:
            unique[child.node_id] = child
            continue
        if child.display_name and child.display_name != existing.display_name:
            _logger.warning(
                "Server returned NodeId %s twice under the same parent with different names (%r, %r); "
                "the tree keeps the first one",
                child.node_id,
                existing.display_name,
                child.display_name,
            )
        else:
            _logger.debug(
                "Ignoring duplicate %s reference to %s from the same parent",
                child.reference_type_name or child.reference_type,
                child.node_id,
            )
        merged_label = child.reference_type_name or child.reference_type
        primary_label = existing.reference_type_name or existing.reference_type
        if merged_label and merged_label != primary_label and merged_label not in existing.additional_reference_types:
            existing.additional_reference_types.append(merged_label)
    return list(unique.values())


def is_forward_hierarchical(reference: ua.ReferenceDescription, hierarchical_ids: frozenset[str]) -> bool:
    """True when the reference points from the browsed node to a child."""
    if not reference.IsForward:
        return False
    return reference.ReferenceTypeId.to_string() in hierarchical_ids
