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


def format_duration(seconds: float | None) -> str:
    """Human-readable duration for diagnostics (``3 d 04:05:06`` / ``04:05:06``).

    ``None`` renders as an empty string; negative values are clamped to 0.
    """
    if seconds is None:
        return ""
    total = max(0, int(round(float(seconds))))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days} d {clock}" if days else clock
