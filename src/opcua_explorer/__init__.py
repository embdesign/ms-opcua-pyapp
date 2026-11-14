"""OPC UA Explorer - address-space browser, exporter and functional test client.

Provides connection/security handling, an on-demand address-space browser,
graph-correct address-space download, JSON/CSV/NodeSet2 export, live monitoring,
and functional tests with optional guarded write/method-call checks.

The package is intentionally layered so the GUI never talks to asyncua
directly:

``opcua_explorer.opcua``      OPC UA client service, traversal, serialization
``opcua_explorer.exporters``  file writers operating on in-memory snapshots
``opcua_explorer.gui``        PySide6 presentation layer
``opcua_explorer.demo``       local demonstration server
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

__all__ = ["__version__"]

# `pyproject.toml` is the single source of truth for the version; never repeat
# the number here. The fallback only applies to an interpreter that imported
# this package from a source tree that was never installed.
try:
    __version__ = _distribution_version("opcua-explorer")
except PackageNotFoundError:  # pragma: no cover - uninstalled source tree
    __version__ = "0.0.0+unknown"
