"""Helpers shared by the headless GUI integration tests."""

from __future__ import annotations

import socket
import subprocess
import sys
import time


def free_port() -> int:
    """Reserve a loopback port for one demo server."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_port(port: int, timeout: float = 120.0) -> None:
    """Wait until the demo server accepts connections on ``port``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise AssertionError(f"demo server did not start on port {port}")


def start_demo_server(port: int, *extra_args: str) -> subprocess.Popen:
    """Start the packaged demo server as a separate process."""
    return subprocess.Popen(
        [sys.executable, "-m", "opcua_explorer.demo.demo_server", "--port", str(port), *extra_args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_demo_server(process: subprocess.Popen) -> None:
    """Terminate a demo server process, escalating to kill if it does not exit."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()
        process.wait(timeout=10)


def connect_window(window, port: int, qapp, timeout: float = 45.0) -> None:
    """Drive the Connection tab to ``connected`` and wait for the GUI to reflect it."""
    wait_for_port(port)
    window.connection_tab.url_edit.setText(f"opc.tcp://127.0.0.1:{port}")
    window.connection_tab._on_connect()
    deadline = time.monotonic() + timeout
    while not window.connection_chip.text().startswith("Connected") and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.03)
    assert window.connection_chip.text().startswith("Connected"), window.connection_tab.error_label.text()
