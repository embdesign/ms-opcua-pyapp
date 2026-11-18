"""Value read options against the demo server."""

from __future__ import annotations

import pytest

from opcua_explorer.errors import OpcUaExplorerError
from opcua_explorer.models import ValueReadOptions


async def test_plain_read_of_an_array(service, demo_model) -> None:
    result = await service.read_value_with_options(demo_model.node_ids["samples"])

    assert result.snapshot.status == "Good"
    assert result.snapshot.value == [1.0, 2.5, 3.75]
    assert result.snapshot.source_timestamp is not None
    assert "the server decides" in result.detail


async def test_an_index_range_is_accepted_and_reported(service, demo_model) -> None:
    options = ValueReadOptions(index_range="1:3", max_age_ms=0, timestamps="Source")

    result = await service.read_value_with_options(demo_model.node_ids["samples"], options)

    # asyncua's demo server does not implement IndexRange, so it returns the whole
    # array; the client reports what it asked for instead of pretending it sliced.
    assert result.snapshot.status == "Good"
    assert result.options.index_range == "1:3"
    assert "IndexRange 1:3 requested" in result.detail
    assert result.snapshot.value is not None


async def test_the_protocol_max_age_and_a_timestamp_choice_are_sent(service, demo_model) -> None:
    options = ValueReadOptions(max_age_ms=4_294_967_295, timestamps="Neither")

    result = await service.read_value_with_options(demo_model.node_ids["temperature"], options)

    assert result.snapshot.status == "Good"
    assert result.options.max_age_ms == 4_294_967_295
    assert result.options.timestamps == "Neither"


async def test_a_node_without_a_value_is_reported(service, demo_model) -> None:
    result = await service.read_value_with_options(demo_model.node_ids["plant"])

    assert result.snapshot.status != "Good"
    assert result.snapshot.encoding == "unavailable"


async def test_an_invalid_range_is_refused_locally(service, demo_model) -> None:
    with pytest.raises(OpcUaExplorerError) as excinfo:
        await service.read_value_with_options(
            demo_model.node_ids["samples"], ValueReadOptions(index_range="1:2:3")
        )
    assert "NumericRange" in str(excinfo.value)

    # The session is untouched and can still read.
    after = await service.read_value_with_options(demo_model.node_ids["samples"])
    assert after.snapshot.status == "Good"
