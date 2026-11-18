"""Interactive write and method call against the demo server."""

from __future__ import annotations

import pytest

from opcua_explorer.errors import InvalidNodeIdError


async def test_write_and_read_back(service, demo_model) -> None:
    result = await service.write_node_value(demo_model.node_ids["temperature"], "42.5")

    assert result.status == "passed"
    assert result.write.sent is True
    assert result.write.new_value == "42.5"
    assert result.write.read_back_value == "42.5"
    assert result.write.write_status_code == "Good"
    assert result.write.previous_known is True
    assert "read it back" in result.summary_text()
    assert await demo_model.temperature.read_value() == 42.5


async def test_restore_is_a_second_verified_write(service, demo_model) -> None:
    await demo_model.temperature.write_value(21.5)

    result = await service.write_node_value(
        demo_model.node_ids["temperature"], "43.0", restore_previous=True
    )

    assert result.status == "passed"
    assert result.restore is not None
    assert result.restore.sent is True
    assert result.restore.written_value == "21.5"
    assert await demo_model.temperature.read_value() == 21.5


async def test_a_refused_value_sends_nothing(service, demo_model) -> None:
    await demo_model.temperature.write_value(10.0)

    result = await service.write_node_value(demo_model.node_ids["temperature"], "not-a-number")

    assert result.status == "skipped"
    assert result.write.sent is False
    assert "Double" in result.summary_text() or "value" in result.summary_text().lower()
    assert await demo_model.temperature.read_value() == 10.0


async def test_a_non_variable_is_refused(service, demo_model) -> None:
    result = await service.write_node_value(demo_model.node_ids["plant"], "1")

    assert result.status == "skipped"
    assert result.write.sent is False


async def test_preview_reports_type_and_nothing_is_sent(service, demo_model) -> None:
    await demo_model.temperature.write_value(10.0)

    inspection = await service.write_preview(demo_model.node_ids["temperature"], "7.5")

    assert inspection.ok is True
    assert inspection.preview == "7.5"
    assert inspection.data_type_label == "Double"
    assert inspection.value_rank == -1
    assert await demo_model.temperature.read_value() == 10.0

    refused = await service.write_preview(demo_model.node_ids["temperature"], "abc")
    assert refused.ok is False
    assert refused.detail


async def test_method_signature_and_one_call(service, demo_model) -> None:
    rows = await service.method_signature(demo_model.node_ids["start_method"])

    assert len(rows) == 1
    assert rows[0]["data_type_name"] == "Double"
    assert rows[0]["value_rank"] == -1

    preview = await service.call_preview(
        demo_model.node_ids["motor"], demo_model.node_ids["start_method"], ["900"]
    )
    assert preview.ok is True

    outcome = await service.invoke_method(
        demo_model.node_ids["motor"], demo_model.node_ids["start_method"], ["900"]
    )

    assert outcome.status == "passed"
    assert outcome.sent is True
    assert outcome.status_code == "Good"
    assert outcome.output_values == ["Motor started at 900"]
    assert "Called" in outcome.detail


async def test_a_call_with_the_wrong_argument_count_sends_nothing(service, demo_model) -> None:
    outcome = await service.invoke_method(
        demo_model.node_ids["motor"], demo_model.node_ids["start_method"], []
    )

    assert outcome.status == "skipped"
    assert outcome.sent is False
    assert "argument" in outcome.detail.lower()

    preview = await service.call_preview(
        demo_model.node_ids["motor"], demo_model.node_ids["start_method"], []
    )
    assert preview.ok is False


async def test_invalid_node_ids_are_rejected(service) -> None:
    with pytest.raises(InvalidNodeIdError):
        await service.write_node_value("not a node id", "1")
    with pytest.raises(InvalidNodeIdError):
        await service.invoke_method("i=85", "not a node id", [])
