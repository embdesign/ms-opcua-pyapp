"""Fixtures for integration tests: a real demo server per test and one Qt app per module."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.opcua.client_service import OpcUaService

# Must be set before PySide6 is imported by a GUI test module.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    """One offscreen QApplication per GUI test module, destroyed after it."""
    from PySide6.QtWidgets import QApplication

    from opcua_explorer.gui.theme import apply_theme

    app = QApplication.instance() or QApplication(["pytest", "-platform", "offscreen"])
    apply_theme(app)
    yield app
    # Destroy the widgets before interpreter finalization. PySide6 aborts in GC
    # ("bool_dealloc ... refcount error in a C extension") when live widgets are
    # still referenced while Python shuts down.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    app.processEvents()


@pytest_asyncio.fixture
async def demo_model():
    """A fresh demo server on a free port."""
    model = await start_demo_server(update_values=False)
    try:
        yield model
    finally:
        await stop_demo_server(model)


@pytest_asyncio.fixture
async def service(tmp_path, demo_model):
    """An OPC UA service connected to the demo server, with an isolated trust store."""
    client_service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await client_service.connect(ConnectionSettings(endpoint_url=demo_model.endpoint))
    try:
        yield client_service
    finally:
        await client_service.shutdown()
