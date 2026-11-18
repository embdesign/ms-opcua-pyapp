"""Unit tests for the DisplayName/Description write path (no server)."""

from __future__ import annotations

from typing import Any

import pytest
from asyncua import ua

from opcua_explorer.models import WriteAuthority
from opcua_explorer.opcua.write_service import (
    PrepareError,
    prepare_attribute_write,
    send_prepared_attribute_write,
    write_attribute_value,
)

AUTHORITY = WriteAuthority("operator-dialog", "write Description")


def _text(value: str | None, status: int = ua.StatusCodes.Good) -> ua.DataValue:
    data_value = ua.DataValue(
        ua.Variant(ua.LocalizedText(Text=value) if value is not None else None, ua.VariantType.LocalizedText)
    )
    data_value.StatusCode = ua.StatusCode(status)
    return data_value


class _FakeNode:
    def __init__(self, current: ua.DataValue | None = None, *, write_error: Exception | None = None) -> None:
        self.current = current if current is not None else _text("old")
        self.write_error = write_error
        self.written: list[tuple[ua.AttributeIds, ua.DataValue]] = []
        self.read_calls: list[ua.AttributeIds] = []

    async def read_node_class(self) -> ua.NodeClass:
        return ua.NodeClass.Variable

    async def read_attribute(
        self, attribute: ua.AttributeIds, indexrange: str | None = None, raise_on_bad_status: bool = True
    ) -> ua.DataValue:
        self.read_calls.append(attribute)
        return self.current

    async def write_attribute(self, attribute: ua.AttributeIds, value: ua.DataValue) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.written.append((attribute, value))
        self.current = value


class _FakeClient:
    def __init__(self, node: Any) -> None:
        self.node = node

    def get_node(self, node_id: str) -> Any:
        return self.node


async def test_prepare_reads_the_previous_text() -> None:
    prepared = await prepare_attribute_write(_FakeClient(_FakeNode(_text("old name"))), "i=1", "DisplayName", "New")

    assert prepared.attribute_name == "DisplayName"
    assert prepared.attribute_id == ua.AttributeIds.DisplayName
    assert prepared.text == "New"
    assert prepared.previous_text == "old name"


async def test_the_attribute_name_is_case_insensitive() -> None:
    prepared = await prepare_attribute_write(_FakeClient(_FakeNode()), "i=1", "description", "text")

    assert prepared.attribute_name == "Description"
    assert prepared.attribute_id == ua.AttributeIds.Description


async def test_only_text_metadata_attributes_are_writable() -> None:
    for name in ("Value", "BrowseName", "NodeId", ""):
        with pytest.raises(PrepareError):
            await prepare_attribute_write(_FakeClient(_FakeNode()), "i=1", name, "text")


async def test_empty_text_is_refused() -> None:
    with pytest.raises(PrepareError):
        await prepare_attribute_write(_FakeClient(_FakeNode()), "i=1", "Description", "   ")


async def test_an_unreadable_node_is_refused() -> None:
    class _Broken(_FakeNode):
        async def read_node_class(self) -> ua.NodeClass:
            raise ua.UaError("BadNodeIdUnknown")

    with pytest.raises(PrepareError):
        await prepare_attribute_write(_FakeClient(_Broken()), "i=404", "Description", "text")


async def test_a_write_is_sent_once_and_verified() -> None:
    node = _FakeNode(_text("old"))
    prepared = await prepare_attribute_write(_FakeClient(node), "i=1", "Description", "boiler outlet")

    outcome = await send_prepared_attribute_write(prepared, authorization=AUTHORITY)

    assert outcome.status == "passed"
    assert outcome.sent is True
    assert outcome.write_status_code == "Good"
    assert outcome.read_back_text == "boiler outlet"
    assert outcome.previous_text == "old"
    assert len(node.written) == 1
    attribute, data_value = node.written[0]
    assert attribute == ua.AttributeIds.Description
    assert data_value.Value.Value == ua.LocalizedText(Text="boiler outlet")
    assert outcome.authorization is AUTHORITY


async def test_a_server_side_refusal_is_reported() -> None:
    node = _FakeNode(write_error=ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied))
    prepared = await prepare_attribute_write(_FakeClient(node), "i=1", "Description", "x")

    outcome = await send_prepared_attribute_write(prepared, authorization=AUTHORITY)

    assert outcome.status == "failed"
    assert outcome.sent is True
    assert outcome.write_status_code == "BadUserAccessDenied"
    assert "BadUserAccessDenied" in outcome.detail


async def test_a_differing_read_back_is_a_failure() -> None:
    class _Stubborn(_FakeNode):
        async def write_attribute(self, attribute: ua.AttributeIds, value: ua.DataValue) -> None:
            self.written.append((attribute, value))
            # The server accepted the call but kept its old text.

    node = _Stubborn(_text("unchanged"))
    prepared = await prepare_attribute_write(_FakeClient(node), "i=1", "DisplayName", "wanted")

    outcome = await send_prepared_attribute_write(prepared, authorization=AUTHORITY)

    assert outcome.status == "failed"
    assert "differs" in outcome.detail
    assert outcome.read_back_text == "unchanged"


async def test_an_unreadable_read_back_is_outcome_unknown() -> None:
    class _Vanish(_FakeNode):
        async def read_attribute(self, attribute: ua.AttributeIds, **kwargs: Any) -> ua.DataValue:
            if self.written:
                raise ua.UaError("BadSessionClosed")
            return self.current

    node = _Vanish(_text("old"))
    prepared = await prepare_attribute_write(_FakeClient(node), "i=1", "Description", "x")

    outcome = await send_prepared_attribute_write(prepared, authorization=AUTHORITY)

    assert outcome.status == "outcome_unknown"
    assert "unknown" in outcome.detail.lower()


async def test_the_convenience_wrapper_skips_an_invalid_request() -> None:
    outcome = await write_attribute_value(_FakeClient(_FakeNode()), "i=1", "Value", "1", authorization=AUTHORITY)

    assert outcome.status == "skipped"
    assert outcome.sent is False
    assert "not a writable text attribute" in outcome.detail
