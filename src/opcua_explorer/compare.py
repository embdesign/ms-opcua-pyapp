"""Structural comparison of two OPC UA address-space snapshots.

The comparison answers one question: *do two servers expose the same information
model?* It is a pure function over two
:class:`~opcua_explorer.models.AddressSpace` objects. It performs no I/O, imports
neither ``asyncua`` nor Qt, and is therefore testable with hand-built snapshots.

Identity rules (they are the point of the module)
-------------------------------------------------
* A node is identified by its **namespace URI + identifier**, never by the
  namespace *index*: indexes are session-local and may differ between two
  servers or between two connections.
* NodeIds are not portable between devices. When an identifier changed but the
  position in the model did not, the fallback match uses the **qualified browse
  path** (namespace URI + BrowseName per level). ``ComparisonConfig.match_by_browse_path``
  controls that fallback.
* Values are compared only for nodes named in ``ComparisonConfig.value_node_ids``
  and only when both sides actually read one. A serial number or a counter is not
  a difference in functionality.

What the result does not claim
------------------------------
A snapshot contains what one session was allowed to browse and read with one
identity. :class:`ComparisonResult` therefore carries warnings when either side
was cancelled, hit a traversal limit, contains inaccessible nodes, or used a
different authentication mode (which makes ``UserAccessLevel`` incomparable), and
keeps "not comparable" nodes apart from equal ones.

NodeSet2 XML is deliberately not the comparison input: the snapshot model keeps
the attributes that matter here (access levels, value rank, data type, type
definition) in a form a NodeSet2 file does not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .defaults import DEFAULT_MAX_COMPARISON_DIFFERENCES
from .models import AddressSpace, Edge, NodeRecord

_logger = logging.getLogger(__name__)

TOLERANCE_FUNCTIONAL = "functional"
TOLERANCE_STRICT = "strict"
TOLERANCES = (TOLERANCE_FUNCTIONAL, TOLERANCE_STRICT)

KIND_MISSING_IN_CANDIDATE = "missing_in_candidate"
KIND_EXTRA_IN_CANDIDATE = "extra_in_candidate"
KIND_CHANGED = "changed"
KIND_NOT_COMPARABLE = "not_comparable"

#: Operator-facing labels for the four buckets (CLI, HTML report, GUI).
KIND_LABELS = {
    KIND_MISSING_IN_CANDIDATE: "missing on the candidate",
    KIND_EXTRA_IN_CANDIDATE: "extra on the candidate",
    KIND_CHANGED: "changed",
    KIND_NOT_COMPARABLE: "not comparable",
}

COMPARISON_FORMAT = "opcua-comparison"
COMPARISON_FORMAT_VERSION = 1

#: Carried in every result: a comparison is evidence, not a certificate.
COMPARISON_LIMITATION_NOTE = (
    "A comparison reflects what each session was allowed to browse and read with the identity it used. "
    "It is evidence of structural agreement for that scope, not proof that both servers behave identically."
)

#: Standard hierarchical reference types used to build browse paths. Custom
#: subtypes are recognised through the reference *name* (resolved by the
#: traversal); the numeric ids are only a fallback for snapshots without names.
_HIERARCHICAL_REFERENCE_NAMES = frozenset(
    {
        "HierarchicalReferences",
        "HasChild",
        "Aggregates",
        "HasComponent",
        "HasProperty",
        "Organizes",
        "HasSubtype",
        "HasEventSource",
        "HasNotifier",
    }
)
_HIERARCHICAL_REFERENCE_IDS = frozenset({"i=34", "i=35", "i=45", "i=46", "i=47"})

#: Upper bounds that keep browse-path collection linear on graphs with many
#: shared children (the demo model deliberately contains one).
_MAX_PATHS_PER_NODE = 8
_MAX_PATH_DEPTH = 64

_NamespaceUriOf = Callable[[int | None], str | None]


# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------


def _parse_node_id(text: str) -> tuple[int | None, str | None, str]:
    """Split a NodeId string into ``(namespace index, inline namespace URI, identifier)``.

    Accepts ``i=85`` (namespace 0), ``ns=2;i=3005``, ``ns=2;s=Temperature`` and
    the expanded form ``nsu=<uri>;i=5``.
    """
    raw = (text or "").strip()
    if not raw:
        return None, None, ""
    head, separator, rest = raw.partition(";")
    if not separator:
        return 0, None, raw
    if head.startswith("ns="):
        try:
            return int(head[3:]), None, rest
        except ValueError:
            return None, None, raw
    if head.startswith("nsu="):
        return None, head[4:], rest
    return 0, None, raw


def qualified_key(node_id: str | None, namespace_uri_of: _NamespaceUriOf) -> str:
    """Return ``<namespace uri>|<identifier>`` for a NodeId string.

    The result is comparable across sessions whose namespace indexes differ.
    """
    if not node_id:
        return ""
    index, inline_uri, identifier = _parse_node_id(node_id)
    uri = inline_uri or namespace_uri_of(index)
    return f"{uri or f'ns={index}'}|{identifier}"


def _browse_key(record: NodeRecord, snapshot: AddressSpace) -> str:
    uri = snapshot.namespace_uri(record.browse_name_namespace_index)
    return f"{uri or f'ns={record.browse_name_namespace_index}'}|{record.browse_name}"


def browse_path_text(path: Iterable[str]) -> str:
    """Readable path from qualified browse keys: the BrowseName parts, joined by ``/``."""
    return "/".join(element.rsplit("|", 1)[-1] for element in path)


def _is_hierarchical_edge(edge: Edge) -> bool:
    if edge.reference_type_name:
        return edge.reference_type_name in _HIERARCHICAL_REFERENCE_NAMES
    return edge.reference_type in _HIERARCHICAL_REFERENCE_IDS


# ---------------------------------------------------------------------------
# Configuration and result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComparisonConfig:
    """What to compare and what to tolerate.

    ``tolerance`` selects the attribute set:

    * ``functional`` — node class, data type, value rank, array dimensions,
      access level, user access level and type definition;
    * ``strict`` — additionally display name and description (text that servers
      may localise, so it is not part of "functional" equivalence).
    """

    tolerance: str = TOLERANCE_FUNCTIONAL
    #: Match nodes whose identifier changed by their qualified browse path.
    match_by_browse_path: bool = True
    #: Also compare the reference graph (more rows, slower).
    compare_references: bool = False
    #: Nodes whose value is compared (NodeId strings or qualified keys).
    value_node_ids: frozenset[str] = frozenset()
    #: Compare the payload, not only status and variant type.
    compare_value_payload: bool = True
    #: Absolute tolerance for numeric payload comparison (0 = exact).
    float_tolerance: float = 0.0
    #: Nodes expected to differ (current time, counters, serial numbers).
    ignore_node_ids: frozenset[str] = frozenset()
    max_differences: int = DEFAULT_MAX_COMPARISON_DIFFERENCES

    def normalised(self) -> ComparisonConfig:
        tolerance = (self.tolerance or TOLERANCE_FUNCTIONAL).strip().lower()
        if tolerance not in TOLERANCES:
            tolerance = TOLERANCE_FUNCTIONAL
        return ComparisonConfig(
            tolerance=tolerance,
            match_by_browse_path=bool(self.match_by_browse_path),
            compare_references=bool(self.compare_references),
            value_node_ids=frozenset(self.value_node_ids or ()),
            compare_value_payload=bool(self.compare_value_payload),
            float_tolerance=max(0.0, float(self.float_tolerance or 0.0)),
            ignore_node_ids=frozenset(self.ignore_node_ids or ()),
            max_differences=max(1, int(self.max_differences or DEFAULT_MAX_COMPARISON_DIFFERENCES)),
        )

    @property
    def strict(self) -> bool:
        return self.tolerance == TOLERANCE_STRICT

    def to_dict(self) -> dict[str, Any]:
        return {
            "tolerance": self.tolerance,
            "match_by_browse_path": self.match_by_browse_path,
            "compare_references": self.compare_references,
            "value_node_ids": sorted(self.value_node_ids),
            "compare_value_payload": self.compare_value_payload,
            "float_tolerance": self.float_tolerance,
            "ignore_node_ids": sorted(self.ignore_node_ids),
            "max_differences": self.max_differences,
        }


@dataclass(frozen=True, slots=True)
class AttributeDifference:
    """One differing attribute of one matched node."""

    attribute: str
    reference: Any = None
    candidate: Any = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attribute": self.attribute,
            "reference": self.reference,
            "candidate": self.candidate,
            "note": self.note,
        }


@dataclass(slots=True)
class NodeDifference:
    """One row of the comparison report."""

    kind: str
    key: str
    browse_path: str = ""
    node_class: str = ""
    reference_node_id: str | None = None
    candidate_node_id: str | None = None
    attributes: list[AttributeDifference] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "browse_path": self.browse_path,
            "node_class": self.node_class,
            "reference_node_id": self.reference_node_id,
            "candidate_node_id": self.candidate_node_id,
            "note": self.note,
            "attributes": [item.to_dict() for item in self.attributes],
        }


@dataclass(frozen=True, slots=True)
class ReferenceDifference:
    """A reference present on only one side (only when references are compared)."""

    kind: str
    source: str
    target: str
    reference_type_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "reference_type": self.reference_type_name,
        }


@dataclass(slots=True)
class ComparisonResult:
    """Everything the report needs about one comparison."""

    reference_endpoint: str
    candidate_endpoint: str
    config: ComparisonConfig
    matched: int = 0
    ignored: int = 0
    missing_in_candidate: list[NodeDifference] = field(default_factory=list)
    extra_in_candidate: list[NodeDifference] = field(default_factory=list)
    changed: list[NodeDifference] = field(default_factory=list)
    not_comparable: list[NodeDifference] = field(default_factory=list)
    reference_differences: list[ReferenceDifference] = field(default_factory=list)
    namespace_differences: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated_differences: int = 0
    reference_summary: dict[str, Any] = field(default_factory=dict)
    candidate_summary: dict[str, Any] = field(default_factory=dict)

    # -- counts ------------------------------------------------------------
    @property
    def differences(self) -> list[NodeDifference]:
        return [
            *self.missing_in_candidate,
            *self.extra_in_candidate,
            *self.changed,
            *self.not_comparable,
        ]

    @property
    def difference_count(self) -> int:
        return len(self.differences) + self.truncated_differences

    @property
    def has_differences(self) -> bool:
        return bool(
            self.difference_count
            or self.reference_differences
            or self.namespace_differences
        )

    @property
    def is_equal(self) -> bool:
        """True when no difference was found in the compared scope.

        Warnings do not make the result unequal, but they always mean the scope
        itself was incomplete - check :attr:`has_warnings` before relying on it.
        """
        return not self.has_differences

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    # -- rendering ---------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return {
            "format": COMPARISON_FORMAT,
            "version": COMPARISON_FORMAT_VERSION,
            "reference_endpoint": self.reference_endpoint,
            "candidate_endpoint": self.candidate_endpoint,
            "config": self.config.to_dict(),
            "matched": self.matched,
            "ignored": self.ignored,
            "difference_count": self.difference_count,
            "counts": {
                KIND_MISSING_IN_CANDIDATE: len(self.missing_in_candidate),
                KIND_EXTRA_IN_CANDIDATE: len(self.extra_in_candidate),
                KIND_CHANGED: len(self.changed),
                KIND_NOT_COMPARABLE: len(self.not_comparable),
                "truncated": self.truncated_differences,
            },
            "reference_differences": len(self.reference_differences),
            "namespace_differences": list(self.namespace_differences),
            "warnings": list(self.warnings),
            "limitations": [COMPARISON_LIMITATION_NOTE],
            "reference": dict(self.reference_summary),
            "candidate": dict(self.candidate_summary),
        }

    def to_dict(self) -> dict[str, Any]:
        document = self.summary()
        document["differences"] = [item.to_dict() for item in self.differences]
        document["references"] = [item.to_dict() for item in self.reference_differences]
        return document

    def summary_text(self) -> str:
        """Operator-facing summary for the status/log view."""
        lines = [
            f"Reference:        {self.reference_endpoint}",
            f"Candidate:        {self.candidate_endpoint}",
            f"Tolerance:        {self.config.tolerance}",
            f"Matched:          {self.matched}",
            f"Missing:          {len(self.missing_in_candidate)}",
            f"Extra:            {len(self.extra_in_candidate)}",
            f"Changed:          {len(self.changed)}",
            f"Not comparable:   {len(self.not_comparable)}",
        ]
        if self.ignored:
            lines.append(f"Ignored:          {self.ignored}")
        if self.truncated_differences:
            lines.append(f"Truncated:        {self.truncated_differences} further difference(s) not listed")
        if self.reference_differences:
            lines.append(f"References:       {len(self.reference_differences)} difference(s)")
        if self.namespace_differences:
            lines.append(f"Namespaces:       {len(self.namespace_differences)} difference(s)")
        for warning in self.warnings:
            lines.append(f"Warning:          {warning}")
        lines.append(
            "Result:           no difference found" if self.is_equal else "Result:           differences found"
        )
        lines.append(f"Note:             {COMPARISON_LIMITATION_NOTE}")
        return "\n".join(lines)


class _Collector:
    """Appends differences up to a limit and counts the rest."""

    def __init__(self, result: ComparisonResult) -> None:
        self._result = result
        self._limit = result.config.max_differences
        self._seen = 0

    def add(self, bucket: list[NodeDifference], difference: NodeDifference) -> None:
        self._seen += 1
        if self._seen > self._limit:
            self._result.truncated_differences += 1
            return
        bucket.append(difference)


# ---------------------------------------------------------------------------
# Snapshot indexing
# ---------------------------------------------------------------------------


class _Index:
    """Qualified keys, records and browse paths of one snapshot."""

    __slots__ = ("by_key", "key_of_node", "node_id_of", "paths", "snapshot")

    def __init__(self, snapshot: AddressSpace) -> None:
        self.snapshot = snapshot
        #: session NodeId -> qualified key
        self.key_of_node: dict[str, str] = {
            node_id: qualified_key(node_id, snapshot.namespace_uri) for node_id in snapshot.nodes
        }
        self.by_key: dict[str, NodeRecord] = {}
        #: qualified key -> session NodeId (for the report's "which node was it" columns)
        self.node_id_of: dict[str, str] = {}
        for node_id, key in self.key_of_node.items():
            self.by_key.setdefault(key, snapshot.nodes[node_id])
            self.node_id_of.setdefault(key, node_id)
        self.paths = _collect_browse_paths(snapshot, self.key_of_node, self.by_key)


def _collect_browse_paths(
    snapshot: AddressSpace,
    node_id_of: Mapping[str, str],
    by_key: Mapping[str, NodeRecord],
) -> dict[str, set[tuple[str, ...]]]:
    """Qualified browse paths per node, bounded in depth and count."""
    children: dict[str, set[str]] = {}
    incoming: set[str] = set()
    for edge in snapshot.references:
        if not _is_hierarchical_edge(edge):
            continue
        parent = node_id_of.get(edge.source)
        child = node_id_of.get(edge.target)
        if parent is None or child is None or parent == child:
            continue
        children.setdefault(parent, set()).add(child)
        incoming.add(child)

    roots = [node_id_of[item] for item in snapshot.roots if item in node_id_of]
    if not roots:
        roots = sorted(key for key in by_key if key not in incoming)
    if not roots:
        return {}

    paths: dict[str, set[tuple[str, ...]]] = {root: {()} for root in roots}
    stack: list[tuple[str, tuple[str, ...], frozenset[str]]] = [
        (root, (), frozenset({root})) for root in sorted(roots, reverse=True)
    ]
    while stack:
        key, path, visited = stack.pop()
        if len(path) >= _MAX_PATH_DEPTH:
            continue
        for child in sorted(children.get(key, ())):
            if child in visited:
                continue
            child_path = path + (_browse_key(by_key[child], snapshot),)
            bucket = paths.setdefault(child, set())
            if child_path in bucket or len(bucket) >= _MAX_PATHS_PER_NODE:
                continue
            bucket.add(child_path)
            stack.append((child, child_path, visited | {child}))
    return paths


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def _attribute_values(record: NodeRecord, snapshot: AddressSpace, *, strict: bool) -> dict[str, Any]:
    """The attributes that define functional equivalence, keyed by report label."""
    values: dict[str, Any] = {
        "NodeClass": record.node_class,
        "DataType": qualified_key(record.data_type, snapshot.namespace_uri),
        "ValueRank": record.value_rank,
        "ArrayDimensions": list(record.array_dimensions or []),
        "AccessLevel": sorted(record.access_level_names),
        "UserAccessLevel": sorted(record.user_access_level_names),
        "TypeDefinition": qualified_key(record.type_definition, snapshot.namespace_uri),
    }
    if strict:
        values["DisplayName"] = record.display_name
        values["Description"] = record.description or ""
    return values


def _values_equal(first: Any, second: Any, tolerance: float) -> bool:
    """Numeric comparison with an optional absolute tolerance; exact otherwise."""
    if isinstance(first, bool) or isinstance(second, bool):
        return first is second
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        difference = abs(float(first) - float(second))
        return difference <= tolerance if tolerance > 0 else difference == 0.0
    if isinstance(first, dict) and isinstance(second, dict):
        return set(first) == set(second) and all(
            _values_equal(first[key], second[key], tolerance) for key in first
        )
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(
            _values_equal(left, right, tolerance) for left, right in zip(first, second, strict=True)
        )
    return first == second


def _value_differences(
    record_reference: NodeRecord,
    record_candidate: NodeRecord,
    config: ComparisonConfig,
) -> list[AttributeDifference]:
    first = record_reference.value
    second = record_candidate.value
    if first is None and second is None:
        return []
    if first is None or second is None:
        missing = "candidate" if second is None else "reference"
        return [
            AttributeDifference(
                attribute="Value",
                reference=None if first is None else first.status,
                candidate=None if second is None else second.status,
                note=f"no value snapshot on the {missing} side",
            )
        ]

    differences: list[AttributeDifference] = []
    if first.status != second.status:
        differences.append(AttributeDifference("Value.Status", first.status, second.status))
    if (first.variant_type or "") != (second.variant_type or ""):
        differences.append(AttributeDifference("Value.Type", first.variant_type, second.variant_type))
    if config.compare_value_payload:
        if first.encoding != second.encoding:
            differences.append(AttributeDifference("Value.Encoding", first.encoding, second.encoding))
        if not _values_equal(first.value, second.value, config.float_tolerance):
            differences.append(AttributeDifference("Value.Payload", first.value, second.value))
    return differences


def _compare_pair(
    record_reference: NodeRecord,
    record_candidate: NodeRecord,
    snapshot_reference: AddressSpace,
    snapshot_candidate: AddressSpace,
    config: ComparisonConfig,
    *,
    compare_values: bool,
) -> tuple[list[AttributeDifference], bool]:
    """Attribute differences of one matched pair, plus "cannot be called equal" flag."""
    reference_values = _attribute_values(record_reference, snapshot_reference, strict=config.strict)
    candidate_values = _attribute_values(record_candidate, snapshot_candidate, strict=config.strict)

    differences: list[AttributeDifference] = []
    not_comparable = False
    for name, reference_value in reference_values.items():
        unavailable_reference = record_reference.unavailable.get(name)
        unavailable_candidate = record_candidate.unavailable.get(name)
        if unavailable_reference and unavailable_candidate:
            # Both servers refused the attribute: nothing to compare, and the
            # absence itself is not a difference.
            continue
        if unavailable_reference or unavailable_candidate:
            # One side exposes it and the other does not: this is not equality,
            # it is an unknown.
            not_comparable = True
            differences.append(
                AttributeDifference(
                    attribute=name,
                    reference=unavailable_reference or reference_value,
                    candidate=unavailable_candidate or candidate_values[name],
                    note=(
                        "unavailable on the reference"
                        if unavailable_reference
                        else "unavailable on the candidate"
                    ),
                )
            )
            continue
        if reference_value != candidate_values[name]:
            differences.append(AttributeDifference(name, reference_value, candidate_values[name]))

    if compare_values:
        differences.extend(_value_differences(record_reference, record_candidate, config))
    return differences, not_comparable


def _match_nodes(
    reference: _Index,
    candidate: _Index,
    config: ComparisonConfig,
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Match nodes by qualified NodeId, then (optionally) by qualified browse path."""
    matched: list[tuple[str, str]] = []
    reference_left = set(reference.by_key)
    candidate_left = set(candidate.by_key)

    common = reference_left & candidate_left
    matched.extend((key, key) for key in sorted(common))
    reference_left -= common
    candidate_left -= common

    if config.match_by_browse_path and reference_left and candidate_left:
        reference_paths: dict[tuple[str, ...], list[str]] = {}
        for key in reference_left:
            for path in reference.paths.get(key, ()):
                if path:
                    reference_paths.setdefault(path, []).append(key)
        candidate_paths: dict[tuple[str, ...], list[str]] = {}
        for key in candidate_left:
            for path in candidate.paths.get(key, ()):
                if path:
                    candidate_paths.setdefault(path, []).append(key)

        for path in sorted(set(reference_paths) & set(candidate_paths)):
            reference_keys = reference_paths[path]
            candidate_keys = candidate_paths[path]
            if len(reference_keys) != 1 or len(candidate_keys) != 1:
                # Ambiguous: sharing a path is not enough to claim "same node".
                continue
            reference_key, candidate_key = reference_keys[0], candidate_keys[0]
            if reference_key in reference_left and candidate_key in candidate_left:
                matched.append((reference_key, candidate_key))
                reference_left.discard(reference_key)
                candidate_left.discard(candidate_key)

    return matched, sorted(reference_left), sorted(candidate_left)


