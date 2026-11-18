"""NodeSet2 XML export with explicit dependency collection.

The brief requires that we do **not** assume an XML exporter browses the server by
itself: the node set, its type/reference dependencies and its parent chain are
collected first, then handed to asyncua's ``XmlExporter``.

What is collected
-----------------
* the nodes of the downloaded snapshot (the model to export),
* their **parent chain** (a NodeSet2 node whose ``ParentNodeId`` is neither in the
  file nor in namespace 0 cannot be imported - verified against asyncua's
  importer),
* **custom type definitions** (``HasTypeDefinition``) and **supertypes**
  (``HasSubtype`` inverse),
* the **DataType** of variables when it is a custom type,
* ``HasEncoding`` nodes of custom structures and members of custom type nodes.

Dependencies in namespace 0 are *recorded but not written*: a NodeSet2 file
references the standard type system of the target server instead of redefining it.

What is reported instead of silently dropped
--------------------------------------------
* dependencies that could not be read (access denied, unknown node, timeout),
* dependencies skipped because the budget was exhausted,
* custom DataTypes without a ``DataTypeDefinition`` (asyncua's exporter cannot
  write their structure, and importers cannot reconstruct it) - they are excluded
  from the file and the variables typed by them are listed,
* references pointing outside the written set (allowed, but may be broken after
  import).

Validation
----------
Always: XML well-formedness plus structural checks (parent present or standard,
namespace indexes declared). Optionally: XSD validation against a schema file when
``lxml`` is installed and a schema path is supplied. The official NodeSet2 schema
is not redistributed with this project.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asyncua import ua
from asyncua.common.xmlexporter import XmlExporter

from ..defaults import DEFAULT_MAX_DEPENDENCIES, DEFAULT_MAX_NODES
from ..errors import ExportError
from ..models import AddressSpace, TraversalProgress, iso_utc, utc_now
from .attributes import AttributeSpec, read_attributes_bulk, status_name
from .browse import browse_many, collect_hierarchical_reference_type_ids
from .nodeset2_validation import (
    ValidationResult,
    _inspect_nodeset,
    _structural_validation,
    _validate_schema,
)

_logger = logging.getLogger(__name__)

ProgressCallback = Callable[[TraversalProgress], None]

_NS0 = "http://opcfoundation.org/UA/"
#: Reference types are compared as NodeId strings ("i=40"), not numeric ids.
_HAS_TYPE_DEFINITION = ua.NodeId(ua.ObjectIds.HasTypeDefinition).to_string()
_HAS_SUBTYPE = ua.NodeId(ua.ObjectIds.HasSubtype).to_string()
_HAS_PROPERTY = ua.NodeId(ua.ObjectIds.HasProperty).to_string()
_HAS_COMPONENT = ua.NodeId(ua.ObjectIds.HasComponent).to_string()
_HAS_ENCODING = ua.NodeId(ua.ObjectIds.HasEncoding).to_string()

_TYPE_NODE_CLASSES = frozenset({"ObjectType", "VariableType", "DataType", "ReferenceType"})


@dataclass(slots=True)
class NodeSet2Config:
    """Options for one NodeSet2 export."""

    include_values: bool = False
    include_dependencies: bool = True
    max_dependency_nodes: int = DEFAULT_MAX_DEPENDENCIES
    pretty: bool = True
    schema_path: str | None = None

    def normalised(self) -> NodeSet2Config:
        return NodeSet2Config(
            include_values=bool(self.include_values),
            include_dependencies=bool(self.include_dependencies),
            max_dependency_nodes=min(DEFAULT_MAX_NODES, max(0, int(self.max_dependency_nodes))),
            pretty=bool(self.pretty),
            schema_path=(self.schema_path or "").strip() or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "include_values": self.include_values,
            "include_dependencies": self.include_dependencies,
            "max_dependency_nodes": self.max_dependency_nodes,
            "pretty": self.pretty,
            "schema_path": self.schema_path,
        }


@dataclass(slots=True)
class NodeSet2Result:
    path: Path
    bytes_written: int = 0
    nodes_written: int = 0
    node_ids_written: list[str] = field(default_factory=list)
    dependency_nodes_added: int = 0
    namespaces: list[dict[str, Any]] = field(default_factory=list)
    standard_dependencies: list[str] = field(default_factory=list)
    missing_dependencies: list[dict[str, Any]] = field(default_factory=list)
    unsupported_data_types: list[dict[str, Any]] = field(default_factory=list)
    argument_values_written: int = 0
    dangling_references: list[dict[str, Any]] = field(default_factory=list)
    validation: ValidationResult = field(default_factory=ValidationResult)
    limitations: list[str] = field(default_factory=list)
    config: NodeSet2Config = field(default_factory=NodeSet2Config)
    generated_at_utc: str = field(default_factory=lambda: iso_utc(utc_now()))
    duration_seconds: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "nodes_written": self.nodes_written,
            "dependency_nodes_added": self.dependency_nodes_added,
            "standard_dependencies": len(self.standard_dependencies),
            "missing_dependencies": len(self.missing_dependencies),
            "unsupported_data_types": len(self.unsupported_data_types),
            "argument_values_written": self.argument_values_written,
            "dangling_references": len(self.dangling_references),
            "validation": self.validation.status,
            "schema_validation": self.validation.schema_status,
            "bytes_written": self.bytes_written,
            "duration_seconds": round(self.duration_seconds, 3),
        }

    def summary_text(self) -> str:
        summary = self.summary()
        lines = [
            f"NodeSet2 XML:   {self.path}",
            f"Nodes written:  {summary['nodes_written']} "
            f"(+{summary['dependency_nodes_added']} dependency node(s))",
            f"Namespaces:     {len(self.namespaces)} mapped in the file",
            f"Standard deps:  {summary['standard_dependencies']} namespace-0 node(s) referenced, not written",
            f"Validation:     {summary['validation']} (schema: {summary['schema_validation']})",
            f"Duration:       {summary['duration_seconds']} s",
        ]
        if self.argument_values_written:
            lines.insert(
                4,
                f"Method args:    {self.argument_values_written} argument property value(s) included",
            )
        if self.missing_dependencies:
            lines.append(f"Missing deps:   {len(self.missing_dependencies)} (see below)")
            for entry in self.missing_dependencies[:5]:
                lines.append(f"  - {entry.get('node_id')}: {entry.get('reason')}")
        if self.unsupported_data_types:
            lines.append(f"DataTypes:      {len(self.unsupported_data_types)} custom DataType(s) not written")
            for entry in self.unsupported_data_types[:5]:
                lines.append(f"  - {entry.get('node_id')}: {entry.get('reason')}")
        if self.dangling_references:
            lines.append(f"References:     {len(self.dangling_references)} point outside the file")
        for note in self.limitations:
            lines.append(f"Note:           {note}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "opcua-nodeset2-export",
            "format_version": 1,
            "generated_at_utc": self.generated_at_utc,
            "path": str(self.path),
            "configuration": self.config.to_dict(),
            "nodes_written": self.nodes_written,
            "node_ids_written": list(self.node_ids_written),
            "dependency_nodes_added": self.dependency_nodes_added,
            "namespaces": list(self.namespaces),
            "standard_dependencies": list(self.standard_dependencies),
            "missing_dependencies": list(self.missing_dependencies),
            "unsupported_data_types": list(self.unsupported_data_types),
            "argument_values_written": self.argument_values_written,
            "dangling_references": list(self.dangling_references),
            "validation": self.validation.to_dict(),
            "limitations": list(self.limitations),
            "summary": self.summary(),
        }


@dataclass(slots=True)
class _NodeMeta:
    node_id: str
    node_class: str
    browse_name: str = ""
    data_type: str | None = None
    in_snapshot: bool = False

    @property
    def namespace_index(self) -> int:
        try:
            return ua.NodeId.from_string(self.node_id).NamespaceIndex
        except Exception:  # pragma: no cover - ids come from the library
            return 0

    @property
    def is_type_node(self) -> bool:
        return self.node_class in _TYPE_NODE_CLASSES


class NodeSetDependencyCollector:
    """Determines which nodes must be written and which dependencies are missing."""

    def __init__(
        self,
        client: Any,
        session: Any,
        snapshot: AddressSpace,
        config: NodeSet2Config,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self._client = client
        self._session = session
        self._snapshot = snapshot
        self._config = config
        self._progress = progress

        self._meta: dict[str, _NodeMeta] = {}
        self._out: dict[str, list[Any]] = {}
        self._in: dict[str, list[Any]] = {}
        self._standard: dict[str, None] = {}
        self._missing: list[dict[str, Any]] = []
        self._reasons: dict[str, str] = {}
        self._hierarchical: frozenset[str] = frozenset()
        self._dependency_budget_exhausted = False
        self._added_nodes: set[str] = set()
        self._skipped_views: list[str] = []

        for record in snapshot.nodes.values():
            self._add_meta(
                _NodeMeta(
                    node_id=record.node_id,
                    node_class=record.node_class,
                    browse_name=record.browse_name,
                    data_type=record.data_type,
                    in_snapshot=True,
                )
            )
        for edge in snapshot.references:
            self._add_edge(edge.source, edge.target, edge.reference_type)

    # -- graph bookkeeping -------------------------------------------------
    def _add_meta(self, meta: _NodeMeta) -> None:
        existing = self._meta.get(meta.node_id)
        if existing is None or (not existing.in_snapshot and meta.in_snapshot):
            self._meta[meta.node_id] = meta

    def _add_edge(self, source: str, target: str, reference_type: str) -> None:
        forward = self._out.setdefault(source, [])
        if not any(edge.target == target and edge.reference_type == reference_type for edge in forward):
            forward.append(_Edge(source, target, reference_type))
        backward = self._in.setdefault(target, [])
        if not any(edge.source == source and edge.reference_type == reference_type for edge in backward):
            backward.append(_Edge(source, target, reference_type))

    def _dependencies_of(self, node_id: str) -> list[tuple[str, str]]:
        meta = self._meta.get(node_id)
        dependencies: list[tuple[str, str]] = []
        for edge in self._out.get(node_id, []):
            if edge.reference_type == _HAS_TYPE_DEFINITION:
                dependencies.append((edge.target, "HasTypeDefinition"))
            elif edge.reference_type == _HAS_ENCODING:
                dependencies.append((edge.target, "HasEncoding"))
            elif meta is not None and (meta.is_type_node or meta.node_class == "Method") and (
                edge.reference_type in (_HAS_PROPERTY, _HAS_COMPONENT)
            ):
                # Type members define the type; method arguments (InputArguments /
                # OutputArguments) are required for the method to be callable.
                dependencies.append((edge.target, "method argument" if meta.node_class == "Method" else "type member"))
        for edge in self._in.get(node_id, []):
            if edge.reference_type == _HAS_SUBTYPE:
                dependencies.append((edge.source, "supertype"))
            elif edge.reference_type in self._hierarchical:
                dependencies.append((edge.source, "parent"))
        if meta is not None and meta.data_type:
            dependencies.append((meta.data_type, "DataType"))
        return dependencies

    # -- collection --------------------------------------------------------
    def _emit(self, message: str, current: int, total: int) -> None:
        if self._progress is None:
            return
        try:
            self._progress(TraversalProgress(phase="dependencies", message=message, nodes_collected=current, nodes_browsed=total))
        except Exception:  # pragma: no cover - GUI callback must not break the export
            _logger.debug("NodeSet2 progress callback failed", exc_info=True)

    async def collect(self) -> list[str]:
        """Resolve dependencies with a bounded, batched breadth-first search."""
        self._hierarchical, _source = await collect_hierarchical_reference_type_ids(self._session)

        pending: deque[str] = deque(self._meta)
        requested: set[str] = set()
        inspected = 0
        batch_size = 50

        while pending:
            batch = [pending.popleft() for _ in range(min(batch_size, len(pending)))]
            wanted: dict[str, str] = {}
            for node_id in batch:
                inspected += 1
                for dependency, reason in self._dependencies_of(node_id):
                    try:
                        namespace_index = ua.NodeId.from_string(dependency).NamespaceIndex
                    except Exception:  # pragma: no cover - library-provided ids
                        continue
                    if namespace_index == 0:
                        self._standard.setdefault(dependency, None)
                        continue
                    if dependency == node_id or dependency in self._meta or dependency in requested:
                        continue
                    wanted.setdefault(dependency, reason)

            if not wanted:
                continue
            if not self._config.include_dependencies:
                for dependency, reason in wanted.items():
                    self._missing.append(
                        {
                            "node_id": dependency,
                            "reason": "dependency collection disabled",
                            "needed_by": self._reasons.get(dependency, "the exported model"),
                            "relationship": reason,
                        }
                    )
                continue

            to_load: list[str] = []
            for dependency, reason in wanted.items():
                if len(self._added_nodes) + len(to_load) >= self._config.max_dependency_nodes:
                    self._dependency_budget_exhausted = True
                    self._missing.append(
                        {
                            "node_id": dependency,
                            "reason": "not collected: dependency budget reached",
                            "needed_by": self._reasons.get(dependency, "the exported model"),
                            "relationship": reason,
                        }
                    )
                    continue
                to_load.append(dependency)
                self._reasons.setdefault(dependency, reason)

            if not to_load:
                continue
            requested.update(to_load)
            self._emit(
                f"Resolving {len(to_load)} dependency node(s)",
                len(self._meta),
                len(self._meta) + len(to_load),
            )
            loaded = await self._load_nodes(to_load)
            self._added_nodes.update(loaded)
            pending.extend(loaded)

        self._emit("Dependency collection finished", len(self._meta), inspected)
        return self._select_for_writing()

    async def _load_nodes(self, node_ids: list[str]) -> list[str]:
        """Browse unknown dependency nodes and read their class/DataType."""
        outcomes = await browse_many(
            self._session,
            node_ids,
            reference_type=ua.ObjectIds.References,
            direction=ua.BrowseDirection.Both,
            include_subtypes=True,
            batch_size=len(node_ids),
        )
        attributes = await read_attributes_bulk(
            self._session,
            [(node_id, spec) for node_id in node_ids for spec in _DEPENDENCY_ATTRIBUTES],
            chunk_size=300,
        )
        loaded: list[str] = []
        for node_id, outcome in zip(node_ids, outcomes, strict=False):
            class_result = attributes.get((node_id, "NodeClass"))
            name_result = attributes.get((node_id, "BrowseName"))
            data_type_result = attributes.get((node_id, "DataType"))
            if class_result is None or not class_result.ok:
                self._missing.append(
                    {
                        "node_id": node_id,
                        "reason": f"NodeClass read returned {class_result.status if class_result else 'no result'}",
                        "needed_by": self._reasons.get(node_id, "dependency"),
                        "relationship": self._reasons.get(node_id, "dependency"),
                    }
                )
                continue
            meta = _NodeMeta(
                node_id=node_id,
                node_class=str(class_result.value),
                browse_name=_qualified_name(name_result.value) if name_result and name_result.ok else "",
                data_type=(
                    data_type_result.value.to_string()
                    if data_type_result is not None and data_type_result.ok and isinstance(data_type_result.value, ua.NodeId)
                    else None
                ),
            )
            self._add_meta(meta)
            loaded.append(node_id)

            if not outcome.ok:
                self._missing.append(
                    {
                        "node_id": node_id,
                        "reason": f"Browse returned {outcome.status}",
                        "needed_by": self._reasons.get(node_id, "dependency"),
                        "relationship": self._reasons.get(node_id, "dependency"),
                    }
                )
                continue
            for reference in outcome.references:
                target = reference.NodeId.to_string()
                reference_type = reference.ReferenceTypeId.to_string()
                if reference.IsForward:
                    self._add_edge(node_id, target, reference_type)
                else:
                    self._add_edge(target, node_id, reference_type)
        return loaded

    # -- emission ----------------------------------------------------------
    def _select_for_writing(self) -> list[str]:
        selected: list[str] = []
        for node_id, meta in self._meta.items():
            namespace_index = meta.namespace_index
            if namespace_index == 0:
                self._standard.setdefault(node_id, None)
                continue
            if meta.node_class == "View":
                # asyncua's XML exporter has no View writer; record the node so the
                # result can report it instead of dropping it silently.
                self._skipped_views.append(node_id)
                continue
            selected.append(node_id)
        return sorted(selected)

    async def find_unsupported_data_types(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Custom DataTypes that cannot be represented (no DataTypeDefinition)."""
        data_types = [
            node_id
            for node_id in node_ids
            if (meta := self._meta.get(node_id)) is not None and meta.node_class == "DataType"
        ]
        if not data_types:
            return []

        unsupported: list[dict[str, Any]] = []
        for node_id in data_types:
            node = self._client.get_node(node_id)
            value = await node.read_attribute(ua.AttributeIds.DataTypeDefinition, raise_on_bad_status=False)
            definition = value.Value.Value if value is not None and value.Value is not None else None
            if definition:
                continue
            status = status_name(value.StatusCode) if value is not None else "BadNoResult"
            unsupported.append(
                {
                    "node_id": node_id,
                    "name": self._meta[node_id].browse_name,
                    "reason": "no DataTypeDefinition attribute"
                    if status == "Good"
                    else f"DataTypeDefinition not readable ({status})",
                }
            )
        return unsupported

    def affected_variables(self, data_type_ids: list[str]) -> list[dict[str, str]]:
        affected = []
        for node_id, meta in self._meta.items():
            if meta.data_type in data_type_ids:
                affected.append({"node_id": node_id, "data_type": meta.data_type or ""})
        return sorted(affected, key=lambda item: item["node_id"])

    def type_members(self, root_ids: list[str]) -> set[str]:
        """Members reachable through HasProperty/HasComponent from the given nodes."""
        members: set[str] = set()
        queue: deque[str] = deque(root_ids)
        while queue:
            node_id = queue.popleft()
            for edge in self._out.get(node_id, []):
                if edge.reference_type not in (_HAS_PROPERTY, _HAS_COMPONENT):
                    continue
                if edge.target in members or edge.target == node_id:
                    continue
                members.add(edge.target)
                queue.append(edge.target)
        return members

    def argument_property_nodes(self, node_ids: list[str]) -> list[str]:
        """Method argument properties (InputArguments / OutputArguments)."""
        result: list[str] = []
        for node_id in node_ids:
            meta = self._meta.get(node_id)
            if meta is None or meta.node_class != "Variable":
                continue
            if meta.browse_name in ("InputArguments", "OutputArguments"):
                result.append(node_id)
        return result

    # -- reporting ---------------------------------------------------------
    @property
    def standard_dependencies(self) -> list[str]:
        return sorted(self._standard)

    @property
    def missing_dependencies(self) -> list[dict[str, Any]]:
        return list(self._missing)

    @property
    def budget_exhausted(self) -> bool:
        return self._dependency_budget_exhausted

    @property
    def meta(self) -> dict[str, _NodeMeta]:
        return self._meta

    @property
    def dependency_nodes_added(self) -> int:
        return len(self._added_nodes)

    @property
    def skipped_views(self) -> list[str]:
        """View nodes the XML exporter cannot write (reported, never silent)."""
        return list(self._skipped_views)


