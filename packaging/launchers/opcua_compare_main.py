"""Frozen entry point for ``opcua-compare``, the headless comparison command.

It is Qt-free by design (the command never creates a ``QApplication``), so it is
built as its own bundle: an address-space comparison must run on a machine with
no display server, and the archive stays much smaller than the GUI bundle.
"""

from __future__ import annotations

from opcua_explorer.cli_compare import main

if __name__ == "__main__":
    raise SystemExit(main())
