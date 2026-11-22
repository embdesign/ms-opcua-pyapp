"""NodeId normalisation shared by the service and its collaborators."""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_explorer.errors import InvalidNodeIdError
from opcua_explorer.opcua.node_ids import normalise_node_id


def test_normalises_string_integer_and_node_id() -> None:
    assert normalise_node_id("ns=2;s=Temperature") == "ns=2;s=Temperature"
    assert normalise_node_id(85) == "i=85"
    assert normalise_node_id(ua.NodeId(85)) == "i=85"
    assert normalise_node_id(ua.NodeId("Temperature", 2)) == "ns=2;s=Temperature"


def test_invalid_input_is_refused() -> None:
    with pytest.raises(InvalidNodeIdError):
        normalise_node_id(1.5)
