"""Unit tests for the universal sequence runner (no server, no Qt).

The fake device below is deliberately arbitrary: the application code must not know that the
gate happens to be an ``InitLock``/``ExitLock`` pair.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from asyncua import ua

from opcua_explorer.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PASSED,
    STATUS_SKIPPED,
    Sequence,
    SequenceGate,
    SequenceStep,
)
from opcua_explorer.opcua.sequence_runner import SequenceBusyError, SequenceRunner


class _Property:
    """A property node (``InputArguments``) with a fixed value."""

    def __init__(self, value: object) -> None:
        self._value = value

    async def read_value(self) -> object:
        return self._value


class FakeNode:
    def __init__(
        self,
        node_id: str,
        value: object = 0.0,
        *,
        data_type: ua.NodeId | None = None,
        value_rank: int = -1,
        node_class: ua.NodeClass | None = None,
        writable: bool = True,
        children: dict[str, object] | None = None,
        variant_type: ua.VariantType | None = None,
        definition: object | None = None,
    ) -> None:
        self.node_id = node_id
        self.nodeid = SimpleNamespace(to_string=lambda: node_id)
        self.value = value
        self.data_type = data_type if data_type is not None else ua.NodeId(ua.ObjectIds.Double)
        self.value_rank = value_rank
        self.node_class = node_class if node_class is not None else ua.NodeClass.Variable
        self.writable = writable
        self.children = dict(children or {})
        self.variant_type = variant_type
        self.definition = definition
        self.client: FakeClient | None = None
        self.write_gate: asyncio.Event | None = None

    async def read_node_class(self) -> ua.NodeClass:
        return self.node_class

    async def read_data_type(self) -> ua.NodeId:
        return self.data_type

    async def read_attribute(
        self, attribute: ua.AttributeIds, raise_on_bad_status: bool = True
    ) -> ua.DataValue:
        if attribute == ua.AttributeIds.ValueRank:
            return ua.DataValue(ua.Variant(self.value_rank, ua.VariantType.Int32))
        if attribute == ua.AttributeIds.Executable:
            return ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
        if attribute == ua.AttributeIds.UserAccessLevel:
            return ua.DataValue(ua.Variant(3, ua.VariantType.Byte))
        if attribute == ua.AttributeIds.DataTypeDefinition and self.definition is not None:
            return ua.DataValue(ua.Variant(self.definition))
        if not raise_on_bad_status:
            return ua.DataValue(ua.Variant(None))
        raise ua.UaStatusCodeError(ua.StatusCodes.BadAttributeIdInvalid)

    async def read_value(self) -> object:
        return self.value

    async def read_data_value(self) -> ua.DataValue:
        variant_type = self.variant_type or ua.datatype_to_varianttype(self.data_type)
        return ua.DataValue(ua.Variant(self.value, variant_type))

    async def write_value(self, data_value: ua.DataValue) -> None:
        if self.write_gate is not None:
            await self.write_gate.wait()
        if not self.writable:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadUserAccessDenied)
        self.value = data_value.Value.Value
        assert self.client is not None
        self.client.log.append(("write", self.node_id, self.value))

    async def get_child(self, path: list[str]) -> object:
        name = path[0]
        if name in self.children:
            return self.children[name]
        raise ua.UaStatusCodeError(ua.StatusCodes.BadNoMatch)

    async def call_method(self, method: FakeNode, *arguments: object) -> object:
        assert self.client is not None
        handler = self.client.methods.get(method.node_id)
        if handler is None:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadNothingToDo)
        self.client.log.append(("call", method.node_id, list(arguments)))
        result = handler(arguments)
        if asyncio.iscoroutine(result):
            return await result
        return result


class FakeClient:
    def __init__(
        self, nodes: dict[str, FakeNode], methods: dict[str, object] | None = None, session: object = None
    ) -> None:
        self.nodes = dict(nodes)
        self.methods = dict(methods or {})
        self.uaclient = SimpleNamespace(session=session)
        self.log: list[tuple[str, str, object]] = []
        for node in self.nodes.values():
            node.client = self

    def get_node(self, node_id: object) -> FakeNode:
        try:
            return self.nodes[str(node_id)]
        except KeyError:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown) from None

    @property
    def writes(self) -> list[tuple[str, str, object]]:
        return [entry for entry in self.log if entry[0] == "write"]

    @property
    def calls(self) -> list[tuple[str, str, object]]:
        return [entry for entry in self.log if entry[0] == "call"]


def _method_node(method_id: str, arguments: list[object] | None = None) -> FakeNode:
    node = FakeNode(method_id, node_class=ua.NodeClass.Method)
    node.children["InputArguments"] = _Property(list(arguments or []))
    return node


class Device:
    """A fake server with a lock object, a setpoint and a mode variable."""

    def __init__(self) -> None:
        self.voltage = FakeNode("ns=2;s=Voltage", 0.0)
        self.held = FakeNode("ns=2;s=LockHeld", False, data_type=ua.NodeId(ua.ObjectIds.Boolean))
        self.mode = FakeNode("ns=2;s=Mode", "Local", data_type=ua.NodeId(ua.ObjectIds.String))
        self.device = FakeNode("ns=2;s=Device", node_class=ua.NodeClass.Object)
        self.init = _method_node("ns=2;s=Lock.InitLock")
        self.exit = _method_node("ns=2;s=Lock.ExitLock")
        self.renew = _method_node("ns=2;s=Lock.RenewLock")
        self.set_limit = _method_node(
            "ns=2;s=Device.SetLimit",
            [SimpleNamespace(Name="Limit", DataType=ua.NodeId(ua.ObjectIds.Double), ValueRank=-1)],
        )
        self.lock = FakeNode(
            "ns=2;s=Lock",
            node_class=ua.NodeClass.Object,
            children={"InitLock": self.init, "ExitLock": self.exit, "RenewLock": self.renew},
        )
        self.device.children["SetLimit"] = self.set_limit
        self.client = FakeClient(
            {
                "ns=2;s=Voltage": self.voltage,
                "ns=2;s=LockHeld": self.held,
                "ns=2;s=Mode": self.mode,
                "ns=2;s=Device": self.device,
                "ns=2;s=Lock": self.lock,
                "ns=2;s=Lock.InitLock": self.init,
                "ns=2;s=Lock.ExitLock": self.exit,
                "ns=2;s=Lock.RenewLock": self.renew,
                "ns=2;s=Device.SetLimit": self.set_limit,
            },
            methods={
                "ns=2;s=Lock.InitLock": self._init_lock,
                "ns=2;s=Lock.ExitLock": self._exit_lock,
                "ns=2;s=Lock.RenewLock": self._renew_lock,
                "ns=2;s=Device.SetLimit": self._set_limit,
            },
        )

    def _init_lock(self, arguments: list[object]) -> None:
        self.held.value = True

    def _exit_lock(self, arguments: list[object]) -> None:
        self.held.value = False

    def _renew_lock(self, arguments: list[object]) -> None:
        return None

    def _set_limit(self, arguments: list[object]) -> None:
        return None


def _gate(*, verify: bool = True) -> SequenceGate:
    gate = SequenceGate(
        on=[SequenceStep(kind="call", id="lock", node_id="ns=2;s=Lock", method_name="InitLock")],
        off=[SequenceStep(kind="call", id="unlock", node_id="ns=2;s=Lock", method_name="ExitLock")],
    )
    if verify:
        gate.verify = SequenceStep(kind="expect", id="gate", node_id="ns=2;s=LockHeld", expected="true")
    return gate


def _sequence(*, verify: bool = True) -> Sequence:
    return Sequence(
        name="psu",
        gate=_gate(verify=verify),
        steps=[
            SequenceStep(kind="write", id="set", node_id="ns=2;s=Voltage", value="24.0", verify=True),
            SequenceStep(kind="expect", id="check", node_id="ns=2;s=Voltage", expected="24.0"),
        ],
    )


async def test_sequence_locks_writes_and_releases():
    device = Device()

    run = await SequenceRunner(device.client).run(_sequence())

    assert run.status == STATUS_PASSED, run.error
    assert run.gate_state == "released"
    assert [entry[0] for entry in device.client.log] == ["call", "write", "call"]
    assert device.client.writes == [("write", "ns=2;s=Voltage", 24.0)]
    assert [result.phase for result in run.results] == ["gate_on", "gate_verify", "step", "step", "gate_off"]


async def test_gate_is_released_when_a_body_step_fails():
    device = Device()
    device.voltage.writable = False

    run = await SequenceRunner(device.client).run(_sequence())

    assert run.status == STATUS_FAILED
    assert "BadUserAccessDenied" in run.error
    assert run.gate_state == "released"
    assert [entry[1] for entry in device.client.calls] == ["ns=2;s=Lock.InitLock", "ns=2;s=Lock.ExitLock"]
    assert device.client.writes == []


async def test_gate_verify_blocks_the_body():
    device = Device()
    device.client.methods["ns=2;s=Lock.InitLock"] = lambda arguments: None  # refuses silently

    run = await SequenceRunner(device.client).run(_sequence())

    assert run.status == STATUS_FAILED
    assert "Expectation not met" in run.error
    assert device.client.writes == []
    assert [entry[1] for entry in device.client.calls] == ["ns=2;s=Lock.InitLock", "ns=2;s=Lock.ExitLock"]


async def test_optional_ensure_on_a_missing_node_is_skipped():
    device = Device()
    sequence = Sequence(
        name="mode",
        steps=[
            SequenceStep(
                kind="ensure", id="remote", node_id="ns=2;s=OperationMode", value="Remote", optional=True
            ),
            SequenceStep(kind="write", id="set", node_id="ns=2;s=Voltage", value="24.0", verify=True),
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_PASSED, run.error
    remote = next(result for result in run.results if result.step_id == "remote")
    assert remote.status == STATUS_SKIPPED
    assert "does not exist" in remote.detail
    assert device.client.writes == [("write", "ns=2;s=Voltage", 24.0)]


async def test_ensure_does_not_write_an_already_matching_value():
    device = Device()
    sequence = Sequence(
        name="mode",
        steps=[SequenceStep(kind="ensure", id="mode", node_id="ns=2;s=Mode", value="Local")],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_PASSED, run.error
    assert run.results[0].status == STATUS_SKIPPED
    assert "already has" in run.results[0].detail
    assert device.client.writes == []


async def test_dry_run_sends_nothing():
    device = Device()

    run = await SequenceRunner(device.client).run(_sequence(), dry_run=True)

    assert run.dry_run is True
    assert run.status == STATUS_PASSED, run.error
    assert device.client.log == []
    assert all(result.status == STATUS_SKIPPED for result in run.results)
    assert "release steps were not sent" in run.gate_state


async def test_a_second_sequence_is_refused_while_one_runs():
    device = Device()
    runner = SequenceRunner(device.client)
    slow = Sequence(name="slow", steps=[SequenceStep(kind="delay", id="wait", seconds=0.3)])

    task = asyncio.create_task(runner.run(slow))
    await asyncio.sleep(0.02)
    try:
        with pytest.raises(SequenceBusyError):
            await runner.run(Sequence(name="other"))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_keepalive_failure_aborts_before_the_next_step():
    device = Device()

    def failing_renew(arguments: list[object]) -> None:
        raise ua.UaStatusCodeError(ua.StatusCodes.BadNotAllowed)

    device.client.methods["ns=2;s=Lock.RenewLock"] = failing_renew
    gate = _gate(verify=False)
    gate.keepalive = SequenceStep(kind="call", id="renew", node_id="ns=2;s=Lock", method_name="RenewLock")
    gate.keepalive_every_seconds = 0.05
    sequence = Sequence(
        name="keepalive",
        gate=gate,
        steps=[
            SequenceStep(kind="delay", id="settle", seconds=0.35),
            SequenceStep(kind="write", id="set", node_id="ns=2;s=Voltage", value="24.0"),
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_FAILED
    assert "keepalive" in run.error.lower()
    assert device.client.writes == []
    assert ("call", "ns=2;s=Lock.ExitLock", []) in device.client.calls


async def test_keepalive_can_be_configured_to_warn_only():
    device = Device()

    def failing_renew(arguments: list[object]) -> None:
        raise ua.UaStatusCodeError(ua.StatusCodes.BadNotAllowed)

    device.client.methods["ns=2;s=Lock.RenewLock"] = failing_renew
    gate = _gate(verify=False)
    gate.keepalive = SequenceStep(kind="call", id="renew", node_id="ns=2;s=Lock", method_name="RenewLock")
    gate.keepalive_every_seconds = 0.05
    gate.keepalive_on_failure = "warn"
    sequence = Sequence(
        name="warn",
        gate=gate,
        steps=[
            SequenceStep(kind="delay", id="settle", seconds=0.25),
            SequenceStep(kind="write", id="set", node_id="ns=2;s=Voltage", value="24.0", verify=True),
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert device.client.writes == [("write", "ns=2;s=Voltage", 24.0)]
    assert run.gate_state == "released"
    assert run.status == STATUS_FAILED  # the failed renewal is reported, not hidden


async def test_cancellation_during_a_write_is_reported_as_outcome_unknown():
    device = Device()
    device.voltage.write_gate = asyncio.Event()
    runner = SequenceRunner(device.client)

    task = asyncio.create_task(runner.run(_sequence(verify=False)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run = runner.last_run
    assert run is not None
    assert run.status == STATUS_CANCELLED
    assert any(result.status == STATUS_OUTCOME_UNKNOWN for result in run.results)
    assert run.gate_state == "released"
    assert ("call", "ns=2;s=Lock.ExitLock", []) in device.client.calls


async def test_cancellation_through_the_token_is_reported_as_cancelled():
    device = Device()
    cancel = asyncio.Event()
    sequence = Sequence(name="stop", steps=[SequenceStep(kind="delay", id="wait", seconds=1.0)])
    runner = SequenceRunner(device.client)

    task = asyncio.create_task(runner.run(sequence, cancel=cancel))
    await asyncio.sleep(0.02)
    cancel.set()
    run = await task

    assert run.status == STATUS_CANCELLED
    assert "Cancelled by the operator" in run.error


async def test_inputs_are_substituted_and_caller_values_win():
    device = Device()
    sequence = Sequence(
        name="inputs",
        inputs={"v": "24.0"},
        steps=[SequenceStep(kind="write", id="set", node_id="ns=2;s=Voltage", value="${v}", verify=True)],
    )

    run = await SequenceRunner(device.client).run(sequence, inputs={"v": "12.5"})

    assert run.status == STATUS_PASSED, run.error
    assert run.inputs["v"] == "12.5"
    assert device.client.writes == [("write", "ns=2;s=Voltage", 12.5)]


async def test_a_missing_input_fails_the_step_before_anything_is_sent():
    device = Device()
    sequence = Sequence(
        name="inputs",
        steps=[SequenceStep(kind="write", id="set", node_id="ns=2;s=Voltage", value="${nope}")],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_FAILED
    assert "Missing sequence input" in run.error
    assert device.client.log == []


async def test_method_arguments_are_typed_from_input_arguments():
    device = Device()
    sequence = Sequence(
        name="call",
        steps=[
            SequenceStep(
                kind="call",
                id="limit",
                node_id="ns=2;s=Device",
                method_node_id="ns=2;s=Device.SetLimit",
                arguments=["21.5"],
            )
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_PASSED, run.error
    assert device.client.calls == [("call", "ns=2;s=Device.SetLimit", [21.5])]


async def test_abort_on_failure_false_continues_but_reports_the_failure():
    device = Device()
    device.voltage.writable = False
    sequence = Sequence(
        name="continue",
        abort_on_failure=False,
        steps=[
            SequenceStep(kind="write", id="bad", node_id="ns=2;s=Voltage", value="1.0", verify=True),
            SequenceStep(kind="write", id="good", node_id="ns=2;s=Mode", value="Remote", verify=True),
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_FAILED
    assert device.client.writes == [("write", "ns=2;s=Mode", "Remote")]


async def test_wait_until_times_out_with_the_last_value():
    device = Device()
    sequence = Sequence(
        name="wait",
        steps=[
            SequenceStep(
                kind="wait_until",
                id="remote",
                node_id="ns=2;s=Mode",
                expected="Remote",
                timeout_seconds=0.15,
                poll_seconds=0.05,
            )
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_FAILED
    assert "did not reach" in run.error
    assert "Local" in run.error


async def test_wait_until_passes_when_the_condition_already_holds():
    device = Device()
    sequence = Sequence(
        name="wait",
        steps=[
            SequenceStep(
                kind="wait_until", id="remote", node_id="ns=2;s=Mode", expected="Local", timeout_seconds=1.0
            )
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_PASSED, run.error
    assert run.results[0].status == STATUS_PASSED


async def test_an_unknown_step_kind_fails_before_anything_is_sent():
    device = Device()
    sequence = Sequence(name="unknown", steps=[SequenceStep(kind="explode", id="bad")])

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_FAILED
    assert "Unsupported step kind" in run.error
    assert device.client.log == []


async def test_an_enumeration_value_can_be_written_by_name():
    device = Device()
    definition = ua.EnumDefinition(
        Fields=[
            ua.EnumField(Value=0, DisplayName=ua.LocalizedText("Local"), Description=ua.LocalizedText("")),
            ua.EnumField(Value=1, DisplayName=ua.LocalizedText("Remote"), Description=ua.LocalizedText("")),
        ]
    )
    mode = FakeNode(
        "ns=2;s=OperationMode",
        0,
        data_type=ua.NodeId(3001, 2),
        variant_type=ua.VariantType.Int32,
    )
    device.client.nodes["ns=2;s=OperationMode"] = mode
    device.client.nodes["ns=2;i=3001"] = FakeNode(
        "ns=2;i=3001",
        data_type=ua.NodeId(3001, 2),
        node_class=ua.NodeClass.DataType,
        definition=definition,
    )
    for node in (mode, device.client.nodes["ns=2;i=3001"]):
        node.client = device.client
    sequence = Sequence(
        name="enum",
        steps=[SequenceStep(kind="write", id="mode", node_id="ns=2;s=OperationMode", value="Remote", verify=True)],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_PASSED, run.error
    assert mode.value == 1
    assert run.results[0].new_value == "1"


class PathSession:
    """A session stub answering TranslateBrowsePathsToNodeIds from a name->NodeId map."""

    def __init__(self, mapping: dict[str, str] | None = None, *, ambiguous: bool = False) -> None:
        self.mapping = dict(mapping or {})
        self.ambiguous = ambiguous
        self.queries: list[str] = []

    async def translate_browsepaths_to_nodeids(self, browse_paths: list[ua.BrowsePath]) -> list[ua.BrowsePathResult]:
        results = []
        for path in browse_paths:
            key = "/".join(
                f"{element.TargetName.NamespaceIndex}:{element.TargetName.Name}"
                for element in path.RelativePath.Elements
            )
            self.queries.append(key)
            if self.ambiguous:
                results.append(
                    ua.BrowsePathResult(
                        StatusCode=ua.StatusCode(ua.StatusCodes.Good),
                        Targets=[
                            ua.BrowsePathTarget(TargetId=ua.ExpandedNodeId(1, 2), RemainingPathIndex=0xFFFFFFFF),
                            ua.BrowsePathTarget(TargetId=ua.ExpandedNodeId(2, 2), RemainingPathIndex=0xFFFFFFFF),
                        ],
                    )
                )
                continue
            node_id = self.mapping.get(key)
            if node_id is None:
                results.append(
                    ua.BrowsePathResult(StatusCode=ua.StatusCode(ua.StatusCodes.BadNoMatch), Targets=[])
                )
                continue
            node = ua.NodeId.from_string(node_id)
            results.append(
                ua.BrowsePathResult(
                    StatusCode=ua.StatusCode(ua.StatusCodes.Good),
                    Targets=[
                        ua.BrowsePathTarget(
                            TargetId=ua.ExpandedNodeId(node.Identifier, node.NamespaceIndex, node.NodeIdType),
                            RemainingPathIndex=0xFFFFFFFF,
                        )
                    ],
                )
            )
        return results


async def test_a_step_can_address_its_node_by_browse_path():
    device = Device()
    session = PathSession({"2:Voltage": "ns=2;s=Voltage"})
    device.client.uaclient.session = session
    sequence = Sequence(
        name="path",
        steps=[
            SequenceStep(
                kind="write", id="set", node_path=["2:Voltage"], path_root="i=85", value="3.5", verify=True
            )
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_PASSED, run.error
    assert device.client.writes == [("write", "ns=2;s=Voltage", 3.5)]
    assert session.queries == ["2:Voltage"]


async def test_an_unresolved_browse_path_fails_before_anything_is_sent():
    device = Device()
    device.client.uaclient.session = PathSession()
    sequence = Sequence(
        name="path",
        steps=[SequenceStep(kind="write", id="set", node_path=["2:Missing"], value="3.5")],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_FAILED
    assert "not resolved" in run.error
    assert device.client.log == []


async def test_an_ambiguous_browse_path_fails_before_anything_is_sent():
    device = Device()
    device.client.uaclient.session = PathSession(ambiguous=True)
    sequence = Sequence(
        name="path",
        steps=[SequenceStep(kind="write", id="set", node_path=["2:Duplicate"], value="3.5")],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_FAILED
    assert "matched 2 nodes" in run.error
    assert device.client.log == []


async def test_an_optional_ensure_with_an_unresolved_path_is_skipped():
    device = Device()
    device.client.uaclient.session = PathSession()
    sequence = Sequence(
        name="path",
        steps=[
            SequenceStep(kind="ensure", id="mode", node_path=["2:OperationMode"], value="Remote", optional=True),
            SequenceStep(kind="write", id="set", node_id="ns=2;s=Voltage", value="3.5", verify=True),
        ],
    )

    run = await SequenceRunner(device.client).run(sequence)

    assert run.status == STATUS_PASSED, run.error
    assert run.results[0].status == STATUS_SKIPPED
    assert "optional step was skipped" in run.results[0].detail
    assert device.client.writes == [("write", "ns=2;s=Voltage", 3.5)]
