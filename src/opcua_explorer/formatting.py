"""Text formatting helpers shared by exporters and value encoding.

This module deliberately imports neither asyncua nor Qt. ``exporters/`` must not
depend on the OPC UA client layer (see ``docs/architecture.md``), so the CSV
formatting it needs lives here rather than in ``opcua.serialize``.
"""

from __future__ import annotations

import json
from typing import Any


def safe_repr(value: Any, limit: int = 200) -> str:
    """``repr()`` that never raises and never returns more than ``limit`` characters."""
    try:
        text = repr(value)
    except Exception:  # pragma: no cover - pathological __repr__
        text = f"<unrepresentable {type(value).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def flatten_for_csv(value: Any) -> str:
    """Compact single-cell representation used by the CSV exporter."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - serializer output is JSON-safe
        return safe_repr(value)
