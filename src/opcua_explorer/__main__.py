"""``python -m opcua_explorer`` starts the desktop application."""

from __future__ import annotations

from .gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
