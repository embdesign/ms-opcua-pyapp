"""Address-space download: graph traversal with visited-node tracking.

OPC UA address spaces are graphs, not trees: children can be shared, and
references can point back to an ancestor. The traversal therefore

* tracks every visited NodeId (no infinite loops on cycles),
* records all references it observes, normalised to forward direction,
* descends only along forward *hierarchical* references,
* fetches children level by level so ``max_depth`` is exact,
* bounds concurrency, request batch size and total node count,
* reports limits and inaccessible nodes instead of failing silently.

The result is a snapshot of what *this session was allowed to browse*. It is not
a guarantee of a complete, independently importable information model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from asyncua import ua

from ..models import (
    AddressSpace,
    ConnectionInfo,
    Edge,
    NamespaceEntry,
    NodeRecord,
    TraversalConfig,
    TraversalProgress,
    utc_now,
)
from .attributes import (
    DECODE_ERROR_STATUS,
    VARIABLE_NODE_CLASSES,
    AttributeSpec,
    apply_attributes_to_record,
    read_attributes_bulk,
    read_values_bulk,
    specs_for,
)
from .browse import (
    BrowseOutcome,
    browse_many,
    collect_hierarchical_reference_type_ids,
    is_forward_hierarchical,
    resolve_browse_names,
    to_node_id,
)
from .ua_names import node_class_name

_logger = logging.getLogger(__name__)

ProgressCallback = Callable[[TraversalProgress], None]

LIMITATION_MODEL_NOTE = (
    "A download from a live server contains what this session was allowed to browse. "
    "It is not guaranteed to be a complete, independently importable information model."
)


@dataclass(slots=True)
class _LevelItem:
    node_id: str
    depth: int


class AddressSpaceDownloader:
    """Downloads a bounded region of an address space into an :class:`AddressSpace`."""

    def __init__(
        self,
        session: Any,
        *,
        namespaces: list[NamespaceEntry],
        config: TraversalConfig,
        progress: ProgressCallback | None = None,
        connection: ConnectionInfo | None = None,
        endpoint_url: str = "",
    ) -> None:
        self._session = session
        self._namespaces = namespaces
        self._namespace_index = {entry.index: entry.uri for entry in namespaces}
        self._config = config.normalised()
        self._progress = progress
        self._connection = connection
        self._endpoint_url = endpoint_url

        self._snapshot = AddressSpace(
            endpoint_url=endpoint_url,
            namespaces=list(namespaces),
            connection=connection,
            values_included=self._config.include_values,
            config=self._config,
        )
        self._edges: dict[tuple[str, str, str], Edge] = {}
        self._hints: dict[str, tuple[str, str]] = {}  # node id -> (node_class, display name)
        self._hierarchical_ids: frozenset[str] = frozenset()
        self._last_progress_emit = 0.0
        self._browsed = 0

    # -- public API --------------------------------------------------------
    async def download(self, roots: list[str]) -> AddressSpace:
        """Traverse from ``roots`` and return the snapshot (partial when cancelled)."""
        started = time.monotonic()
        self._snapshot.roots = list(roots)
        try:
            await self._run(roots)
        except asyncio.CancelledError:
            self._snapshot.cancelled = True
            self._snapshot.limitations.append(
                "The download was cancelled. The snapshot contains everything collected up to that point."
            )
            _logger.info(
                "Address-space download cancelled after %d node(s)", len(self._snapshot.nodes)
            )
        except Exception as exc:
            _logger.exception("Address-space download failed")
            self._snapshot.errors.append(f"{type(exc).__name__}: {exc}")
            self._snapshot.limitations.append(
                "The download stopped because of an unexpected error; the snapshot is incomplete."
            )
        finally:
            self._snapshot.references = list(self._edges.values())
            self._snapshot.finished_at = utc_now()
            self._snapshot.limitations.append(LIMITATION_MODEL_NOTE)
            self._snapshot.limitations.extend(self._value_limitations())
            _logger.info(
                "Address-space download finished: %d node(s), %d reference(s), %.2f s%s",
                len(self._snapshot.nodes),
                len(self._snapshot.references),
                time.monotonic() - started,
                " (cancelled)" if self._snapshot.cancelled else "",
            )
        return self._snapshot

    # -- internals ---------------------------------------------------------
    async def _run(self, roots: list[str]) -> None:
        self._emit(TraversalProgress(phase="preparing", message="Enumerating hierarchical reference types"))

        self._hierarchical_ids, source = await collect_hierarchical_reference_type_ids(self._session)
        if source == "fallback":
            self._snapshot.limitations.append(
                "The server's reference-type hierarchy could not be enumerated; a standard set of "
                "hierarchical reference types was used to decide which references are children."
            )

        frontier: list[_LevelItem] = [_LevelItem(root, 0) for root in roots]
        visited: set[str] = set(roots)

        while frontier:
            level, overflow = self._take_within_budget(frontier)
            if not level:
                self._note_limit("max_nodes")
                break
            if overflow:
                self._note_limit("max_nodes")

            depth = level[0].depth if level else 0
            outcomes = await self._browse_level(level)

            next_frontier: list[_LevelItem] = []
            for item, outcome in zip(level, outcomes, strict=False):
                self._browsed += 1
                self._consume_browse_result(item, outcome, next_frontier, visited)

            self._emit(
                TraversalProgress(
                    phase="browsing",
                    message=f"Browsed {len(level)} node(s) at depth {depth}",
                    nodes_collected=len(self._snapshot.nodes),
                    nodes_browsed=self._browsed,
                    nodes_pending=len(next_frontier),
                    references_found=len(self._edges),
                    depth=depth,
                )
            )

            if overflow:
                next_frontier = []
            elif next_frontier and self._depth_exhausted(depth):
                self._note_limit("max_depth")
                next_frontier = []

            frontier = next_frontier

        if not self._snapshot.nodes:
            self._snapshot.limitations.append("No nodes were collected; check access rights and the start NodeId.")
            return

        # Publish the reference list before name resolution: the resolver needs
        # to iterate it, and the incremental edge map contains the same objects.
        self._snapshot.references = list(self._edges.values())

        await self._read_all_attributes()
        await self._resolve_names()
        await self._collect_values()

        self._emit(
            TraversalProgress(
                phase="complete",
                message="Download complete",
                nodes_collected=len(self._snapshot.nodes),
                nodes_browsed=self._browsed,
                references_found=len(self._edges),
            )
        )

    def _budget_used(self) -> int:
        """Nodes already collected (the limit applies to the exported snapshot)."""
        return len(self._snapshot.nodes)

    def _budget_exhausted(self) -> bool:
        limit = self._config.max_nodes
        return limit is not None and self._budget_used() >= limit

    def _take_within_budget(self, frontier: list[_LevelItem]) -> tuple[list[_LevelItem], bool]:
        """Trim a level so that ``max_nodes`` collected nodes are never exceeded.

        Returns ``(items_to_browse, was_trimmed)``. The budget counts *collected*
        nodes; every node discovered but not yet browsed is in the frontier, so it
        is enough to compare against the remaining budget here.
        """
        limit = self._config.max_nodes
        if limit is None:
            return frontier, False
        remaining = max(0, limit - len(self._snapshot.nodes))
        if remaining >= len(frontier):
            return frontier, False
        return frontier[:remaining], True

    def _depth_exhausted(self, current_depth: int) -> bool:
        limit = self._config.max_depth
        return limit is not None and current_depth >= limit

    def _note_limit(self, name: str) -> None:
        if name not in self._snapshot.limits_hit:
            self._snapshot.limits_hit.append(name)
            _logger.info("Traversal limit reached: %s", name)

    def _namespace_allowed(self, node_id: str) -> bool:
        selected = self._config.namespace_filter
        if not selected:
            return True
        try:
            index = to_node_id(node_id).NamespaceIndex
        except Exception:  # pragma: no cover - ids originate from the library
            return True
        # Namespace 0 is always kept: the standard type system and the folder
        # structure live there, even when the user only selects their own models.
        return index == 0 or index in selected

    async def _browse_level(self, level: list[_LevelItem]) -> list[BrowseOutcome]:
        """Browse one BFS level with bounded concurrency and per-chunk error isolation."""
        batch_size = self._config.batch_size
        chunks = [level[index : index + batch_size] for index in range(0, len(level), batch_size)]
        semaphore = asyncio.Semaphore(self._config.concurrency)

        async def run_chunk(chunk: list[_LevelItem]) -> list[BrowseOutcome]:
            async with semaphore:
                try:
                    return await browse_many(
                        self._session,
                        [item.node_id for item in chunk],
                        reference_type=ua.ObjectIds.References,
                        direction=ua.BrowseDirection.Both,
                        include_subtypes=True,
                        max_references_per_node=self._config.max_references_per_node,
                        batch_size=len(chunk),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _logger.warning("Browsing %d node(s) failed: %s", len(chunk), exc)
                    return [
                        BrowseOutcome(node_id=item.node_id, status=f"BadCommunicationError ({exc})", ok=False)
                        for item in chunk
                    ]

        chunk_results = await asyncio.gather(*(run_chunk(chunk) for chunk in chunks))
        return [outcome for chunk in chunk_results for outcome in chunk]

    def _consume_browse_result(
        self,
        item: _LevelItem,
        outcome: BrowseOutcome,
        next_frontier: list[_LevelItem],
        visited: set[str],
    ) -> None:
        node_id = outcome.node_id
        record = self._snapshot.nodes.get(node_id)
        if record is None:
            record = NodeRecord(node_id=node_id)
            node_class, display_name = self._hints.get(node_id, ("Unspecified", ""))
            record.node_class = node_class
            record.display_name = display_name
            self._snapshot.nodes[node_id] = record

        with contextlib.suppress(Exception):  # defensive: ids originate from the library
            record.namespace_uri = self._namespace_index.get(to_node_id(node_id).NamespaceIndex)

        if not outcome.ok:
            self._snapshot.inaccessible[node_id] = outcome.status
            record.errors.append(f"Browse failed: {outcome.status}")
            return

        for reference in outcome.references:
            edge = self._canonical_edge(node_id, reference)
            self._edges.setdefault(edge.key, edge)

            # HasTypeDefinition is not hierarchical, but it is what populates the
            # TypeDefinition column of the export.
            if reference.IsForward and reference.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasTypeDefinition):
                record.type_definition = reference.NodeId.to_string()

            if not is_forward_hierarchical(reference, self._hierarchical_ids):
                continue

            child_id = reference.NodeId.to_string()
            self._hints.setdefault(
                child_id,
                (
                    node_class_name(reference.NodeClass),
                    reference.DisplayName.Text if isinstance(reference.DisplayName, ua.LocalizedText) else "",
                ),
            )
            if child_id in visited:
                continue
            if not self._namespace_allowed(child_id):
                continue
            if self._depth_exhausted(item.depth):
                self._note_limit("max_depth")
                continue
            if self._budget_exhausted():
                self._note_limit("max_nodes")
                continue

            visited.add(child_id)
            next_frontier.append(_LevelItem(child_id, item.depth + 1))

    def _canonical_edge(self, node_id: str, reference: ua.ReferenceDescription) -> Edge:
        target = reference.NodeId.to_string()
        reference_type = reference.ReferenceTypeId.to_string()
        if reference.IsForward:
            return Edge(source=node_id, target=target, reference_type=reference_type)
        # An inverse reference observed at `node_id` is the forward edge target->node.
        return Edge(source=target, target=node_id, reference_type=reference_type)

    async def _read_all_attributes(self) -> None:
        nodes = self._snapshot.nodes
        self._emit(
            TraversalProgress(phase="attributes", message="Reading base attributes", nodes_collected=len(nodes))
        )

        # Pass 1: attributes every NodeClass has (NodeClass decides the rest).
        base_specs = [
            AttributeSpec(ua.AttributeIds.NodeClass, "NodeClass"),
            AttributeSpec(ua.AttributeIds.BrowseName, "BrowseName"),
            AttributeSpec(ua.AttributeIds.DisplayName, "DisplayName"),
        ]
        if self._config.include_description:
            base_specs.append(AttributeSpec(ua.AttributeIds.Description, "Description"))

        requests = [(node_id, spec) for node_id in nodes for spec in base_specs]
        base_results = await read_attributes_bulk(self._session, requests)

        for node_id, record in nodes.items():
            attributes = {
                spec.name: base_results[(node_id, spec.name)]
                for spec in base_specs
                if (node_id, spec.name) in base_results
            }
            apply_attributes_to_record(record, attributes, namespace_uri_of=self._namespace_index.get)
            if not record.display_name:
                record.display_name = record.browse_name

        # Pass 2: NodeClass-specific attributes.
        self._emit(TraversalProgress(phase="attributes", message="Reading class-specific attributes"))
        class_specs: dict[str, list[Any]] = {}
        for record in nodes.values():
            if record.node_class not in class_specs:
                class_specs[record.node_class] = specs_for(record.node_class)
        type_requests = [
            (node_id, spec)
            for node_id, record in nodes.items()
            for spec in class_specs.get(record.node_class, [])
        ]
        if not type_requests:
            return
        type_results = await read_attributes_bulk(self._session, type_requests)
        for node_id, record in nodes.items():
            attributes = {
                spec.name: type_results[(node_id, spec.name)]
                for spec in class_specs.get(record.node_class, [])
                if (node_id, spec.name) in type_results
            }
            apply_attributes_to_record(record, attributes, namespace_uri_of=self._namespace_index.get)

    async def _resolve_names(self) -> None:
        """Resolve DataType, TypeDefinition and reference-type BrowseNames."""
        data_type_ids = {record.data_type for record in self._snapshot.nodes.values() if record.data_type}
        type_definition_ids = {
            record.type_definition for record in self._snapshot.nodes.values() if record.type_definition
        }
        reference_type_ids = {edge.reference_type for edge in self._snapshot.references}

        wanted = set(data_type_ids) | set(type_definition_ids) | set(reference_type_ids)
        if not wanted:
            return
        self._emit(TraversalProgress(phase="names", message="Resolving type names"))
        names = await resolve_browse_names(self._session, wanted)

        for record in self._snapshot.nodes.values():
            if record.data_type:
                record.data_type_name = names.get(record.data_type)
            if record.type_definition:
                record.type_definition_name = names.get(record.type_definition)

        for edge in self._snapshot.references:
            edge.reference_type_name = names.get(edge.reference_type)

    async def _collect_values(self) -> None:
        if not self._config.include_values:
            return
        variable_ids = [
            node_id
            for node_id, record in self._snapshot.nodes.items()
            if record.node_class in VARIABLE_NODE_CLASSES
        ]
        if not variable_ids:
            return
        self._emit(TraversalProgress(phase="values", message="Reading variable values", nodes_collected=len(variable_ids)))
        snapshots = await read_values_bulk(self._session, variable_ids)
        for node_id, snapshot in snapshots.items():
            record = self._snapshot.nodes.get(node_id)
            if record is None:
                continue
            if snapshot.encoding == "unavailable":
                record.unavailable["Value"] = snapshot.status
                continue
            record.value = snapshot
            for note in snapshot.unsupported:
                entry = dict(note)
                entry["node_id"] = node_id
                self._snapshot.unsupported_encodings.append(entry)

    def _value_limitations(self) -> list[str]:
        notes: list[str] = []
        if not self._config.include_values:
            notes.append(
                "Variable values were excluded (the 'include values' option is off), so no Value, "
                "SourceTimestamp or ServerTimestamp is part of this export."
            )
        elif self._snapshot.unsupported_encodings:
            notes.append(
                f"{len(self._snapshot.unsupported_encodings)} value encoding(s) could not be represented "
                "faithfully and are flagged in the export."
            )
        if self._snapshot.inaccessible:
            notes.append(
                f"{len(self._snapshot.inaccessible)} node(s) could not be browsed (access denied, timeout or "
                "disconnect); they are listed separately."
            )
        undecodable = self._undecodable_value_ids()
        if undecodable:
            notes.append(
                f"{len(undecodable)} value(s) could not be decoded by this client: the server returned data the "
                "client's decoders reject (for example an enumeration value outside the OPC UA specification). "
                "Only those values are missing - the rest of the download is unaffected."
            )
        if self._snapshot.limits_hit:
            notes.append(
                "Traversal limits stopped the download: " + ", ".join(self._snapshot.limits_hit) + "."
            )
        return notes

    def _undecodable_value_ids(self) -> list[str]:
        """Nodes whose Value the client could not decode (see ``read_values_bulk``)."""
        return [
            node_id
            for node_id, record in self._snapshot.nodes.items()
            if str(record.unavailable.get("Value", "")).startswith(DECODE_ERROR_STATUS)
        ]

    def _emit(self, progress: TraversalProgress) -> None:
        if self._progress is None:
            return
        now = time.monotonic()
        # Throttle: one update per 100 ms, always delivering terminal messages.
        if progress.phase in ("preparing", "complete", "values", "names") or now - self._last_progress_emit >= 0.1:
            self._last_progress_emit = now
            try:
                self._progress(progress)
            except Exception:  # pragma: no cover - a GUI callback must not break the download
                _logger.debug("Progress callback failed", exc_info=True)


async def download_address_space(
    session: Any,
    roots: list[str],
    *,
    namespaces: list[NamespaceEntry],
    config: TraversalConfig,
    progress: ProgressCallback | None = None,
    connection: ConnectionInfo | None = None,
    endpoint_url: str = "",
) -> AddressSpace:
    """Convenience entry point used by the client service."""
    downloader = AddressSpaceDownloader(
        session,
        namespaces=namespaces,
        config=config,
        progress=progress,
        connection=connection,
        endpoint_url=endpoint_url,
    )
    return await downloader.download(roots)
