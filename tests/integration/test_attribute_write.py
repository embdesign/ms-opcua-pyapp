"""Attribute writes: refused by the demo server, accepted by an admin-role server."""

from __future__ import annotations

import pytest_asyncio
from asyncua import Server, ua
from asyncua.server.user_managers import User, UserManager, UserRole

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import free_port

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin-pass"


async def test_the_demo_server_refuses_metadata_writes(service, demo_model) -> None:
    preview = await service.attribute_write_preview(demo_model.node_ids["temperature"], "Description", "x")
    assert preview.ok is True

    outcome = await service.write_attribute_value(demo_model.node_ids["temperature"], "Description", "x")

    # asyncua's demo server allows non-Value writes for the Admin role only.
    assert outcome.status == "failed"
    assert outcome.sent is True
    assert outcome.write_status_code == "BadUserAccessDenied"
    assert outcome.previous_text == "Temperature"


async def test_an_invalid_attribute_never_reaches_the_server(service, demo_model) -> None:
    outcome = await service.write_attribute_value(demo_model.node_ids["temperature"], "Value", "1")

    assert outcome.status == "skipped"
    assert outcome.sent is False

    preview = await service.attribute_write_preview(demo_model.node_ids["temperature"], "Value", "1")
    assert preview.ok is False


class _AdminUserManager(UserManager):
    """One administrator identity, so non-Value writes are permitted."""

    def get_user(
        self,
        isession,  # noqa: ANN001 - asyncua signature
        username: str | None = None,
        password: str | None = None,
        certificate: bytes | None = None,
    ) -> User | None:
        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            return User(role=UserRole.Admin, name=ADMIN_USER)
        return None


@pytest_asyncio.fixture
async def admin_server(tmp_path):
    """A local server with an Admin identity and one writable variable."""
    server = Server(user_manager=_AdminUserManager())
    await server.init()
    port = free_port()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    index = await server.register_namespace("urn:opcua-explorer:test:attributes")
    obj = await server.nodes.objects.add_object(ua.NodeId("AttrTest", index), "AttrTest")
    variable = await obj.add_variable(ua.NodeId("AttrVar", index), "AttrVar", 1.0)
    await variable.set_writable()
    await server.start()

    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await service.connect(
        ConnectionSettings(
            endpoint_url=f"opc.tcp://127.0.0.1:{port}",
            auth_mode="username",
            username=ADMIN_USER,
            password=ADMIN_PASSWORD,
        )
    )
    try:
        yield service, variable
    finally:
        await service.shutdown()
        await server.stop()


async def test_an_admin_can_change_display_name_and_description(admin_server) -> None:
    service, variable = admin_server
    node_id = variable.nodeid.to_string()

    description = await service.write_attribute_value(node_id, "Description", "Boiler outlet")
    assert description.status == "passed", description.detail
    assert description.sent is True
    assert description.read_back_text == "Boiler outlet"

    display = await service.write_attribute_value(node_id, "DisplayName", "Outlet")
    assert display.status == "passed", display.detail
    assert display.read_back_text == "Outlet"
    assert (await variable.read_display_name()).Text == "Outlet"
    assert (await variable.read_description()).Text == "Boiler outlet"


async def test_empty_text_sends_nothing(admin_server) -> None:
    service, variable = admin_server

    outcome = await service.write_attribute_value(variable.nodeid.to_string(), "Description", "   ")

    assert outcome.status == "skipped"
    assert outcome.sent is False
    assert (await variable.read_description()).Text != "   "


async def test_a_bad_node_id_is_reported(admin_server) -> None:
    service, _ = admin_server

    outcome = await service.write_attribute_value("ns=9;i=9999", "Description", "x")

    assert outcome.status == "skipped"
    assert outcome.sent is False
