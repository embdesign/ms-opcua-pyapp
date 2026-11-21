"""Metadata reads must distinguish an absent property from an unreadable one."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from asyncua.ua.uaerrors import BadNoMatch

from opcua_explorer.opcua.method_support import MetadataUnavailable, read_input_arguments


class _Property:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self._value = value
        self._error = error

    async def read_value(self):
        if self._error is not None:
            raise self._error
        return self._value


class _MethodNode:
    def __init__(self, child=None, error: Exception | None = None) -> None:
        self._child = child
        self._error = error

    async def get_child(self, names):  # noqa: ANN001 - test double
        if self._error is not None:
            raise self._error
        return self._child


class _Client:
    def __init__(self, node: _MethodNode) -> None:
        self._node = node

    def get_node(self, node_id: str) -> _MethodNode:
        return self._node


def _argument(name: str = "Value") -> SimpleNamespace:
    return SimpleNamespace(Name=name, DataType="i=11", ValueRank=-1)


async def test_missing_input_arguments_property_means_no_arguments() -> None:
    client = _Client(_MethodNode(error=BadNoMatch()))
    assert await read_input_arguments(client, "ns=2;s=Start") == []


async def test_unlocatable_property_is_not_reported_as_no_arguments() -> None:
    client = _Client(_MethodNode(error=RuntimeError("browse failed")))
    with pytest.raises(MetadataUnavailable, match="could not be located"):
        await read_input_arguments(client, "ns=2;s=Start")


async def test_unreadable_property_raises_metadata_unavailable() -> None:
    client = _Client(_MethodNode(child=_Property(error=RuntimeError("read failed"))))
    with pytest.raises(MetadataUnavailable, match="could not be read"):
        await read_input_arguments(client, "ns=2;s=Start")


async def test_valid_arguments_are_returned() -> None:
    argument = _argument()
    client = _Client(_MethodNode(child=_Property(value=[argument])))
    assert await read_input_arguments(client, "ns=2;s=Start") == [argument]


async def test_malformed_arguments_are_refused() -> None:
    client = _Client(_MethodNode(child=_Property(value=[SimpleNamespace(Name="missing fields")])))
    with pytest.raises(MetadataUnavailable, match="malformed"):
        await read_input_arguments(client, "ns=2;s=Start")
