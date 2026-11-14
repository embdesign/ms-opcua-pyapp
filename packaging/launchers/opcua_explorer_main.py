"""Frozen entry point for the OPC UA Explorer desktop bundle.

PyInstaller analyses this file as a plain script, so it must not rely on package
relative imports. The GUI bootstrap starts the asyncio worker thread, builds the
window and shuts both down in its ``finally`` block.
"""

from __future__ import annotations

from opcua_explorer.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
