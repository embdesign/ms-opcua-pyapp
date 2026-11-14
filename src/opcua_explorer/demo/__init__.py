"""Local demonstration OPC UA server used by the GUI, README and tests.

Import from :mod:`opcua_explorer.demo.demo_server` directly; this package does
not re-export it so that ``python -m opcua_explorer.demo.demo_server`` runs
cleanly (runpy would otherwise execute the module twice).
"""
