"""Address-space graph, node records and download configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..defaults import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_NODES,
    MAX_TRAVERSAL_BATCH_SIZE,
    MAX_TRAVERSAL_CONCURRENCY,
)
from .common import ConnectionInfo, NamespaceEntry, iso_utc, utc_now


@dataclass(slots=True)
class ValueSnapshot:
    """A variable value with its status and timestamps.

    ``encoding`` is ``"json"`` for successfully serialized values,
    ``"unavailable"`` when the server returned a bad StatusCode, and
    ``"unsupported"`` when parts of the value could not be represented
    faithfully (details are kept in ``unsupported``).
    """

    value: Any
    status: str = "Good"
    variant_type: str | None = None
    source_timestamp: str | None = None
    server_timestamp: str | None = None
    encoding: str = "json"
    unsupported: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "variant_type": self.variant_type,
            "status": self.status,
            "source_timestamp": self.source_timestamp,
            "server_timestamp": self.server_timestamp,
            "encoding": self.encoding,
            "unsupported_encodings": list(self.unsupported),
        }

    @property
    def unsupported_encoding(self) -> bool:
        return bool(self.unsupported)


#: Browse directions offered by the address-space filter.
BROWSE_DIRECTIONS = ("Forward", "Inverse", "Both")

#: Reference types offered by the address-space filter as ``(label, NodeId)``.
BROWSE_REFERENCE_TYPES = (
    ("Hierarchical references", "i=33"),
    ("Organizes", "i=35"),
    ("HasComponent", "i=47"),
    ("HasProperty", "i=46"),
    ("HasChild", "i=34"),
    ("All references", "i=31"),
)

#: ``NodeClassMask`` values (OPC UA Part 4) as ``(label, mask)``.
BROWSE_NODE_CLASSES = (
    ("All node classes", 0),
    ("Object", 1),
    ("Variable", 2),
    ("Method", 4),
    ("ObjectType", 8),
    ("VariableType", 16),
    ("ReferenceType", 32),
    ("DataType", 64),
    ("View", 128),
)


@dataclass(slots=True)
class BrowseOptions:
    """Filters for one hierarchical browse of the address-space tree.

    Defaults reproduce the tree's normal behaviour: forward references of
    ``HierarchicalReferences`` including subtypes, every node class. The server
    decides how strictly it applies them; the tree says what was asked for.
    """

    reference_type: str = "i=33"
    include_subtypes: bool = True
    direction: str = "Forward"
    node_class_mask: int = 0

    def normalised(self) -> BrowseOptions:
        direction = self.direction if self.direction in BROWSE_DIRECTIONS else "Forward"
        mask = int(self.node_class_mask)
        return BrowseOptions(
            reference_type=(self.reference_type or "i=33").strip(),
            include_subtypes=bool(self.include_subtypes),
            direction=direction,
            node_class_mask=max(0, mask),
        )

    def is_default(self) -> bool:
        return self.normalised() == BrowseOptions()

    def label(self) -> str:
        references = {node_id: name for name, node_id in BROWSE_REFERENCE_TYPES}
        name = references.get(self.reference_type, self.reference_type)
        parts = [name, self.direction]
        if not self.include_subtypes:
            parts.append("no subtypes")
        classes = {mask: name for name, mask in BROWSE_NODE_CLASSES}
        if self.node_class_mask:
            parts.append(classes.get(self.node_class_mask, f"mask {self.node_class_mask}"))
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_type": self.reference_type,
            "include_subtypes": self.include_subtypes,
            "direction": self.direction,
            "node_class_mask": self.node_class_mask,
        }


@dataclass(slots=True)
class NodeRecord:
    """Canonical representation of one address-space node."""

    node_id: str
    node_class: str = "Unspecified"
    browse_name: str = ""
    browse_name_namespace_index: int = 0
    display_name: str = ""
    namespace_uri: str | None = None
    description: str | None = None
    data_type: str | None = None
    data_type_name: str | None = None
    value_rank: int | None = None
    array_dimensions: list[int] | None = None
    access_level: int | None = None
    access_level_names: list[str] = field(default_factory=list)
    user_access_level: int | None = None
    user_access_level_names: list[str] = field(default_factory=list)
    type_definition: str | None = None
    type_definition_name: str | None = None
    value: ValueSnapshot | None = None
    #: attribute name -> OPC UA StatusCode name for attributes the server refused
    unavailable: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_class": self.node_class,
            "browse_name": self.browse_name,
            "browse_name_namespace_index": self.browse_name_namespace_index,
            "display_name": self.display_name,
            "namespace_uri": self.namespace_uri,
            "description": self.description,
            "data_type": self.data_type,
            "data_type_name": self.data_type_name,
            "value_rank": self.value_rank,
            "array_dimensions": self.array_dimensions,
            "access_level": self.access_level,
            "access_level_names": list(self.access_level_names),
            "user_access_level": self.user_access_level,
            "user_access_level_names": list(self.user_access_level_names),
            "type_definition": self.type_definition,
            "type_definition_name": self.type_definition_name,
            "value": None if self.value is None else self.value.to_dict(),
            "unavailable_attributes": dict(self.unavailable),
            "errors": list(self.errors),
        }

    @property
    def has_value(self) -> bool:
        return self.value is not None

    @property
    def is_writable(self) -> bool:
        return "CurrentWrite" in self.access_level_names


@dataclass(slots=True)
class Edge:
    """A directed reference, normalised to its forward direction.

    An inverse reference ``A --(InverseOf)--> B`` observed while browsing ``A``
    is stored as the forward edge ``B -> A`` so the same relationship discovered
    from both ends collapses into one row. ``reference_type_name`` is resolved
    after the traversal completes, which is why this dataclass is not frozen.
    """

    source: str
    target: str
    reference_type: str
    reference_type_name: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.reference_type)


@dataclass(slots=True)
class ChildEntry:
    """One child as shown by the lazy GUI tree."""

    node_id: str
    browse_name: str
    browse_name_namespace_index: int
    display_name: str
    node_class: str
    reference_type: str
    reference_type_name: str | None = None
    is_forward: bool = True
    type_definition: str | None = None
    type_definition_name: str | None = None
    #: Extra reference types that pointed at the same node and were merged into
    #: this row, so the tree can explain why the server reported it more than
    #: once (for example ``Organizes`` next to ``HasComponent``).
    additional_reference_types: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReferenceInfo:
    """One reference row of the node details panel."""

    reference_type: str
    reference_type_name: str | None
    direction: str
    target_node_id: str
    target_browse_name: str
    target_display_name: str
    target_node_class: str


@dataclass(slots=True)
class AttributeResult:
    """One attribute of one node, already decoded for display.

    ``value`` stays native (NodeId, QualifiedName, int, bool, ...) so a caller can
    compare it; ``display`` optionally carries a human-readable rendering such as
    the decoded names of a bit field (``"1 (SubscribeToEvents)"``).
    """

    attribute: str
    value: Any = None
    status: str = "Good"
    ok: bool = True
    display: str | None = None


@dataclass(slots=True)
class NodeDetails:
    """Everything the browser shows for a selected node."""

    record: NodeRecord
    attributes: list[AttributeResult] = field(default_factory=list)
    references: list[ReferenceInfo] = field(default_factory=list)


@dataclass(slots=True)
class TraversalConfig:
    """Bounds for an address-space download.

    All limits are optional; ``None`` means "no limit". Every limit is reported
    back in the export summary when it stopped the traversal.
    """

    max_depth: int | None = None
    max_nodes: int | None = DEFAULT_MAX_NODES
    concurrency: int = DEFAULT_CONCURRENCY
    batch_size: int = DEFAULT_BATCH_SIZE
    max_references_per_node: int = 0
    namespace_filter: frozenset[int] | None = None
    include_values: bool = False
    include_description: bool = True
    follow_subtypes: bool = True

    def normalised(self) -> TraversalConfig:
        return TraversalConfig(
            max_depth=None if self.max_depth is None or self.max_depth < 0 else self.max_depth,
            max_nodes=None if self.max_nodes is None or self.max_nodes <= 0 else self.max_nodes,
            concurrency=max(1, min(int(self.concurrency), MAX_TRAVERSAL_CONCURRENCY)),
            batch_size=max(1, min(int(self.batch_size), MAX_TRAVERSAL_BATCH_SIZE)),
            max_references_per_node=max(0, int(self.max_references_per_node)),
            namespace_filter=self.namespace_filter,
            include_values=bool(self.include_values),
            include_description=bool(self.include_description),
            follow_subtypes=bool(self.follow_subtypes),
        )


@dataclass(slots=True)
class TraversalProgress:
    """Progress notification emitted from the asyncio thread."""

    phase: str
    message: str = ""
    nodes_collected: int = 0
    nodes_browsed: int = 0
    nodes_pending: int = 0
    references_found: int = 0
    depth: int = 0


#: Standard OPC UA attributes read only for the Address space details panel
#: (``opcua/attributes.py``), never during a download, so no snapshot export carries
#: them. Kept as text here because the model layer must not import the asyncua-based reader.
DETAILS_ONLY_ATTRIBUTES: tuple[str, ...] = (
    "EventNotifier",
    "WriteMask",
    "UserWriteMask",
    "Historizing",
    "MinimumSamplingInterval",
    "IsAbstract",
    "Symmetric",
    "InverseName",
    "ContainsNoLoops",
    "DataTypeDefinition",
    "AccessRestrictions",
    "RolePermissions",
    "UserRolePermissions",
    "Executable",
    "UserExecutable",
)


@dataclass(slots=True)
class AddressSpace:
    """A downloaded (possibly partial) address-space snapshot."""

    endpoint_url: str
    roots: list[str] = field(default_factory=list)
    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    references: list[Edge] = field(default_factory=list)
    namespaces: list[NamespaceEntry] = field(default_factory=list)
    connection: ConnectionInfo | None = None
    values_included: bool = False
    config: TraversalConfig = field(default_factory=TraversalConfig)
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    cancelled: bool = False
    limits_hit: list[str] = field(default_factory=list)
    inaccessible: dict[str, str] = field(default_factory=dict)
    unsupported_encodings: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def namespace_uri(self, index: int) -> str | None:
        for entry in self.namespaces:
            if entry.index == index:
                return entry.uri
        return None

    def summary(self) -> dict[str, Any]:
        classes: dict[str, int] = {}
        for record in self.nodes.values():
            classes[record.node_class] = classes.get(record.node_class, 0) + 1
        included = set(self.nodes)
        dangling = sum(1 for edge in self.references if edge.target not in included)
        return {
            "nodes": len(self.nodes),
            "references": len(self.references),
            "references_to_nodes_outside_snapshot": dangling,
            "namespaces_recorded": len(self.namespaces),
            "node_classes": dict(sorted(classes.items())),
            "values_included": self.values_included,
            "cancelled": self.cancelled,
            "limits_hit": list(self.limits_hit),
            "inaccessible_nodes": len(self.inaccessible),
            "unsupported_value_encodings": len(self.unsupported_encodings),
            "errors": len(self.errors),
            "duration_seconds": None if self.duration_seconds is None else round(self.duration_seconds, 3),
            "started_at_utc": iso_utc(self.started_at),
            "finished_at_utc": iso_utc(self.finished_at),
        }

    def summary_text(self) -> str:
        """Human-readable summary used by the export tab."""
        summary = self.summary()
        classes = ", ".join(f"{name}={count}" for name, count in summary["node_classes"].items())
        lines = [
            f"Endpoint:        {self.endpoint_url}",
            f"Nodes:           {summary['nodes']}",
            f"References:      {summary['references']} "
            f"({summary['references_to_nodes_outside_snapshot']} point outside the snapshot)",
            f"Node classes:    {classes or '-'}",
            f"Values included: {'yes' if self.values_included else 'no'}",
            f"Duration:        {summary['duration_seconds']} s",
        ]
        if self.cancelled:
            lines.append("State:           CANCELLED (partial snapshot)")
        if self.limits_hit:
            lines.append(f"Limits hit:      {', '.join(self.limits_hit)}")
        if self.inaccessible:
            lines.append(f"Inaccessible:    {len(self.inaccessible)} node(s)")
        if self.unsupported_encodings:
            lines.append(f"Unsupported:     {len(self.unsupported_encodings)} value encoding(s)")
        if self.errors:
            lines.append(f"Errors:          {len(self.errors)}")
            for message in self.errors[:5]:
                lines.append(f"  - {message}")
        for note in self.limitations:
            lines.append(f"Note:            {note}")
        return "\n".join(lines)

    def coverage_text(self) -> str:
        """What this snapshot contains and what no snapshot export can contain.

        The snapshot is the authoritative record: ``config`` holds the options the
        *download* used, which may differ from the options the Export tab shows now.
        """
        included = ["base attributes and references"]
        excluded: list[str] = []
        if self.config.include_values:
            included.append("variable values (Value, SourceTimestamp, ServerTimestamp)")
        else:
            excluded.append("variable values (Value, SourceTimestamp, ServerTimestamp)")
        if self.config.include_description:
            included.append("node descriptions")
        else:
            excluded.append("node descriptions")

        lines = [
            f"Nodes:          {len(self.nodes)}",
            f"References:     {len(self.references)}",
            f"Included:       {', '.join(included)}",
        ]
        if excluded:
            lines.append(f"NOT included:   {', '.join(excluded)}")
        lines.append(
            "Never in a snapshot (details panel only): " + ", ".join(DETAILS_ONLY_ATTRIBUTES) + "."
        )
        if self.cancelled:
            lines.append("State:          CANCELLED (partial snapshot)")
        if self.limits_hit:
            lines.append(f"Limits hit:     {', '.join(self.limits_hit)} (partial snapshot)")
        if self.inaccessible:
            lines.append(f"Inaccessible:   {len(self.inaccessible)} node(s) could not be browsed")
        refused = sum(len(record.unavailable) for record in self.nodes.values())
        if refused:
            lines.append(f"Refused attrs:  {refused} attribute read(s) returned a bad StatusCode")
        if self.unsupported_encodings:
            lines.append(
                f"Unsupported:    {len(self.unsupported_encodings)} value encoding(s) flagged as approximate"
            )
        if self.errors:
            lines.append(f"Errors:         {len(self.errors)}")
        return "\n".join(lines)