def _lookup_keys(entries: Iterable[str], snapshot: AddressSpace) -> set[str]:
    """Resolve configured NodeIds (session indexes) or qualified keys for one snapshot."""
    keys: set[str] = set()
    for entry in entries:
        text = (entry or "").strip()
        if not text:
            continue
        if "://" in text or text.startswith("urn:"):
            keys.add(text)
        else:
            keys.add(qualified_key(text, snapshot.namespace_uri))
    return keys


def _path_of(index: _Index, key: str) -> str:
    paths = index.paths.get(key)
    if paths:
        best = min(paths)
        if best:
            return browse_path_text(best)
    # A root (or an isolated node) has an empty path; show at least its own name.
    record = index.by_key.get(key)
    if record is None:
        return ""
    return browse_path_text((_browse_key(record, index.snapshot),))


def _snapshot_warnings(snapshot: AddressSpace, role: str) -> list[str]:
    warnings: list[str] = []
    if not snapshot.nodes:
        warnings.append(f"The {role} snapshot contains no nodes.")
    if snapshot.cancelled:
        warnings.append(
            f"The {role} snapshot was cancelled; it is partial and differences may be artifacts."
        )
    if snapshot.limits_hit:
        warnings.append(
            f"The {role} snapshot hit traversal limits ({', '.join(snapshot.limits_hit)}); "
            "nodes outside the budget were not compared."
        )
    if snapshot.inaccessible:
        warnings.append(
            f"The {role} session could not browse {len(snapshot.inaccessible)} node(s); "
            "they are reported as not comparable."
        )
    return warnings


