"""NodeSet2 output validation: well-formedness, structure and optional XSD.

Split out of ``nodeset2.py`` so the exporter and its dependency collector stay
readable. The exporter calls these helpers after writing the file; the names are
also re-exported from ``opcua.nodeset2`` for existing callers and tests.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asyncua import ua


@dataclass(slots=True)
class ValidationResult:
    well_formed: bool = True
    xml_parse_error: str | None = None
    structural_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_attempted: bool = False
    schema_valid: bool | None = None
    schema_error: str | None = None
    schema_path: str | None = None

    @property
    def status(self) -> str:
        if not self.well_formed or self.structural_errors:
            return "invalid"
        if self.schema_valid is True:
            return "valid"
        if self.schema_valid is False:
            return "invalid"
        return "not-validated"

    @property
    def schema_status(self) -> str:
        if self.schema_valid is True:
            return "valid"
        if self.schema_valid is False:
            return "invalid"
        if self.schema_attempted:
            return "skipped"
        return "not-requested"

    def to_dict(self) -> dict[str, Any]:
        return {
            "well_formed": self.well_formed,
            "xml_parse_error": self.xml_parse_error,
            "structural_errors": list(self.structural_errors),
            "warnings": list(self.warnings),
            "status": self.status,
            "schema_status": self.schema_status,
            "schema_path": self.schema_path,
            "schema_error": self.schema_error,
        }


_UANS = "{http://opcfoundation.org/UA/2011/03/UANodeSet.xsd}"


@dataclass(slots=True)
class _Inspection:
    namespaces: list[str]
    node_ids: set[str]
    parents: set[str]
    references: list[tuple[str, str]]


def _namespace_index_of(node_id: str) -> int:
    try:
        return ua.NodeId.from_string(node_id).NamespaceIndex
    except Exception:  # pragma: no cover - defensive
        return 0


def _inspect_nodeset(path: Path) -> _Inspection:
    """Parse the written file back (well-formedness + structure)."""
    root = ET.parse(path).getroot()
    namespaces = [element.text or "" for element in root.findall(f"{_UANS}NamespaceUris/{_UANS}Uri")]
    nodes = [
        element
        for element in root
        if element.tag.startswith(_UANS) and element.tag[len(_UANS) :].startswith("UA")
    ]
    node_ids: set[str] = set()
    parents: set[str] = set()
    references: list[tuple[str, str]] = []
    for element in nodes:
        node_id = element.get("NodeId")
        if node_id:
            node_ids.add(node_id)
        parent = element.get("ParentNodeId")
        if parent:
            parents.add(parent)
        for reference in element.findall(f"{_UANS}References/{_UANS}Reference"):
            references.append((node_id or "", reference.text or ""))
    return _Inspection(namespaces=namespaces, node_ids=node_ids, parents=parents, references=references)


def _structural_validation(inspection: _Inspection) -> tuple[list[str], list[dict[str, str]]]:
    """Check parent resolvability, namespace declarations and dangling references."""
    errors: list[str] = []
    dangling: list[dict[str, str]] = []
    declared = len(inspection.namespaces)  # ns=0 is implicit, ns=1..N are declared

    def check_namespace(value: str, where: str) -> None:
        match = re.search(r"ns=(\d+)", value or "")
        if match and int(match.group(1)) > declared:
            errors.append(
                f"{where} uses ns={match.group(1)} but only {declared} namespace URI(s) are declared"
            )

    for node_id in sorted(inspection.node_ids):
        check_namespace(node_id, "NodeId")
    for parent in sorted(inspection.parents):
        check_namespace(parent, "ParentNodeId")
        if _namespace_index_of(parent) != 0 and parent not in inspection.node_ids:
            errors.append(f"ParentNodeId {parent} is neither in the file nor in namespace 0")
    for source, target in inspection.references:
        check_namespace(target, "Reference")
        if _namespace_index_of(target) != 0 and target not in inspection.node_ids:
            dangling.append({"source": source, "target": target})
    return errors, dangling


def _validate_schema(path: Path, schema_path: str | None) -> tuple[bool, bool | None, str | None]:
    """Optional XSD validation; ``lxml`` is an optional dependency."""
    if not schema_path:
        return False, None, None
    schema_file = Path(schema_path)
    if not schema_file.exists():
        return True, None, f"schema file not found: {schema_file}"
    try:
        from lxml import etree as lxml_etree
    except Exception:
        return True, None, "schema validation skipped: lxml is not installed (install the 'validation' extra)"
    try:
        schema = lxml_etree.XMLSchema(lxml_etree.parse(str(schema_file)))
        document = lxml_etree.parse(str(path))
        if schema.validate(document):
            return True, True, None
        messages = [f"line {entry.line}: {entry.message}" for entry in list(schema.error_log)[:5]]
        return True, False, "; ".join(messages)
    except Exception as exc:  # pragma: no cover - defensive
        return True, None, f"schema validation could not be performed: {exc}"
