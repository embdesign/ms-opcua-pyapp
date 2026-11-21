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


class _RefreshNode:
    """The node ConditionRefresh is called on; records method id and arguments."""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []
        self._result = result
        self._error = error

    async def call_method(self, method: Any, *args: Any) -> Any:
        self.calls.append((method, args))
        if self._error is not None:
            raise self._error
        return self._result


class _RefreshClient:
    def __init__(self, node: _RefreshNode) -> None:
        self.node = node
        self.requested: list[Any] = []

    def get_node(self, node_id: Any) -> _RefreshNode:
        self.requested.append(node_id)
        return self.node


async def test_refresh_conditions_sends_one_call_with_the_subscription_id() -> None:
    node = _RefreshNode()
    client = _RefreshClient(node)

    outcome = await write_service.refresh_conditions(client, 42, authorization=_AUTHORITY)

    # One call, on the ConditionType object, with the SubscriptionId as the only input.
    assert client.requested == [ua.NodeId(ua.ObjectIds.ConditionType)]
    assert len(node.calls) == 1
    method, arguments = node.calls[0]
    assert method == ua.NodeId(ua.ObjectIds.ConditionType_ConditionRefresh)
    assert len(arguments) == 1
    assert arguments[0].Value == 42
    assert arguments[0].VariantType == ua.VariantType.UInt32
    assert outcome.status == STATUS_PASSED
    assert outcome.status_code == "Good"
    assert outcome.sent is True
    assert outcome.authorization is _AUTHORITY
    assert outcome.arguments == ["SubscriptionId=42"]


async def test_refresh_conditions_can_name_another_parent_object() -> None:
    node = _RefreshNode()
    client = _RefreshClient(node)

    outcome = await write_service.refresh_conditions(
        client, 7, object_node_id="i=2253", authorization=_AUTHORITY
    )

    assert client.requested == [ua.NodeId(ua.ObjectIds.Server)]
    assert outcome.object_node_id == "i=2253"
    assert len(node.calls) == 1


async def test_refresh_conditions_reports_a_refused_request() -> None:
    node = _RefreshNode(error=ua.UaStatusCodeError(ua.StatusCodes.BadSubscriptionIdInvalid))

    outcome = await write_service.refresh_conditions(_RefreshClient(node), 999, authorization=_AUTHORITY)

    assert outcome.status == STATUS_FAILED
    assert outcome.status_code == "BadSubscriptionIdInvalid"
    assert outcome.sent is True
    assert "refused" in outcome.detail


async def test_refresh_conditions_without_a_subscription_sends_nothing() -> None:
    node = _RefreshNode()
    client = _RefreshClient(node)

    outcome = await write_service.refresh_conditions(client, 0, authorization=_AUTHORITY)

    assert outcome.status == STATUS_SKIPPED
    assert outcome.sent is False
    assert node.calls == []
    assert client.requested == []
