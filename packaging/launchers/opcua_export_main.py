"""Frozen entry point for ``opcua-export``, the headless NodeSet2 exporter.

It is Qt-free by design (the CLI never creates a ``QApplication``), which is why
it is built as its own bundle: no Qt platform plugin is needed to export an
address space, and the archive stays much smaller.
"""

from __future__ import annotations

from opcua_explorer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