def _identity_warnings(reference: AddressSpace, candidate: AddressSpace) -> list[str]:
    first, second = reference.connection, candidate.connection
    if first is None or second is None:
        return []
    if first.auth_mode != second.auth_mode:
        return [
            f"The sessions used different identities ({first.auth_mode} vs {second.auth_mode}); "
            "UserAccessLevel differences cannot be attributed to the servers."
        ]
    return []


def _namespace_differences(
    reference: AddressSpace,
    candidate: AddressSpace,
) -> list[dict[str, Any]]:
    """Namespace URIs declared on only one side (index changes are expected)."""
    reference_uris = {entry.uri for entry in reference.namespaces}
    candidate_uris = {entry.uri for entry in candidate.namespaces}
    differences: list[dict[str, Any]] = []
    for uri in sorted(reference_uris - candidate_uris):
        differences.append({"uri": uri, "reference_index": _namespace_index(reference, uri), "candidate_index": None})
    for uri in sorted(candidate_uris - reference_uris):
        differences.append({"uri": uri, "reference_index": None, "candidate_index": _namespace_index(candidate, uri)})
    return differences


def _namespace_index(snapshot: AddressSpace, uri: str) -> int | None:
    for entry in snapshot.namespaces:
        if entry.uri == uri:
            return entry.index
    return None


