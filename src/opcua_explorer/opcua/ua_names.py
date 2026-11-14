"""Conversions from asyncua enum values to the names used in models and reports.

These helpers used to be copied into several modules (browse, traversal,
client_service, test_runner, serialize, monitor_service, attributes). Keeping
one implementation guarantees that the same StatusCode or NodeClass is rendered
identically in the GUI, the exports and the test reports.

Every conversion is defensive: a malformed or unknown value is rendered with
``str()`` instead of raising, because these names end up in diagnostics for
servers that returned something unexpected.
"""

from __future__ import annotations

from typing import Any

from asyncua import ua


def node_class_name(value: Any) -> str:
    """Return the ``ua.NodeClass`` name (for example ``"Variable"``)."""
    try:
        return ua.NodeClass(int(value)).name
    except Exception:
        return str(value)


def status_name(code: Any) -> str:
    """Return the ``ua.StatusCode`` name (for example ``"BadUserAccessDenied"``)."""
    try:
        return ua.StatusCode(int(getattr(code, "value", code))).name
    except Exception:
        return str(code)


def variant_type_name(variant_type: Any) -> str:
    """Return the ``ua.VariantType`` name (for example ``"Double"``)."""
    try:
        return ua.VariantType(int(variant_type)).name
    except Exception:
        return str(variant_type)
