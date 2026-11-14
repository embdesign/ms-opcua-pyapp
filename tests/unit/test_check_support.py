"""Unit tests for the shared functional-check primitives."""

from __future__ import annotations

from opcua_explorer.models import STATUS_FAILED, STATUS_PASSED, TestCheck
from opcua_explorer.opcua.check_support import (
    CheckSkipped,
    environment_info,
    mark_failed,
    mark_passed,
    preview_value,
)


def test_mark_helpers_set_status_and_detail() -> None:
    check = TestCheck(id="x", name="X", category="test")
    mark_passed(check, "fine")
    assert check.status == STATUS_PASSED and check.detail == "fine"
    mark_failed(check, "broken")
    assert check.status == STATUS_FAILED and check.detail == "broken"


def test_preview_value_is_bounded_and_json_safe() -> None:
    assert preview_value(1) == "1"
    assert preview_value({"a": [1, 2]}) == '{"a": [1, 2]}'
    text = preview_value("x" * 500)
    assert len(text) <= 160 and text.endswith("...")


def test_environment_info_has_the_report_fields() -> None:
    info = environment_info()
    assert {"application", "asyncua", "pyside6", "python", "platform", "hostname"} <= set(info)


def test_check_skipped_is_an_exception() -> None:
    assert issubclass(CheckSkipped, Exception)