def _reference_differences(
    reference: _Index,
    candidate: _Index,
) -> list[ReferenceDifference]:
    def edges_of(index: _Index) -> dict[tuple[str, str, str], str]:
        seen: dict[tuple[str, str, str], str] = {}
        for edge in index.snapshot.references:
            source = index.key_of_node.get(edge.source)
            target = index.key_of_node.get(edge.target)
            if source is None or target is None:
                continue
            name = edge.reference_type_name or edge.reference_type
            seen.setdefault((source, name, target), name)
        return seen

    reference_edges = edges_of(reference)
    candidate_edges = edges_of(candidate)
    differences: list[ReferenceDifference] = []
    for source, name, target in sorted(reference_edges.keys() - candidate_edges.keys()):
        differences.append(ReferenceDifference(KIND_MISSING_IN_CANDIDATE, source, target, name))
    for source, name, target in sorted(candidate_edges.keys() - reference_edges.keys()):
        differences.append(ReferenceDifference(KIND_EXTRA_IN_CANDIDATE, source, target, name))
    return differences


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def compare_address_spaces(
    reference: AddressSpace,
    candidate: AddressSpace,
    config: ComparisonConfig | None = None,
) -> ComparisonResult:
    """Compare a reference snapshot with a candidate snapshot.

    ``reference`` is normally the existing device, ``candidate`` the replacement.
    Nothing is connected, read or written here: both snapshots must already exist
    (for example from two ``OpcUaService.download()`` calls).
    """
    settings = (config or ComparisonConfig()).normalised()
    result = ComparisonResult(
        reference_endpoint=reference.endpoint_url,
        candidate_endpoint=candidate.endpoint_url,
        config=settings,
        reference_summary=reference.summary(),
        candidate_summary=candidate.summary(),
    )
    result.warnings.extend(_snapshot_warnings(reference, "reference"))
    result.warnings.extend(_snapshot_warnings(candidate, "candidate"))
    result.warnings.extend(_identity_warnings(reference, candidate))
    result.namespace_differences = _namespace_differences(reference, candidate)
    if result.namespace_differences:
        result.warnings.append(
            f"{len(result.namespace_differences)} namespace URI(s) are declared on only one side; "
            "nodes in them cannot be matched."
        )

    reference_index = _Index(reference)
    candidate_index = _Index(candidate)
    ignore_reference = _lookup_keys(settings.ignore_node_ids, reference)
    ignore_candidate = _lookup_keys(settings.ignore_node_ids, candidate)
    value_reference = _lookup_keys(settings.value_node_ids, reference)
    value_candidate = _lookup_keys(settings.value_node_ids, candidate)

    matched, missing, extra = _match_nodes(reference_index, candidate_index, settings)
    collector = _Collector(result)

    for reference_key, candidate_key in matched:
        if reference_key in ignore_reference or candidate_key in ignore_candidate:
            result.ignored += 1
            continue
        record_reference = reference_index.by_key[reference_key]
        record_candidate = candidate_index.by_key[candidate_key]
        path = _path_of(reference_index, reference_key) or _path_of(candidate_index, candidate_key)

        node_reference = reference_index.node_id_of[reference_key]
        node_candidate = candidate_index.node_id_of[candidate_key]
        if node_reference in reference.inaccessible or node_candidate in candidate.inaccessible:
            status = (
                reference.inaccessible.get(node_reference)
                or candidate.inaccessible.get(node_candidate)
                or ""
            )
            collector.add(
                result.not_comparable,
                NodeDifference(
                    kind=KIND_NOT_COMPARABLE,
                    key=reference_key,
                    browse_path=path,
                    node_class=record_reference.node_class or record_candidate.node_class,
                    reference_node_id=node_reference,
                    candidate_node_id=node_candidate,
                    note=f"the node could not be browsed: {status}",
                ),
            )
            continue

        result.matched += 1
        differences, not_comparable = _compare_pair(
            record_reference,
            record_candidate,
            reference,
            candidate,
            settings,
            compare_values=reference_key in value_reference or candidate_key in value_candidate,
        )
        if not differences:
            continue
        collector.add(
            result.not_comparable if not_comparable else result.changed,
            NodeDifference(
                kind=KIND_NOT_COMPARABLE if not_comparable else KIND_CHANGED,
                key=reference_key,
                browse_path=path,
                node_class=record_reference.node_class or record_candidate.node_class,
                reference_node_id=reference_index.node_id_of[reference_key],
                candidate_node_id=candidate_index.node_id_of[candidate_key],
                attributes=differences,
            ),
        )

    for key in missing:
        if key in ignore_reference:
            result.ignored += 1
            continue
        record = reference_index.by_key[key]
        collector.add(
            result.missing_in_candidate,
            NodeDifference(
                kind=KIND_MISSING_IN_CANDIDATE,
                key=key,
                browse_path=_path_of(reference_index, key),
                node_class=record.node_class,
                reference_node_id=reference_index.node_id_of[key],
            ),
        )

    for key in extra:
        if key in ignore_candidate:
            result.ignored += 1
            continue
        record = candidate_index.by_key[key]
        collector.add(
            result.extra_in_candidate,
            NodeDifference(
                kind=KIND_EXTRA_IN_CANDIDATE,
                key=key,
                browse_path=_path_of(candidate_index, key),
                node_class=record.node_class,
                candidate_node_id=candidate_index.node_id_of[key],
            ),
        )

    if settings.compare_references:
        result.reference_differences = _reference_differences(reference_index, candidate_index)
    if result.truncated_differences:
        result.warnings.append(
            f"{result.truncated_differences} difference(s) were not listed "
            f"(limit {settings.max_differences}); the counts are complete."
        )

    _logger.info(
        "Address-space comparison: matched=%d missing=%d extra=%d changed=%d not_comparable=%d warnings=%d",
        result.matched,
        len(result.missing_in_candidate),
        len(result.extra_in_candidate),
        len(result.changed),
        len(result.not_comparable),
        len(result.warnings),
    )
    return result
