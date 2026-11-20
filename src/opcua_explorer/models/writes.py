"""Write, method-call and node-management request/outcome records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .common import iso_utc, utc_now

#: The only non-Value attributes this client writes: their text is metadata, not
#: process data, and the server still decides whether the write is allowed.
WRITABLE_TEXT_ATTRIBUTES = ("DisplayName", "Description")

#: Node classes this client can create (one node per confirmed request).
CREATABLE_NODE_CLASSES = ("Object", "Variable")


#: Where one write or method call was authorised. Every send records its authority, so a
#: result can never look like an unattributed write.
AUTHORITY_OPERATOR_DIALOG = "operator-dialog"
AUTHORITY_SEQUENCE_DOCUMENT = "sequence-document"


@dataclass(frozen=True, slots=True)
class WriteAuthority:
    """Who authorised one write or method call, and what that authority pointed at.

    ``source`` is one of the ``AUTHORITY_*`` constants; ``reference`` names the check id or the
    sequence step, so a report can be traced back to the confirmation the operator saw.
    """

    source: str
    reference: str = ""

    def describe(self) -> str:
        return f"{self.source}: {self.reference}" if self.reference else self.source

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "reference": self.reference}


@dataclass(slots=True)
class NodeCreateRequest:
    """One node-creation request: a child below a parent, with a server-assigned NodeId.

    The client never invents a NodeId: it asks the server for one in the parent's
    namespace (or the namespace the operator names) and reports the id that came
    back. ``value_text`` only applies to a Variable and is parsed with the
    server's DataType before anything is sent.
    """

    parent_node_id: str
    browse_name: str
    node_class: str = "Object"
    display_name: str = ""
    data_type: str = "i=11"
    value_text: str = "0"
    namespace_index: int = -1

    def normalised(self) -> NodeCreateRequest:
        node_class = (self.node_class or "Object").strip() or "Object"
        name = (self.browse_name or "").strip()
        return NodeCreateRequest(
            parent_node_id=(self.parent_node_id or "").strip(),
            browse_name=name,
            node_class=node_class,
            display_name=(self.display_name or "").strip() or name,
            data_type=(self.data_type or "i=11").strip(),
            value_text=(self.value_text or "0").strip(),
            namespace_index=int(self.namespace_index),
        )

    def label(self) -> str:
        parts = [self.node_class, f"{self.browse_name!r} below {self.parent_node_id}"]
        if self.node_class == "Variable":
            parts.append(f"DataType {self.data_type}, value {self.value_text}")
        if self.namespace_index >= 0:
            parts.append(f"namespace {self.namespace_index}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_node_id": self.parent_node_id,
            "browse_name": self.browse_name,
            "node_class": self.node_class,
            "display_name": self.display_name,
            "data_type": self.data_type,
            "value_text": self.value_text,
            "namespace_index": self.namespace_index,
        }


@dataclass(slots=True)
class NodeCreateOutcome:
    """Result of one AddNodes request, including the read-back of the new node."""

    request: NodeCreateRequest
    node_id: str = ""
    status: str = "failed"
    status_code: str = ""
    detail: str = ""
    read_back_name: str = ""
    verified: bool | None = None
    sent: bool = False
    duration_ms: float = 0.0
    authorization: Any = None
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        head = f"Create {self.request.node_class} {self.request.browse_name!r}"
        if self.node_id:
            head += f" -> {self.node_id}"
        return f"{head}: {self.detail or self.status}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "node_id": self.node_id,
            "status": self.status,
            "status_code": self.status_code,
            "detail": self.detail,
            "read_back_name": self.read_back_name,
            "verified": self.verified,
            "sent": self.sent,
            "duration_ms": round(self.duration_ms, 3),
            "authorization": self.authorization.describe() if self.authorization else "",
            "received_at": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class NodeDeleteOutcome:
    """Result of one DeleteNodes request, verified by reading the node afterwards."""

    node_id: str
    status: str = "failed"
    status_code: str = ""
    detail: str = ""
    verified: bool | None = None
    sent: bool = False
    duration_ms: float = 0.0
    authorization: Any = None
    received_at: datetime = field(default_factory=utc_now)

    def summary_text(self) -> str:
        return f"Delete {self.node_id}: {self.detail or self.status}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "status_code": self.status_code,
            "detail": self.detail,
            "verified": self.verified,
            "sent": self.sent,
            "duration_ms": round(self.duration_ms, 3),
            "authorization": self.authorization.describe() if self.authorization else "",
            "received_at": iso_utc(self.received_at),
        }


@dataclass(slots=True)
class WriteTarget:
    """One explicitly requested write. There is no way to write to more than one node per entry."""

    node_id: str
    value_text: str
    #: Off by default: a previous value is only restored when the operator asks for it.
    restore_previous_value: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "value_text": self.value_text,
            "restore_previous_value": self.restore_previous_value,
            "note": self.note,
        }


@dataclass(slots=True)
class MethodCallTarget:
    """One explicitly requested method call on one object node."""

    object_node_id: str
    method_node_id: str
    #: Raw argument texts in server order; validated against ``InputArguments``.
    arguments: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_node_id": self.object_node_id,
            "method_node_id": self.method_node_id,
            "arguments": list(self.arguments),
            "note": self.note,
        }
