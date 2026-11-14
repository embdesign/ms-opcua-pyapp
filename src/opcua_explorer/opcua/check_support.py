"""Shared primitives for the functional check suite.

The runner and the guarded write/method-call checks both need the same
"skipped" control flow, result markers, value previews and environment record.
Keeping them here avoids a circular import between ``test_runner`` and
``guarded_operations`` and gives the guarded checks no hidden dependency on the
runner's private attributes.
"""

from __future__ import annotations

import json
import platform
import sys
from importlib import metadata
from typing import Any

from ..models import STATUS_FAILED, STATUS_PASSED, TestCheck
from .serialize import SerializationContext, serialize_value


class CheckSkipped(Exception):
    """Raised by a check that cannot run; the reason is shown to the user."""


def mark_passed(check: TestCheck, detail: str) -> None:
    check.status = STATUS_PASSED
    check.detail = detail


def mark_failed(check: TestCheck, detail: str) -> None:
    check.status = STATUS_FAILED
    check.detail = detail


def preview_value(value: Any, limit: int = 160) -> str:
    """Compact, JSON-safe preview of a value for reports and check details."""
    context = SerializationContext()
    encoded = serialize_value(value, context)
    try:
        text = json.dumps(encoded, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - encoder output is JSON-safe
        text = str(encoded)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def environment_info() -> dict[str, str]:
    """Versions that matter when comparing two reports."""

    def _version(package: str) -> str:
        try:
            return metadata.version(package)
        except Exception:  # pragma: no cover - not installed / unusual packaging
            return "unknown"

    from .. import __version__

    return {
        "application": f"opcua-explorer {__version__}",
        "asyncua": _version("asyncua"),
        "pyside6": _version("PySide6"),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.platform(),
        "hostname": platform.node(),
    }
