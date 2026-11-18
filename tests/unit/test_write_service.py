"""Unit tests for the single write/call/restore implementation (no server, no Qt).

The fake node below records what it was asked to write, so these tests can prove the mechanical
rules the module owns: one send per call, a read-back that decides the status, an honest
``outcome_unknown`` when the effect cannot be checked, and an authority on every outcome.
"""

from __future__ import annotations

from typing import Any

from asyncua import ua

from opcua_explorer.models import (
    AUTHORITY_OPERATOR_DIALOG,
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    WriteAuthority,
)
from opcua_explorer.opcua import write_service

_AUTHORITY = WriteAuthority(AUTHORITY_OPERATOR_DIALOG, "write_value_1")
_NODE_ID = "ns=2;s=Demo.Temperature"


class FakeNode:
    """A variable with one value that records every write.

    A read returns the current value, like a normal server; ``read_back`` overrides that to
    simulate a server that acknowledges a write and then stores something else.
    """

    def __init__(
        self,
        value: Any = 21.5,
        *,
        write_error: Exception | None = None,
        read_error: Exception | None = None,
        read_back: Any = None,
    ) -> None:
        self.node_id = _NODE_ID
        self.writes: list[Any] = []
        self._value = value
        self._write_error = write_error
        self._read_error = read_error
        self._read_back = read_back

    async def write_value(self, data_value: ua.DataValue) -> None:
        if self._write_error is not None:
            raise self._write_error
        self._value = data_value.Value.Value
        self.writes.append(self._value)

    async def read_data_value(self) -> ua.DataValue:
        if self._read_error is not None:
            raise self._read_error
        value = self._read_back if self._read_back is not None else self._value
        return ua.DataValue(ua.Variant(value, ua.VariantType.Double))


def _prepared(node: FakeNode) -> write_service.PreparedWrite:
    return write_service.PreparedWrite(
        node=node,
        node_id=node.node_id,
        data_type=ua.NodeId(11),
        value_rank=-1,
        variant_type=ua.VariantType.Double,
        parsed=42.5,
    )


async def test_restore_writes_the_previous_value_once_and_verifies_it() -> None:
    node = FakeNode(21.5)

    outcome = await write_service.restore_value(_prepared(node), 21.5, authorization=_AUTHORITY)

    assert node.writes == [21.5]
    assert outcome.status == STATUS_PASSED
    assert outcome.sent is True
    assert outcome.written_value == "21.5"
    assert outcome.read_back_value == "21.5"
    assert outcome.authorization is _AUTHORITY


async def test_restore_reports_a_read_back_error_as_outcome_unknown() -> None:
    node = FakeNode(read_error=RuntimeError("link gone"))

    outcome = await write_service.restore_value(_prepared(node), 21.5, authorization=_AUTHORITY)

    assert node.writes == [21.5]
    assert outcome.status == STATUS_OUTCOME_UNKNOWN
    assert outcome.sent is True
    assert "unknown" in outcome.detail.lower()


async def test_restore_reports_a_rejected_write_as_failed() -> None:
    node = FakeNode(write_error=ua.UaStatusCodeError(0x80340000))

    outcome = await write_service.restore_value(_prepared(node), 21.5, authorization=_AUTHORITY)

    assert node.writes == []
    assert outcome.status == STATUS_FAILED
    assert outcome.sent is True
    assert "BadNodeIdUnknown" in outcome.detail


async def test_restore_marks_a_mismatched_read_back_as_failed() -> None:
    node = FakeNode(21.5, read_back=99.0)

    outcome = await write_service.restore_value(_prepared(node), 21.5, authorization=_AUTHORITY)

    assert node.writes == [21.5]
    assert outcome.status == STATUS_FAILED
    assert outcome.read_back_value == "99.0"
    assert "differs" in outcome.detail


async def test_prepared_write_records_the_authority_and_the_previous_value() -> None:
    node = FakeNode(21.5)

    outcome = await write_service.send_prepared_write(_prepared(node), authorization=_AUTHORITY)

    assert node.writes == [42.5]
    assert outcome.status == STATUS_PASSED
    assert outcome.previous_value == "21.5"
    assert outcome.previous_known is True
    assert outcome.authorization is _AUTHORITY


async def test_a_refused_write_keeps_the_authority() -> None:
    class UnreadableClient:
        def get_node(self, node_id: str) -> Any:
            raise RuntimeError(f"no node {node_id}")

    outcome = await write_service.write_variable(
        UnreadableClient(), _NODE_ID, "42.5", authorization=_AUTHORITY
    )

    assert outcome.status == STATUS_SKIPPED
    assert outcome.sent is False
    assert outcome.authorization is _AUTHORITY
    assert "could not be read" in outcome.detail
