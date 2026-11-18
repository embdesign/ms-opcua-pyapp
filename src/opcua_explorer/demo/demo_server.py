"""Local OPC UA demonstration server.

The model is shaped like a small plant and deliberately contains the structures
that a real address space has and naive tree browsers get wrong:

* a **shared child** (``SharedConfig``) referenced from two parents,
* a **cycle** (the motor organises the plant it belongs to),
* a **second namespace** so namespace filters can be exercised,
* variables of several types (bool, double, string, array, DateTime,
  ByteString, ExtensionObject), a writable variable, a method, a property and
  a **custom DataType**.

Credentials for the demo only: user ``demo`` / ``demo-pass``. These are
documented demo values, not secrets - but the application still never logs them.

Usage::

    uv run opcua-demo-server                      # opc.tcp://127.0.0.1:4840
    uv run opcua-demo-server --port 4841 --no-username
    uv run opcua-demo-server --cert server.der --key server.pem   # secure demo
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import socket
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from asyncua import Server, ua
from asyncua.server.user_managers import User, UserManager, UserRole

from .. import __version__

DEMO_NAMESPACE = "urn:opcua-explorer:demo"
DEMO_EXTRA_NAMESPACE = "urn:opcua-explorer:demo:extra"
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo-pass"

_logger = logging.getLogger(__name__)


class DemoUserManager(UserManager):
    """Anonymous access, one documented username/password pair, or a client certificate.

    ``certificate`` is the peer certificate of the secure channel; for an
    unencrypted channel asyncua passes an empty ``bytes`` value, so it must be
    tested for truthiness rather than for ``None``.
    """

    def __init__(self, *, allow_username: bool = True) -> None:
        self.allow_username = allow_username

    def get_user(
        self,
        iserver: Any,
        username: str | None = None,
        password: str | None = None,
        certificate: Any = None,
    ) -> User | None:
        if username is not None:
            if self.allow_username and username == DEMO_USERNAME and password == DEMO_PASSWORD:
                return User(role=UserRole.User, name=username)
            # Explicitly reject wrong credentials: returning None makes the server
            # answer ActivateSession with BadUserAccessDenied.
            return None
        if certificate:
            # A real server would validate the chain here; the demo trusts any
            # client certificate presented over a secure channel.
            return User(role=UserRole.User, name="certificate")
        return User(role=UserRole.User, name="anonymous")


@dataclass(slots=True)
class DemoModel:
    """Handles to the interesting nodes, used by tests and the value updater."""

    server: Server
    namespace_index: int
    extra_namespace_index: int
    endpoint: str
    objects: Any
    plant: Any
    line1: Any
    motor: Any
    temperature: Any
    pressure: Any
    speed: Any
    running: Any
    recipe: Any
    samples: Any
    serial_number: Any
    timestamp: Any
    blob: Any
    operating_range: Any
    shared_config: Any
    shared_threshold: Any
    extra_value: Any
    start_method: Any
    custom_data_type: Any
    event_generator: Any = None
    node_ids: dict[str, str] = field(default_factory=dict)


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


async def build_demo_server(
    endpoint: str = "opc.tcp://127.0.0.1:4840",
    *,
    allow_username: bool = True,
    certificate: str | None = None,
    private_key: str | None = None,
) -> DemoModel:
    """Create and initialise (but do not start) the demo server."""
    user_manager = DemoUserManager(allow_username=allow_username) if allow_username else None
    server = Server(user_manager=user_manager)
    await server.init()
    server.set_endpoint(endpoint)
    server.set_server_name("OPC UA Explorer Demo Server")
    # Must match the URI in the server certificate's subjectAltName when a
    # certificate is loaded, otherwise asyncua warns that clients may reject it.
    await server.set_application_uri("urn:opcua-explorer:demo:server")
    await server.set_build_info(
        product_uri="urn:opcua-explorer:demo:server",
        manufacturer_name="opcua-explorer",
        product_name="OPC UA Explorer Demo Server",
        software_version=__version__,
        build_number="1",
        build_date=datetime.now(UTC).replace(tzinfo=None),
    )

    if certificate and private_key:
        await server.load_certificate(certificate)
        await server.load_private_key(private_key)
        server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
        _logger.info("Demo server secured with Basic256Sha256 / SignAndEncrypt")

    index = await server.register_namespace(DEMO_NAMESPACE)
    extra_index = await server.register_namespace(DEMO_EXTRA_NAMESPACE)

    def node_id(name: str, namespace: int | None = None) -> ua.NodeId:
        return ua.NodeId(name, index if namespace is None else namespace)

    def qname(name: str, namespace: int | None = None) -> ua.QualifiedName:
        return ua.QualifiedName(name, index if namespace is None else namespace)

    objects = server.get_objects_node()

    plant = await objects.add_object(node_id("Plant"), qname("Plant"))
    line1 = await plant.add_object(node_id("Line1"), qname("Line1"))
    motor = await line1.add_object(node_id("Motor"), qname("Motor"))

    temperature = await line1.add_variable(node_id("Temperature"), qname("Temperature"), 21.5)
    await temperature.set_writable()
    pressure = await line1.add_variable(node_id("Pressure"), qname("Pressure"), 1013.25)
    await pressure.set_writable()
    recipe = await line1.add_variable(node_id("Recipe"), qname("Recipe"), "Line-1 standard")
    await recipe.set_writable()
    samples = await line1.add_variable(node_id("Samples"), qname("Samples"), [1.0, 2.5, 3.75])
    await samples.set_writable()
    timestamp = await line1.add_variable(node_id("Timestamp"), qname("Timestamp"), datetime.now(UTC))
    await timestamp.set_writable()
    blob = await line1.add_variable(node_id("Blob"), qname("Blob"), b"\x00\x01\x02\xff")
    await blob.set_writable()
    operating_range = await line1.add_variable(
        node_id("OperatingRange"),
        qname("OperatingRange"),
        ua.Range(Low=0.0, High=100.0),
        varianttype=ua.VariantType.ExtensionObject,
        datatype=ua.ObjectIds.Range,
    )

    speed = await motor.add_variable(node_id("Speed"), qname("Speed"), 0.0)
    await speed.set_writable()
    running = await motor.add_variable(node_id("Running"), qname("Running"), False)
    await running.set_writable()
    serial_number = await motor.add_property(node_id("SerialNumber"), qname("SerialNumber"), "SN-0001")

    async def start_motor(parent: Any, requested_speed: Any) -> list[ua.Variant]:
        # asyncua passes method arguments as Variants.
        if isinstance(requested_speed, ua.Variant):
            requested_speed = requested_speed.Value
        target = float(requested_speed)
        await speed.write_value(target)
        await running.write_value(True)
        return [ua.Variant(f"Motor started at {target:.0f}", ua.VariantType.String)]

    start_method = await motor.add_method(
        node_id("Start"),
        qname("Start"),
        start_motor,
        [ua.VariantType.Double],
        [ua.VariantType.String],
    )

    # A node referenced from two parents: a graph, not a tree.
    shared_config = await objects.add_object(node_id("SharedConfig"), qname("SharedConfig"))
    shared_threshold = await shared_config.add_variable(node_id("SharedThreshold"), qname("SharedThreshold"), 5.0)
    await shared_threshold.set_writable()
    await plant.add_reference(shared_config, ua.ObjectIds.Organizes)
    await line1.add_reference(shared_config, ua.ObjectIds.Organizes)

    # A cycle: the motor references the plant that contains it.
    await motor.add_reference(plant, ua.ObjectIds.Organizes)

    # A node in a different namespace, for namespace-filter tests.
    extra_object = await objects.add_object(
        ua.NodeId("Extra", extra_index), qname("Extra", extra_index)
    )
    extra_value = await extra_object.add_variable(
        ua.NodeId("ExtraValue", extra_index), qname("ExtraValue", extra_index), 42
    )

    # A custom (server-defined) DataType: appears in the exported type graph.
    # Properties are added explicitly because asyncua 2.1.0's `properties=`
    # shortcut reuses the DataType's own NodeId for every member node.
    custom_data_type = await server.create_custom_data_type(
        node_id("MeasurementType"),
        qname("MeasurementType"),
        ua.ObjectIds.BaseDataType,
        description="Demo structure with Value and Unit members",
    )
    await custom_data_type.add_property(
        node_id("MeasurementType_Value"), qname("Value"), 0.0, varianttype=ua.VariantType.Double
    )
    await custom_data_type.add_property(
        node_id("MeasurementType_Unit"), qname("Unit"), "", varianttype=ua.VariantType.String
    )

    model = DemoModel(
        server=server,
        namespace_index=index,
        extra_namespace_index=extra_index,
        endpoint=endpoint,
        objects=objects,
        plant=plant,
        line1=line1,
        motor=motor,
        temperature=temperature,
        pressure=pressure,
        speed=speed,
        running=running,
        recipe=recipe,
        samples=samples,
        serial_number=serial_number,
        timestamp=timestamp,
        blob=blob,
        operating_range=operating_range,
        shared_config=shared_config,
        shared_threshold=shared_threshold,
        extra_value=extra_value,
        start_method=start_method,
        custom_data_type=custom_data_type,
    )
    model.node_ids = {
        "objects": objects.nodeid.to_string(),
        "plant": plant.nodeid.to_string(),
        "line1": line1.nodeid.to_string(),
        "motor": motor.nodeid.to_string(),
        "temperature": temperature.nodeid.to_string(),
        "pressure": pressure.nodeid.to_string(),
        "speed": speed.nodeid.to_string(),
        "running": running.nodeid.to_string(),
        "recipe": recipe.nodeid.to_string(),
        "samples": samples.nodeid.to_string(),
        "serial_number": serial_number.nodeid.to_string(),
        "timestamp": timestamp.nodeid.to_string(),
        "blob": blob.nodeid.to_string(),
        "operating_range": operating_range.nodeid.to_string(),
        "shared_config": shared_config.nodeid.to_string(),
        "shared_threshold": shared_threshold.nodeid.to_string(),
        "extra_value": extra_value.nodeid.to_string(),
        "start_method": start_method.nodeid.to_string(),
        "custom_data_type": custom_data_type.nodeid.to_string(),
    }
    return model


async def _update_values(model: DemoModel, interval: float = 1.0) -> None:
    """Keep a couple of variables changing so subscriptions have something to report."""
    tick = 0.0
    while True:
        await asyncio.sleep(interval)
        tick += interval
        try:
            await model.temperature.write_value(round(21.5 + 2.0 * math.sin(tick / 10.0), 3))
            await model.speed.write_value(round(1500.0 + 200.0 * math.sin(tick / 5.0), 1))
            await model.running.write_value(int(tick) % 20 < 10)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the demo must survive client churn
            _logger.debug("Demo value update failed", exc_info=True)


async def _emit_events(model: DemoModel, interval: float = 1.0) -> None:
    """Publish one demo event per interval so an event subscription has something to show."""
    generator = model.event_generator
    if generator is None:  # pragma: no cover - only called after the generator exists
        return
    sequence = 0
    while True:
        await asyncio.sleep(interval)
        sequence += 1
        try:
            await generator.trigger(message=f"Demo event {sequence} from the OPC UA Explorer demo server")
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the demo must survive client churn
            _logger.debug("Demo event trigger failed", exc_info=True)


async def start_demo_server(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    allow_username: bool = True,
    certificate: str | None = None,
    private_key: str | None = None,
    update_values: bool = True,
    event_interval: float | None = None,
    history: bool = False,
) -> DemoModel:
    """Start a demo server and return its model (``model.endpoint`` is usable).

    ``event_interval`` (seconds) publishes a demo event on the Server object at
    that rate; it is off by default so ordinary runs stay deterministic.
    ``history`` historizes Temperature and Speed in the server's in-memory
    history store so raw HistoryRead can be exercised.
    """
    chosen_port = find_free_port(host) if port in (None, 0) else int(port)
    endpoint = f"opc.tcp://{host}:{chosen_port}"
    model = await build_demo_server(
        endpoint,
        allow_username=allow_username,
        certificate=certificate,
        private_key=private_key,
    )
    await model.server.start()
    if history:
        await model.server.historize_node_data_change(
            [model.temperature, model.speed], count=2_000
        )
    if update_values:
        task = asyncio.create_task(_update_values(model))
        model.server._demo_update_task = task
    if event_interval and float(event_interval) > 0:
        model.event_generator = await model.server.get_event_generator(
            ua.ObjectIds.BaseEventType, ua.ObjectIds.Server
        )
        event_task = asyncio.create_task(_emit_events(model, interval=float(event_interval)))
        model.server._demo_event_task = event_task
    _logger.info("Demo server listening on %s", endpoint)
    return model


async def stop_demo_server(model: DemoModel) -> None:
    for attribute in ("_demo_update_task", "_demo_event_task"):
        task = getattr(model.server, attribute, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    await model.server.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OPC UA Explorer demo server")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=4840, help="TCP port (0 = pick a free port)")
    parser.add_argument(
        "--no-username",
        action="store_true",
        help="disable the demo username/password identity (anonymous only)",
    )
    parser.add_argument(
        "--no-updates",
        action="store_true",
        help=(
            "keep Temperature/Speed/Running static instead of changing them every second; "
            "use this for the demo sequences and any value check that must be deterministic"
        ),
    )
    parser.add_argument(
        "--event-interval",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="publish one demo event on the Server object at this rate (0 = off, the default)",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="historize Temperature and Speed in memory so raw HistoryRead can be exercised",
    )
    parser.add_argument("--cert", help="server certificate (.der or .pem) to enable Basic256Sha256/SignAndEncrypt")
    parser.add_argument("--key", help="server private key (.pem)")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # asyncua logs thousands of lines while building the standard address space.
    logging.getLogger("asyncua").setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    async def runner() -> None:
        model = await start_demo_server(
            host=args.host,
            port=args.port,
            allow_username=not args.no_username,
            certificate=args.cert,
            private_key=args.key,
            update_values=not args.no_updates,
            event_interval=args.event_interval or None,
            history=args.history,
        )
        print(f"Demo server running at {model.endpoint}")
        print(f"  namespaces: {DEMO_NAMESPACE} (index {model.namespace_index}), {DEMO_EXTRA_NAMESPACE}")
        if not args.no_username:
            print(f"  identity:   anonymous, or username={DEMO_USERNAME!r} password={DEMO_PASSWORD!r}")
        print(f"  start node: Objects folder (i=85) or ns={model.namespace_index};s=Plant")
        if model.event_generator is not None:
            print(f"  events:     one demo event every {args.event_interval:g} s on the Server object (i=2253)")
        if args.history:
            print("  history:    Temperature and Speed are historized (raw HistoryRead only)")
        print("Press Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await stop_demo_server(model)
            print("Demo server stopped.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
