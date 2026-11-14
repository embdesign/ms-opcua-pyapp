"""PySide6 presentation layer.

The GUI never imports asyncua: it submits coroutines to
:class:`opcua_explorer.gui.async_runner.AsyncRunner` and renders plain model
objects returned through Qt signals.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Lazy entry point so importing the package does not require Qt."""
    from .app import main as _main

    return _main(argv)
