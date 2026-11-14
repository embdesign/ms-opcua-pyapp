"""Generate the sample exports in ``examples/`` from the local demo server.

    uv run python scripts/generate_example_exports.py

Values are excluded (the default), so the examples contain structure only.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

from opcua_explorer.config import ConnectionSettings
from opcua_explorer.demo.demo_server import start_demo_server, stop_demo_server
from opcua_explorer.exporters import export_csv, export_json
from opcua_explorer.models import TraversalConfig
from opcua_explorer.opcua.client_service import OpcUaService

MAX_NODES = 300
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
        snapshot = await service.download(
            ["i=85"],
            TraversalConfig(max_nodes=MAX_NODES, concurrency=4, batch_size=25),
        )

        EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        json_result = export_json(snapshot, EXAMPLES_DIR / "demo-address-space.json")
        csv_result = export_csv(snapshot, EXAMPLES_DIR / "demo-address-space.csv")

        print(f"endpoint         : {model.endpoint}")
        print(f"nodes            : {len(snapshot.nodes)}")
        print(f"references       : {len(snapshot.references)}")
        print(f"limits hit       : {', '.join(snapshot.limits_hit) or '-'}")
        print(f"JSON             : {json_result.path} ({json_result.bytes_written} bytes)")
        print(f"CSV              : {csv_result.path} ({csv_result.rows} rows)")
        return 0
    finally:
        await service.shutdown()
        await stop_demo_server(model)


def main() -> int:
    return asyncio.run(generate())


if __name__ == "__main__":
    sys.exit(main())
