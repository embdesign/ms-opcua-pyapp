"""Guarded write and method-call checks against the demo server."""

from __future__ import annotations

import pytest

from opcua_explorer.models import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    MethodCallTarget,
    TestConfig,
    WriteTarget,
)
from opcua_explorer.reporting import run_to_document


def _config(**kwargs) -> TestConfig:
    base = {
        "variable_node_id": None,
        "latency_samples": 1,
        "notification_wait_seconds": 1.0,
    }
    base.update(kwargs)
    return TestConfig(**base)


def _check(run, check_id: str):
    return next(check for check in run.checks if check.id == check_id)


@pytest.mark.asyncio
async def test_writes_are_skipped_unless_explicitly_allowed(service, demo_model):
    temperature = demo_model.node_ids["temperature"]
    config = _config(
        allow_writes=False,
        write_targets=[WriteTarget(node_id=temperature, value_text="42.5")],
        method_calls=[
            MethodCallTarget(
                object_node_id=demo_model.node_ids["motor"],
                method_node_id=demo_model.node_ids["start_method"],
                arguments=["900"],
            )
        ],
    )

    run = await service.run_functional_tests(config)

    write = _check(run, "write_value_1")
    call = _check(run, "method_call_1")
    assert write.status == STATUS_SKIPPED
    assert "disabled" in write.detail.lower()
    assert call.status == STATUS_SKIPPED
    assert "disabled" in call.detail.lower()
    # Nothing changed on the server.
    assert (await service.read_value(temperature)).value == pytest.approx(21.5)
    assert any("writes are disabled" in note for note in run.notes)


@pytest.mark.asyncio
async def test_guarded_write_is_verified_by_read_back(service, demo_model):
    temperature = demo_model.node_ids["temperature"]
    run = await service.run_functional_tests(
        _config(allow_writes=True, write_targets=[WriteTarget(node_id=temperature, value_text="42.5")])
    )

    write = _check(run, "write_value_1")
    assert write.status == STATUS_PASSED, write.detail
    assert write.data["data_type_name"] == "Double"
    assert write.data["parsed_value"] == "42.5"
    assert write.data["read_back"] == "42.5"
    assert "not restored" in write.data["restore"]
    assert write.data["confirmed_by_operator"] is True
    assert any(entry["operation"] == "Write" for entry in write.status_codes)
    assert (await service.read_value(temperature)).value == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_previous_value_is_only_restored_when_requested(service, demo_model):
    temperature = demo_model.node_ids["temperature"]
    run = await service.run_functional_tests(
        _config(
            allow_writes=True,
            write_targets=[
                WriteTarget(node_id=temperature, value_text="55.0", restore_previous_value=True)
            ],
        )
    )

    write = _check(run, "write_value_1")
    assert write.status == STATUS_PASSED, write.detail
    assert write.data["previous_value"] == "21.5"
    assert write.data["restore"] == "restored to 21.5"
    assert (await service.read_value(temperature)).value == pytest.approx(21.5)


@pytest.mark.asyncio
async def test_unparsable_value_is_skipped_and_nothing_is_written(service, demo_model):
    temperature = demo_model.node_ids["temperature"]
    run = await service.run_functional_tests(
        _config(allow_writes=True, write_targets=[WriteTarget(node_id=temperature, value_text="hot")])
    )

    write = _check(run, "write_value_1")
    assert write.status == STATUS_SKIPPED
    assert "not a number" in write.detail
    assert (await service.read_value(temperature)).value == pytest.approx(21.5)


@pytest.mark.asyncio
async def test_write_to_a_non_variable_is_refused(service, demo_model):
    run = await service.run_functional_tests(
        _config(
            allow_writes=True,
            write_targets=[WriteTarget(node_id=demo_model.node_ids["motor"], value_text="1")],
        )
    )

    write = _check(run, "write_value_1")
    assert write.status == STATUS_SKIPPED
    assert "not a Variable" in write.detail
    assert write.data["node_class"] == "Object"


@pytest.mark.asyncio
async def test_write_to_a_read_only_node_fails_with_a_status_code(service, demo_model):
    run = await service.run_functional_tests(
        _config(
            allow_writes=True,
            write_targets=[WriteTarget(node_id=demo_model.node_ids["serial_number"], value_text="SN-9999")],
        )
    )

    write = _check(run, "write_value_1")
    assert write.status == STATUS_FAILED
    assert "Bad" in write.detail
    assert any(entry["status"].startswith("Bad") for entry in write.status_codes)


@pytest.mark.asyncio
async def test_method_call_is_executed_with_validated_arguments(service, demo_model):
    run = await service.run_functional_tests(
        _config(
            allow_writes=True,
            method_calls=[
                MethodCallTarget(
                    object_node_id=demo_model.node_ids["motor"],
                    method_node_id=demo_model.node_ids["start_method"],
                    arguments=["900"],
                )
            ],
        )
    )

    call = _check(run, "method_call_1")
    assert call.status == STATUS_PASSED, call.detail
    assert call.data["signature"][0]["data_type_name"] == "Double"
    assert call.data["parsed_arguments"] == ["900.0"]
    # The demo method writes the requested speed, so the effect is observable.
    assert (await service.read_value(demo_model.node_ids["speed"])).value == pytest.approx(900.0)


@pytest.mark.asyncio
async def test_method_call_with_wrong_argument_count_is_skipped(service, demo_model):
    run = await service.run_functional_tests(
        _config(
            allow_writes=True,
            method_calls=[
                MethodCallTarget(
                    object_node_id=demo_model.node_ids["motor"],
                    method_node_id=demo_model.node_ids["start_method"],
                    arguments=[],
                )
            ],
        )
    )

    call = _check(run, "method_call_1")
    assert call.status == STATUS_SKIPPED
    assert "expects 1 argument(s)" in call.detail
    assert "was not called" in call.detail


@pytest.mark.asyncio
async def test_method_call_with_unparsable_argument_is_skipped(service, demo_model):
    run = await service.run_functional_tests(
        _config(
            allow_writes=True,
            method_calls=[
                MethodCallTarget(
                    object_node_id=demo_model.node_ids["motor"],
                    method_node_id=demo_model.node_ids["start_method"],
                    arguments=["fast"],
                )
            ],
        )
    )

    call = _check(run, "method_call_1")
    assert call.status == STATUS_SKIPPED
    assert "Argument 1" in call.detail
    # Unchanged: the demo method never ran.
    assert (await service.read_value(demo_model.node_ids["speed"])).value == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_read_only_run_stays_at_eleven_checks(service, demo_model):
    run = await service.run_functional_tests(_config())

    assert len(run.checks) == 11
    assert not any(check.id.startswith(("write_value", "method_call")) for check in run.checks)
    assert any("read-only" in note for note in run.notes)


@pytest.mark.asyncio
async def test_config_serialises_guarded_operations_for_the_report(service, demo_model):
    run = await service.run_functional_tests(
        _config(
            allow_writes=True,
            write_targets=[
                WriteTarget(node_id=demo_model.node_ids["temperature"], value_text="1.0", note="demo")
            ],
            method_calls=[
                MethodCallTarget(
                    object_node_id=demo_model.node_ids["motor"],
                    method_node_id=demo_model.node_ids["start_method"],
                    arguments=["300"],
                )
            ],
        )
    )

    document = run_to_document(run)
    config = document["configuration"]
    assert config["allow_writes"] is True
    assert config["write_targets"][0]["node_id"] == demo_model.node_ids["temperature"]
    assert config["method_calls"][0]["arguments"] == ["300"]
    assert document["summary"]["total"] == 13