@dataclass(frozen=True, slots=True)
class _Edge:
    """One canonical (forward) reference between two node ids."""

    source: str
    target: str
    reference_type: str


_DEPENDENCY_ATTRIBUTES: tuple[AttributeSpec, ...] = (
    AttributeSpec(ua.AttributeIds.NodeClass, "NodeClass"),
    AttributeSpec(ua.AttributeIds.BrowseName, "BrowseName"),
    AttributeSpec(ua.AttributeIds.DataType, "DataType"),
)


def _qualified_name(value: Any) -> str:
    if isinstance(value, ua.QualifiedName):
        return value.Name or ""
    return str(value or "")


def _exporter_limitations(written_classes: set[str], skipped_views: list[str]) -> list[str]:
    """Name what the underlying XML exporter cannot write, so nothing is lost silently.

    ``asyncua.common.xmlexporter`` has no writer for ``WriteMask``/``UserWriteMask``
    (an upstream FIXME), for the access/role attributes, for ``Symmetric`` and
    ``IsAbstract`` on a custom ``ReferenceType`` or ``IsAbstract`` on a custom
    ``DataType``, and for the ``View`` node class.
    """
    notes = [
        "WriteMask and UserWriteMask are not written for any node: the XML exporter does not export them, "
        "so importers apply their own defaults.",
        "AccessRestrictions, RolePermissions and UserRolePermissions are not written for any node: the XML "
        "exporter does not export them.",
    ]
    if "ReferenceType" in written_classes:
        notes.append(
            "Symmetric and IsAbstract are not written for custom ReferenceType nodes: the XML exporter writes "
            "only the inverse name, so a symmetric or abstract reference type loses that flag on import."
        )
    if "DataType" in written_classes:
        notes.append(
            "IsAbstract is not written for custom DataType nodes: the XML exporter writes only the definition."
        )
    if skipped_views:
        notes.append(
            f"{len(skipped_views)} View node(s) were not written: the XML exporter has no View node writer. "
            "A child exported below a View keeps a ParentNodeId that is not part of the file."
        )
    return notes


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def _inject_argument_values(client: Any, exporter: Any, node_ids: list[str]) -> tuple[int, list[str]]:
    """Write InputArguments/OutputArguments values even when value export is disabled.

    A method without its argument metadata is declared but not callable, so these
    values are treated as part of the reference dependencies rather than as
    optional data. A second exporter reuses the namespace mapping of the main file
    so the produced ``<Value>`` element can be transplanted by NodeId.
    """
    if not node_ids:
        return 0, []

    written = 0
    problems: list[str] = []
    helper = XmlExporter(client, export_values=True)
    helper.aliases = exporter.aliases
    helper._addr_idx_to_xml_idx = dict(exporter._addr_idx_to_xml_idx)  # noqa: SLF001 - pinned API
    root = exporter.etree.getroot()
    by_node_id: dict[str, Any] = {}
    for element in root:
        node_id_attr = element.attrib.get("NodeId")
        if node_id_attr:
            by_node_id[node_id_attr] = element

    for node_id in node_ids:
        try:
            helper_root = helper.etree.getroot()
            before = len(helper_root)
            await helper.node_to_etree(client.get_node(node_id))
            if len(helper_root) <= before:
                problems.append(f"{node_id}: exporter produced no element")
                continue
            generated = helper_root[-1]
            value = generated.find("Value")
            target = by_node_id.get(generated.attrib.get("NodeId", ""))
            if value is None or target is None:
                problems.append(f"{node_id}: no value could be generated")
                continue
            if target.find("Value") is None:
                target.append(value)
                written += 1
        except Exception as exc:  # pragma: no cover - depends on server data
            problems.append(f"{node_id}: {type(exc).__name__}: {exc}")
    return written, problems


