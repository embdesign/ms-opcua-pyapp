"""Unit tests for the one HistoryUpdate path (request shape, refusal, verification)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from asyncua import ua
from asyncua.ua.uaerrors import UaStatusCodeError

from opcua_explorer.models import (
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    HistoryUpdateConfig,
    HistoryUpdateEntry,
    WriteAuthority,
)
from opcua_explorer.opcua import history_service
from opcua_explorer.opcua.type_support import ValueType

AUTHORITY = WriteAuthority("operator-dialog", "unit-test")
START = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


def _double_type() -> ValueType:
    return ValueType(
        kind="builtin",
        data_type=ua.NodeId(ua.ObjectIds.Double),
        variant_type=ua.VariantType.Double,
        value_rank=-1,
    )


def _data_value(value: float, moment: datetime = START) -> ua.DataValue:
    data_value = ua.DataValue(ua.Variant(value, ua.VariantType.Double))
    data_value.SourceTimestamp = moment
    return data_value


class _Node:
    def __init__(self, node_class: ua.NodeClass = ua.NodeClass.Variable) -> None:
        self.node_class = node_class
        self.nodeid = ua.NodeId("Temperature", 2)

    async def read_node_class(self) -> ua.NodeClass:
        return self.node_class


class _Client:
    def __init__(self, node: _Node | None = None, session=None) -> None:
        self.node = node or _Node()
        self.uaclient = type("UaClient", (), {"session": session})()

    def get_node(self, _node_id: str) -> _Node:
        return self.node


class _Session:
    def __init__(self, results=None, error: Exception | None = None) -> None:
        self.results = results
        self.error = error
        self.requests: list[ua.HistoryUpdateParameters] = []

    async def history_update(self, params: ua.HistoryUpdateParameters):
        self.requests.append(params)
        if self.error is not None:
            raise self.error
        return self.results


def _update_result(status: ua.StatusCodes, operation: ua.StatusCodes = ua.StatusCodes.Good):
    result = ua.HistoryUpdateResult()
    result.StatusCode = ua.StatusCode(status)
    result.OperationResults = [ua.StatusCode(operation)]
    return [result]


def test_config_is_normalised_and_unknown_action_is_refused() -> None:
    config = HistoryUpdateConfig(
        action=" INSERT ",
        node_id=" ns=2;s=Temperature ",
        entries=[HistoryUpdateEntry(source_timestamp=START, value_text="21.5")],
    ).normalised()
    assert config.action == "insert"
    assert config.node_id == "ns=2;s=Temperature"

    with pytest.raises(history_service.PrepareHistoryError, match="not a history update action"):
        history_service.build_history_update_details(
            "ns=2;s=Temperature",
            HistoryUpdateConfig(action="append", node_id="ns=2;s=Temperature"),
            value_type=_double_type(),
            values=[_data_value(1.0)],
        )


def test_insert_and_replace_build_update_data_details() -> None:
    for action, expected in (("insert", ua.PerformUpdateType.Insert), ("replace", ua.PerformUpdateType.Replace)):
        config = HistoryUpdateConfig(
            action=action,
            node_id="ns=2;s=Temperature",
            entries=[HistoryUpdateEntry(source_timestamp=START, value_text="21.5")],
        )
        details = history_service.build_history_update_details(
            config.node_id, config, value_type=_double_type(), values=[_data_value(21.5)]
        )
        assert isinstance(details, ua.UpdateDataDetails)
        assert details.PerformInsertReplace == expected
        assert details.NodeId.to_string() == "ns=2;s=Temperature"
        assert details.UpdateValues[0].SourceTimestamp == START


def test_a_structure_uses_update_structure_data_details() -> None:
    structure = ValueType(
        kind="structure",
        data_type=ua.NodeId("MeasurementType", 2),
        variant_type=ua.VariantType.ExtensionObject,
        value_rank=-1,
    )
    config = HistoryUpdateConfig(
        action="replace",
        node_id="ns=2;s=Measurement",
        entries=[HistoryUpdateEntry(source_timestamp=START, value_text="1,2")],
    )

    details = history_service.build_history_update_details(
        config.node_id, config, value_type=structure, values=[_data_value(1.0)]
    )

    assert isinstance(details, ua.UpdateStructureDataDetails)


def test_delete_needs_a_range_and_carries_no_values() -> None:
    config = HistoryUpdateConfig(
        action="delete",
        node_id="ns=2;s=Temperature",
        start=START,
        end=datetime(2026, 11, 21, 11, 0, tzinfo=UTC),
    )

    details = history_service.build_history_update_details(config.node_id, config)

    assert isinstance(details, ua.DeleteRawModifiedDetails)
    assert details.IsDeleteModified is False
    assert details.StartTime == START
    with pytest.raises(history_service.PrepareHistoryError, match="start and an end"):
        history_service.build_history_update_details(
            "ns=2;s=Temperature", HistoryUpdateConfig(action="delete", node_id="ns=2;s=Temperature")
        )


async def test_prepare_parses_every_value_with_the_server_type(monkeypatch) -> None:
    async def fake_data_type(_client, _node_id):
        return ua.NodeId(ua.ObjectIds.Double), -1

    async def fake_type(_client, _data_type, _rank):
        return _double_type()

    monkeypatch.setattr(history_service, "read_node_data_type", fake_data_type)
    monkeypatch.setattr(history_service, "resolve_value_type", fake_type)
    config = HistoryUpdateConfig(
        action="insert",
        node_id="ns=2;s=Temperature",
        entries=[
            HistoryUpdateEntry(source_timestamp=START, value_text="21.5"),
            HistoryUpdateEntry(
                source_timestamp=datetime(2026, 11, 21, 10, 0, 1, tzinfo=UTC), value_text="22"
            ),
        ],
    )

    prepared = await history_service.prepare_history_update(_Client(), config)

    assert prepared.parsed == [21.5, 22.0]
    assert prepared.sent_value_count == 2
    assert prepared.details.UpdateValues[1].SourceTimestamp.minute == 0


async def test_a_bad_value_is_refused_before_sending(monkeypatch) -> None:
    async def fake_data_type(_client, _node_id):
        return ua.NodeId(ua.ObjectIds.Double), -1

    async def fake_type(_client, _data_type, _rank):
        return _double_type()

    monkeypatch.setattr(history_service, "read_node_data_type", fake_data_type)
    monkeypatch.setattr(history_service, "resolve_value_type", fake_type)
    config = HistoryUpdateConfig(
        action="insert",
        node_id="ns=2;s=Temperature",
        entries=[HistoryUpdateEntry(source_timestamp=START, value_text="not-a-number")],
    )

    with pytest.raises(history_service.PrepareHistoryError, match="not sent"):
        await history_service.prepare_history_update(_Client(), config)


async def test_a_non_variable_is_refused(monkeypatch) -> None:
    config = HistoryUpdateConfig(
        action="insert",
        node_id="ns=2;s=Temperature",
        entries=[HistoryUpdateEntry(source_timestamp=START, value_text="1")],
    )

    with pytest.raises(history_service.PrepareHistoryError, match="only a Variable"):
        await history_service.prepare_history_update(_Client(_Node(ua.NodeClass.Object)), config)


async def test_a_good_result_is_passed(monkeypatch) -> None:
    prepared = history_service.PreparedHistoryUpdate(
        node_id="ns=2;s=Temperature",
        config=HistoryUpdateConfig(action="insert", node_id="ns=2;s=Temperature"),
        details=ua.UpdateDataDetails(),
        parsed=[21.5],
        timestamps=[START],
    )
    session = _Session(_update_result(ua.StatusCodes.Good))

    outcome = await history_service.send_prepared_history_update(
        session, prepared, authorization=AUTHORITY
    )

    assert outcome.status == STATUS_PASSED
    assert outcome.sent is True
    assert outcome.entries_sent == 1
    assert len(session.requests) == 1


async def test_a_per_operation_refusal_fails_the_outcome() -> None:
    prepared = history_service.PreparedHistoryUpdate(
        node_id="ns=2;s=Temperature",
        config=HistoryUpdateConfig(action="replace", node_id="ns=2;s=Temperature"),
        details=ua.UpdateDataDetails(),
        parsed=[21.5],
        timestamps=[START],
    )
    session = _Session(_update_result(ua.StatusCodes.Good, ua.StatusCodes.BadNotWritable))

    outcome = await history_service.send_prepared_history_update(
        session, prepared, authorization=AUTHORITY
    )

    assert outcome.status == STATUS_FAILED
    assert "BadNotWritable" in outcome.detail


async def test_a_service_fault_is_reported_not_raised() -> None:
    prepared = history_service.PreparedHistoryUpdate(
        node_id="ns=2;s=Temperature",
        config=HistoryUpdateConfig(action="insert", node_id="ns=2;s=Temperature"),
        details=ua.UpdateDataDetails(),
        parsed=[21.5],
        timestamps=[START],
    )
    session = _Session(error=UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied))

    outcome = await history_service.send_prepared_history_update(
        session, prepared, authorization=AUTHORITY
    )

    assert outcome.status == STATUS_FAILED
    assert outcome.status_code == "BadUserAccessDenied"
    assert outcome.sent is True


async def test_an_unexpected_send_error_is_outcome_unknown() -> None:
    prepared = history_service.PreparedHistoryUpdate(
        node_id="ns=2;s=Temperature",
        config=HistoryUpdateConfig(action="insert", node_id="ns=2;s=Temperature"),
        details=ua.UpdateDataDetails(),
        parsed=[21.5],
        timestamps=[START],
    )
    session = _Session(error=OSError("link lost"))

    outcome = await history_service.send_prepared_history_update(
        session, prepared, authorization=AUTHORITY
    )

    assert outcome.status == STATUS_OUTCOME_UNKNOWN
    assert outcome.sent is True


async def test_a_prepare_error_sends_nothing_and_is_skipped(monkeypatch) -> None:
    async def fail(_client, _config):
        raise history_service.PrepareHistoryError("nothing was sent")

    monkeypatch.setattr(history_service, "prepare_history_update", fail)
    config = HistoryUpdateConfig(
        action="insert",
        node_id="ns=2;s=Temperature",
        entries=[HistoryUpdateEntry(source_timestamp=START, value_text="1")],
    )

    outcome = await history_service.history_update(_Client(), config, authorization=AUTHORITY)

    assert outcome.status == STATUS_SKIPPED
    assert outcome.sent is False
    assert "nothing was sent" in outcome.detail
