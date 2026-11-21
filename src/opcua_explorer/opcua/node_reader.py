"""Session-bound browse and read helpers.

``OpcUaService`` keeps the public methods; the node-detail/attribute reading
lives here so the façade does not also own that implementation. The collaborator
receives the session and the current namespace index lazily, because the service
has no session until it connects.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from asyncua import ua

from ..models import (
    AttributeResult,
    BrowseOptions,
    ChildEntry,
    NodeDetails,
    ReferenceInfo,
    ValueReadOptions,
    ValueReadResult,
    ValueSnapshot,
)
from .attributes import (
    AttributeSpec,
    read_attributes_bulk,
    read_node_attributes,
    read_values_bulk,
)
from .browse import browse_children as browse_children_impl
from .browse import browse_many, resolve_browse_names
from .node_ids import normalise_node_id
from .path_support import PathRequest, PathResolution, resolve_paths
from .ua_names import node_class_name
from .value_read import read_value_with_options as read_value_with_options_impl

_logger = logging.getLogger(__name__)


class NodeReader:
    """Browse and read operations on one session."""

    def __init__(
        self,
        session_provider: Callable[[], Any],
        namespace_index: Callable[[], dict[int, str]],
    ) -> None:
        self._session = session_provider
        self._namespace_index = namespace_index

    async def browse_children(
        self, node_id: Any, *, follow_subtypes: bool = True, options: BrowseOptions | None = None
    ) -> list[ChildEntry]:
        return await browse_children_impl(
            self._session(),
            normalise_node_id(node_id),
            follow_subtypes=follow_subtypes,
            options=options,
        )

    async def resolve_browse_paths(self, requests: list[PathRequest]) -> list[PathResolution]:
        """Resolve browse paths with TranslateBrowsePathsToNodeIds on the current session."""
        return await resolve_paths(self._session(), requests)

    async def read_node_details(self, node_id: Any) -> NodeDetails:
        """Read all applicable attributes plus references of one node."""
        session = self._session()
        node_id_string = normalise_node_id(node_id)

        node_class_result = await read_attributes_bulk(
            session, [(node_id_string, AttributeSpec(ua.AttributeIds.NodeClass, "NodeClass"))]
        )
        class_result = node_class_result.get((node_id_string, "NodeClass"))
        node_class = (
            str(class_result.value) if class_result and class_result.ok and class_result.value else "Unspecified"
        )

        record, attributes = await read_node_attributes(
            session,
            node_id_string,
            node_class=node_class,
            include_value=True,
            extended=True,
            namespace_uri_of=self._namespace_index().get,
        )

        references = await self._read_references(session, node_id_string, record)

        # Human-readable names for the DataType and TypeDefinition columns.
        wanted = {item for item in (record.data_type, record.type_definition) if item}
        if wanted:
            try:
                names = await resolve_browse_names(session, wanted)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - cosmetic only
                _logger.debug("Resolving type names for %s failed: %s", node_id_string, exc)
                names = {}
            if record.data_type:
                record.data_type_name = names.get(record.data_type)
            if record.type_definition:
                record.type_definition_name = names.get(record.type_definition)

        return NodeDetails(
            record=record,
            attributes=self._detail_attributes(record, attributes),
            references=references,
        )

    @staticmethod
    def _detail_attributes(record: Any, attributes: list[Any]) -> list[AttributeResult]:
        """Add values that come from browsing rather than from a Read service call."""
        leading = [
            AttributeResult(attribute="NodeId", value=record.node_id),
            AttributeResult(attribute="NamespaceUri", value=record.namespace_uri or "(not in namespace table)"),
        ]
        trailing: list[AttributeResult] = []
        if record.type_definition:
            label = record.type_definition_name or "unknown"
            trailing.append(
                AttributeResult(attribute="TypeDefinition", value=f"{label} ({record.type_definition})")
            )
        if record.data_type and record.data_type_name:
            trailing.append(AttributeResult(attribute="DataTypeName", value=record.data_type_name))
        return leading + list(attributes) + trailing

    async def _read_references(self, session: Any, node_id: str, record: Any) -> list[ReferenceInfo]:
        outcomes = await browse_many(
            session,
            [node_id],
            reference_type=ua.ObjectIds.References,
            direction=ua.BrowseDirection.Both,
            include_subtypes=True,
            batch_size=1,
        )
        if not outcomes:
            return []
        outcome = outcomes[0]
        if not outcome.ok:
            record.errors.append(f"Browsing references failed: {outcome.status}")
            return []

        reference_type_ids = {reference.ReferenceTypeId.to_string() for reference in outcome.references}
        names = await resolve_browse_names(session, reference_type_ids) if reference_type_ids else {}

        infos: list[ReferenceInfo] = []
        for reference in outcome.references:
            reference_type_id = reference.ReferenceTypeId.to_string()
            target_id = reference.NodeId.to_string()

            if reference.IsForward and reference.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasTypeDefinition):
                record.type_definition = target_id
                record.type_definition_name = (
                    reference.BrowseName.Name if isinstance(reference.BrowseName, ua.QualifiedName) else None
                )

            infos.append(
                ReferenceInfo(
                    reference_type=reference_type_id,
                    reference_type_name=names.get(reference_type_id),
                    direction="Forward" if reference.IsForward else "Inverse",
                    target_node_id=target_id,
                    target_browse_name=(
                        reference.BrowseName.Name if isinstance(reference.BrowseName, ua.QualifiedName) else ""
                    ),
                    target_display_name=(
                        reference.DisplayName.Text if isinstance(reference.DisplayName, ua.LocalizedText) else ""
                    ),
                    target_node_class=node_class_name(reference.NodeClass),
                )
            )

        infos.sort(
            key=lambda info: (
                info.direction != "Forward",
                info.reference_type_name or info.reference_type,
                info.target_display_name,
            )
        )
        return infos

    async def read_value(self, node_id: Any) -> ValueSnapshot:
        node_id_string = normalise_node_id(node_id)
        snapshots = await read_values_bulk(self._session(), [node_id_string])
        snapshot = snapshots.get(node_id_string)
        if snapshot is None:  # pragma: no cover - read_values_bulk always returns an entry
            return ValueSnapshot(value=None, status="BadNoResult", encoding="unavailable")
        return snapshot

    async def read_value_with_options(
        self, node_id: Any, options: ValueReadOptions | None = None
    ) -> ValueReadResult:
        """Read the Value attribute once with IndexRange, MaxAge and TimestampsToReturn."""
        return await read_value_with_options_impl(
            self._session(),
            normalise_node_id(node_id),
            (options or ValueReadOptions()).normalised(),
        )
