"""Unit tests for the interactive write/call primitives (no server)."""

from __future__ import annotations

from typing import Any

import pytest
from asyncua import ua

from opcua_explorer.models import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    WriteAuthority,
)
from opcua_explorer.opcua.write_service import (
    RestoreOutcome,
    WriteOutcome,
    WriteResult,
    read_method_signature,
)

AUTHORITY = WriteAuthority("operator-dialog", "write ns=2;s=Temperature")


def _write(status: str = STATUS_PASSED, **kwargs: Any) -> WriteOutcome:
    return WriteOutcome(node_id="ns=2;s=Temperature", status=status, authorization=AUTHORITY, **kwargs)


def test_result_without_restore_reports_the_write() -> None:
    result = WriteResult(_write(detail="Wrote 42.5 and read it back."))

    assert result.status == STATUS_PASSED
    assert result.summary_text() == "Wrote 42.5 and read it back."


def test_a_failed_write_is_not_hidden_by_a_successful_restore() -> None:
    result = WriteResult(
        _write(STATUS_FAILED, detail="Read-back differs."),
        RestoreOutcome(node_id="ns=2;s=Temperature", status=STATUS_PASSED, detail="Restored."),
    )

    assert result.status == STATUS_FAILED
    assert result.summary_text() == "Read-back differs. Restored."


def test_a_passed_write_reports_the_restore_status() -> None:
    result = WriteResult(
        _write(detail="Wrote."),
        RestoreOutcome(node_id="ns=2;s=Temperature", status=STATUS_FAILED, detail="Restore refused."),
    )

    assert result.status == STATUS_FAILED
    assert result.summary_text() == "Wrote. Restore refused."


class _FakeProperty:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def read_value(self) -> Any:
        return self._value


class _FakeMethod:
    def __init__(self, arguments: Any) -> None:
        self._arguments = arguments

    async def get_child(self, names: list[str]) -> _FakeProperty:
        if self._arguments is None:
            raise ua.UaError("BadNoMatch")
        return _FakeProperty(self._arguments)


class _FakeClient:
    def __init__(self, arguments: Any) -> None:
        self._method = _FakeMethod(arguments)

    def get_node(self, node_id: str) -> _FakeMethod:
        return self._method


def _argument(name: str, data_type: ua.NodeId, description: str = "") -> ua.Argument:
    argument = ua.Argument()
    argument.Name = name
    argument.DataType = data_type
    argument.ValueRank = -1
    argument.Description = ua.LocalizedText(Text=description) if description else ua.LocalizedText()
    return argument


async def test_signature_rows_carry_name_type_rank_and_description() -> None:
    client = _FakeClient([_argument("Target", ua.NodeId(ua.ObjectIds.Double), "speed")])

    rows = await read_method_signature(client, "ns=2;s=Start")

    assert rows == [
        {
            "name": "Target",
            "data_type": "i=11",
            "data_type_name": "Double",
            "value_rank": -1,
            "description": "speed",
        }
    ]


async def test_a_method_without_input_arguments_has_an_empty_signature() -> None:
    assert await read_method_signature(_FakeClient(None), "i=1") == []


async def test_an_unreadable_signature_is_a_prepare_error() -> None:
    client = _FakeClient("unexpected")

    class _Broken(_FakeProperty):
        async def read_value(self) -> Any:
            raise ua.UaError("BadTooManyOperations")

    class _BrokenMethod(_FakeMethod):
        async def get_child(self, names: list[str]) -> _Broken:
            return _Broken(None)

    client._method = _BrokenMethod(None)  # type: ignore[attr-defined]

    with pytest.raises(Exception) as excinfo:
        await read_method_signature(client, "i=1")
    assert "InputArguments" in str(excinfo.value) or "unreadable" in str(excinfo.value).lower()


def test_skipped_write_result_keeps_the_reason() -> None:
    result = WriteResult(_write(STATUS_SKIPPED, detail="Value is not a Double."))

    assert result.status == STATUS_SKIPPED
    assert result.restore is None
    assert "Double" in result.summary_text()
