"""Generate the sample NodeSet2 export in ``examples/`` from the local demo server.

    uv run python scripts/generate_example_nodeset.py

The example is exported from the demo's ``Objects`` folder with dependency
collection enabled. Type nodes created under ``Types`` (such as the demo's
custom ``MeasurementType``) are outside this snapshot; when a snapshot contains
a custom DataType without a ``DataTypeDefinition``, the exporter reports it as
unsupported instead of writing a node other servers cannot import.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

from opcua_explorer.config import ConnectionSettings
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.models import TraversalConfig
from opcua_explorer.opcua.client_service import OpcUaService
from opcua_explorer.opcua.nodeset2 import NodeSet2Config

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


async def generate() -> int:
    port = 4840 if _port_is_free(4840) else 0
    model = await start_demo_server(port=port, update_values=False)
    service = OpcUaService()
    try:
        await service.connect(ConnectionSettings(endpoint_url=model.endpoint))
        snapshot = await service.download(["i=85"], TraversalConfig(concurrency=4, batch_size=25))

        EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        xml_path = EXAMPLES_DIR / "demo-nodeset2.xml"
        result = await service.export_nodeset2(xml_path, snapshot, NodeSet2Config(include_values=False))

        manifest_path = EXAMPLES_DIR / "demo-nodeset2-manifest.json"
        manifest = result.to_dict()
        # Keep the committed example free of machine-specific absolute paths.
        manifest["path"] = xml_path.name
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")

        print(f"endpoint         : {model.endpoint}")
        print(f"nodes            : {result.nodes_written}")
        print(f"dependencies     : {result.dependency_nodes_added}")
        print(f"method args      : {result.argument_values_written}")
        print(f"dangling refs    : {len(result.dangling_references)}")
        print(f"validation       : {result.validation.status} (schema {result.validation.schema_status})")
        print(f"XML              : {xml_path} ({result.bytes_written} bytes)")
        print(f"manifest         : {manifest_path}")
        return 0
    finally:
        await service.shutdown()
        await stop_demo_server(model)


def main() -> int:
    return asyncio.run(generate())


if __name__ == "__main__":
    sys.exit(main())