async def export_nodeset2(
    client: Any,
    snapshot: AddressSpace,
    path: str | Path,
    config: NodeSet2Config,
    *,
    session: Any | None = None,
    progress: ProgressCallback | None = None,
) -> NodeSet2Result:
    """Collect nodes + dependencies from the live session and write NodeSet2 XML."""
    started = time.perf_counter()
    settings = config.normalised()
    target = Path(path)
    if not snapshot.nodes:
        raise ExportError("The snapshot is empty. Download the address space before exporting NodeSet2.")

    collector = NodeSetDependencyCollector(
        client,
        session if session is not None else client.uaclient.session,
        snapshot,
        settings,
        progress=progress,
    )
    emit_ids = await collector.collect()

    custom_data_types = [
        node_id
        for node_id, meta in collector.meta.items()
        if meta.node_class == "DataType" and meta.namespace_index != 0
    ]
    unsupported = await collector.find_unsupported_data_types(custom_data_types)
    if unsupported:
        unsupported_ids = {entry["node_id"] for entry in unsupported}
        affected: dict[str, list[str]] = {}
        for item in collector.affected_variables(sorted(unsupported_ids)):
            affected.setdefault(item["data_type"], []).append(item["node_id"])
        # Members of an unsupported DataType cannot be written either: their parent
        # would be missing and importers reject the file (BadParentNodeIdInvalid).
        excluded = set(unsupported_ids) | collector.type_members(sorted(unsupported_ids))
        for entry in unsupported:
            entry["affected_variables"] = affected.get(entry["node_id"], [])
            entry["excluded_members"] = sorted(collector.type_members([entry["node_id"]]))
        emit_ids = [node_id for node_id in emit_ids if node_id not in excluded]

    if not emit_ids:
        raise ExportError(
            "Nothing to write: no nodes in a non-zero namespace were selected for the NodeSet2 file."
        )

    snapshot_ids = set(snapshot.nodes)
    dependency_nodes_added = sum(1 for node_id in emit_ids if node_id not in snapshot_ids)

    if progress is not None:
        try:
            progress(
                TraversalProgress(
                    phase="writing",
                    message=f"Writing {len(emit_ids)} node(s) to NodeSet2 XML",
                    nodes_collected=len(emit_ids),
                )
            )
        except Exception:  # pragma: no cover - GUI callback must not break the export
            _logger.debug("NodeSet2 progress callback failed", exc_info=True)

    exporter = XmlExporter(client, export_values=settings.include_values)
    await exporter.build_etree([client.get_node(node_id) for node_id in emit_ids])
    argument_values_written = 0
    argument_problems: list[str] = []
    if not settings.include_values:
        argument_values_written, argument_problems = await _inject_argument_values(
            client, exporter, collector.argument_property_nodes(emit_ids)
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        await exporter.write_xml(str(target), pretty=settings.pretty)
    except Exception as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc

    validation = ValidationResult(schema_path=settings.schema_path)
    dangling: list[dict[str, str]] = []
    namespaces: list[dict[str, Any]] = []
    try:
        inspection = await asyncio.to_thread(_inspect_nodeset, target)
    except Exception as exc:
        validation.well_formed = False
        validation.xml_parse_error = f"{type(exc).__name__}: {exc}"
    else:
        errors, dangling = _structural_validation(inspection)
        validation.structural_errors = errors
        namespaces = [{"index": 0, "uri": _NS0}] + [
            {"index": index + 1, "uri": uri} for index, uri in enumerate(inspection.namespaces)
        ]
        if dangling:
            validation.warnings.append(f"{len(dangling)} reference(s) point to nodes outside the file.")

    schema_attempted, schema_valid, schema_error = await asyncio.to_thread(
        _validate_schema, target, settings.schema_path
    )
    validation.schema_attempted = schema_attempted
    validation.schema_valid = schema_valid
    validation.schema_error = schema_error

    limitations = [
        "Namespace 0 nodes are referenced, not redefined: the target server's standard information model "
        "is required for the file to be importable.",
        "An export from a live server contains what this session was allowed to browse; it is not "
        "guaranteed to be a complete, independently importable information model.",
        "Validation checks XML well-formedness and structure. XSD validation against the official NodeSet2 "
        "schema only runs when a schema file and lxml are available.",
    ]
    written_classes = {
        meta.node_class for node_id in emit_ids if (meta := collector.meta.get(node_id)) is not None
    }
    limitations.extend(_exporter_limitations(written_classes, collector.skipped_views))
    if dangling:
        limitations.append(
            f"{len(dangling)} reference(s) point to nodes that are not part of the file (for example nodes "
            "outside a truncated download); importers may report them as broken."
        )
    if unsupported:
        limitations.append(
            f"{len(unsupported)} custom DataType(s) without a DataTypeDefinition were not written; variables "
            "typed by them keep a DataType reference to a node outside the file."
        )
    if collector.budget_exhausted:
        limitations.append("The dependency budget was reached; some dependencies were not collected.")
    if not settings.include_dependencies:
        limitations.append("Dependency collection was disabled; the file may lack parents or type definitions.")
    if settings.include_values:
        limitations.append(
            "Values were included. asyncua marks value export as limited, and custom structures may not "
            "round-trip."
        )
    elif argument_values_written:
        limitations.append(
            f"Values are not included except for {argument_values_written} method argument property "
            "(InputArguments / OutputArguments) required to call the methods."
        )
    for problem in argument_problems:
        limitations.append(f"Method argument metadata not written: {problem}")
    if collector.missing_dependencies:
        limitations.append(f"{len(collector.missing_dependencies)} dependencies could not be resolved; see the list.")
    if snapshot.limits_hit:
        limitations.append(
            "The downloaded snapshot was truncated (limits: "
            + ", ".join(snapshot.limits_hit)
            + "); the exported model covers only the downloaded region."
        )

    try:
        size = target.stat().st_size
    except OSError:  # pragma: no cover - file was just written
        size = 0

    return NodeSet2Result(
        path=target,
        bytes_written=size,
        nodes_written=len(emit_ids),
        node_ids_written=list(emit_ids),
        dependency_nodes_added=dependency_nodes_added,
        namespaces=namespaces,
        standard_dependencies=collector.standard_dependencies,
        missing_dependencies=collector.missing_dependencies,
        unsupported_data_types=unsupported,
        argument_values_written=argument_values_written,
        dangling_references=dangling,
        validation=validation,
        limitations=limitations,
        config=settings,
        duration_seconds=time.perf_counter() - started,
    )

