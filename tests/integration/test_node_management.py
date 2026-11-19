"""Node management: refused by the demo server, accepted by an admin-role server."""

from __future__ import annotations

import pytest_asyncio
from asyncua import Server, ua
from asyncua.server.user_managers import User, UserManager, UserRole

from opcua_explorer.config import ConnectionSettings, TrustStore
from opcua_explorer.models import NodeCreateRequest
from opcua_explorer.opcua.client_service import OpcUaService

from .gui_support import free_port

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin-pass"


async def test_the_demo_server_refuses_node_management(service, demo_model) -> None:
    request = NodeCreateRequest(parent_node_id=str(demo_model.node_ids["plant"]), browse_name="NotAllowed")

    preview = await service.node_create_preview(request)
    assert preview.ok is True

    created = await service.create_node(request)
    assert created.status == "failed"
    assert created.sent is True
    assert created.status_code == "BadUserAccessDenied"

    deleted = await service.delete_node(str(demo_model.node_ids["temperature"]))
    assert deleted.status == "failed"
    assert deleted.status_code == "BadUserAccessDenied"


class _AdminUserManager(UserManager):
    """One administrator identity, so node management is permitted."""

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
async def admin_service(tmp_path):
    """A local server with an Admin identity and one parent object."""
    server = Server(user_manager=_AdminUserManager())
    await server.init()
    port = free_port()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    index = await server.register_namespace("urn:opcua-explorer:test:nodes")
    parent = await server.nodes.objects.add_object(ua.NodeId("Parent", index), "Parent")

    service = OpcUaService(trust_store=TrustStore(tmp_path / "trust.json"))
    await server.start()
    await service.connect(
        ConnectionSettings(
            endpoint_url=f"opc.tcp://127.0.0.1:{port}",
            auth_mode="username",
            username=ADMIN_USER,
            password=ADMIN_PASSWORD,
        )
    )
    try:
        yield service, parent
    finally:
        await service.shutdown()
        await server.stop()


async def test_an_admin_creates_and_deletes_an_object(admin_service) -> None:
    service, parent = admin_service
    parent_id = parent.nodeid.to_string()

    created = await service.create_node(
        NodeCreateRequest(parent_node_id=parent_id, browse_name="NewObject", display_name="New Object")
    )

    assert created.status == "passed", created.detail
    assert created.verified is True
    assert created.node_id
    assert (await service._require_client().get_node(created.node_id).read_display_name()).Text == "New Object"

    children = await service.browse_children(parent_id)
    assert "New Object" in {child.display_name for child in children}

    deleted = await service.delete_node(created.node_id)
    assert deleted.status == "passed", deleted.detail
    assert deleted.verified is True
    assert "New Object" not in {child.display_name for child in await service.browse_children(parent_id)}


async def test_an_admin_creates_a_variable_with_its_value(admin_service) -> None:
    service, parent = admin_service

    created = await service.create_node(
        NodeCreateRequest(
            parent_node_id=parent.nodeid.to_string(),
            browse_name="NewVariable",
            node_class="Variable",
            data_type="i=11",
            value_text="42.5",
        )
    )

    assert created.status == "passed", created.detail
    value = await service.read_value(created.node_id)
    assert value.value == 42.5
    assert value.status == "Good"

    assert (await service.delete_node(created.node_id)).status == "passed"


async def test_a_mismatched_value_creates_nothing(admin_service) -> None:
    service, parent = admin_service
    before = {child.node_id for child in await service.browse_children(parent.nodeid.to_string())}

    created = await service.create_node(
        NodeCreateRequest(
            parent_node_id=parent.nodeid.to_string(),
            browse_name="Broken",
            node_class="Variable",
            data_type="i=11",
            value_text="not-a-number",
        )
    )

    assert created.status == "skipped"
    assert created.sent is False
    after = {child.node_id for child in await service.browse_children(parent.nodeid.to_string())}
    assert after == before


async def test_deleting_an_unknown_node_is_reported(admin_service) -> None:
    service, _ = admin_service

    deleted = await service.delete_node("ns=9;i=4242")

    assert deleted.status == "failed"
    assert deleted.status_code == "BadNodeIdUnknown"
