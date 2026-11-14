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

__version__ = "0.1.0"
__all__ = ["__version__"]
