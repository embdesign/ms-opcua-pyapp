"""Architecture guards: layer boundaries that must not regress.

``docs/architecture.md`` §2 declares the pure core Qt-free and asyncua-free: ``models``,
``config``, ``defaults``, ``project``, ``formatting``, ``operator_text``,
``sequence_document``, ``reporting``, ``compare``, ``trend`` and ``exporters``. Each module is
imported in its own subprocess, so the probe observes exactly what importing that one
module (and its dependencies) pulls in, and a regression names the offending module.

The write guards enforce the other documented invariant the same way: there is exactly one
write/method-call/restore implementation (``opcua/write_service.py``), it stays transport-neutral,
and every send states its authority.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from opcua_explorer.opcua import node_management, write_service

#: Modules documented as importing neither asyncua nor Qt (``docs/architecture.md`` §2).
_PURE_MODULES = (
    "opcua_explorer.models",
    "opcua_explorer.config",
    "opcua_explorer.defaults",
    "opcua_explorer.project",
    "opcua_explorer.formatting",
    "opcua_explorer.operator_text",
    "opcua_explorer.sequence_document",
    "opcua_explorer.reporting",
    "opcua_explorer.compare",
    "opcua_explorer.trend",
    "opcua_explorer.exporters",
)

_PROBE = """
import importlib
import sys

importlib.import_module(sys.argv[1])
forbidden = ("asyncua", "PySide6", "PySide2", "shiboken6")
print(",".join(name for name in forbidden if name in sys.modules))
"""

#: Repository-relative package root, independent of the working directory.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "opcua_explorer"

#: The only module that may send a write, a method call or a restore.
_SEND_MODULE = "opcua/write_service.py"
#: The only module that may send AddNodes/DeleteNodes.
_STRUCTURE_MODULE = "opcua/node_management.py"
#: The demo server writes its own local model and is never a client.
_SEND_EXEMPT_PREFIXES = ("demo/",)
_SEND_ATTRIBUTES = ("write_value", "call_method")
_STRUCTURE_ATTRIBUTES = ("add_nodes", "delete_nodes")


def _send_call_sites() -> dict[str, list[int]]:
    """Every ``write_value(...)`` / ``call_method(...)`` outside the one allowed module."""
    sites: dict[str, list[int]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        if relative == _SEND_MODULE or relative.startswith(_SEND_EXEMPT_PREFIXES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _SEND_ATTRIBUTES:
                continue
            # ``write_service.call_method(...)`` is the one path, not a second implementation.
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "write_service":
                continue
            sites.setdefault(relative, []).append(node.lineno)
    return sites


@pytest.mark.parametrize("module", _PURE_MODULES)
def test_pure_module_imports_neither_asyncua_nor_qt(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{module} could not be imported in isolation:\n{result.stderr}"
    assert result.stdout.strip() == "", f"{module} must stay Qt-free and asyncua-free: {result.stdout.strip()}"


def test_only_write_service_sends_writes_and_calls() -> None:
    sites = _send_call_sites()
    assert sites == {}, f"send a write/call through opcua/write_service.py instead: {sites}"


def _structure_call_sites() -> dict[str, list[int]]:
    """Every ``add_nodes(...)`` / ``delete_nodes(...)`` outside the allowed module."""
    sites: dict[str, list[int]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        if relative == _STRUCTURE_MODULE or relative.startswith(_SEND_EXEMPT_PREFIXES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _STRUCTURE_ATTRIBUTES:
                continue
            sites.setdefault(relative, []).append(node.lineno)
    return sites


def test_only_node_management_sends_add_or_delete_nodes() -> None:
    sites = _structure_call_sites()
    assert sites == {}, f"send AddNodes/DeleteNodes through opcua/node_management.py instead: {sites}"


def test_node_management_requires_an_authority() -> None:
    for name in ("create_node", "delete_node"):
        parameter = inspect.signature(getattr(node_management, name)).parameters.get("authorization")
        assert parameter is not None, f"{name} must state the authority it sends under"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}: authorization must be keyword-only"
        assert parameter.default is inspect.Parameter.empty, f"{name}: authorization must not have a default"


def test_write_service_stays_transport_neutral() -> None:
    tree = ast.parse((_PACKAGE_ROOT / _SEND_MODULE).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    segments = {part for name in imported for part in name.split(".") if part}
    forbidden = segments & {"gui", "defaults", "PySide6", "PySide2", "shiboken6"}
    assert forbidden == set(), f"write_service must stay Qt-free and default-free: {sorted(forbidden)}"


def _client_construction_sites() -> dict[str, list[int]]:
    """Every ``Client(...)`` call in the package (asyncua's client class name)."""
    sites: dict[str, list[int]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Client":
                sites.setdefault(relative, []).append(node.lineno)
    return sites


def test_only_session_setup_constructs_an_asyncua_client() -> None:
    sites = _client_construction_sites()
    assert set(sites) == {"opcua/session_setup.py"}, (
        "construct the asyncua client only in opcua/session_setup.py: " f"{sites}"
    )


def _asyncio_import_sites() -> dict[str, list[int]]:
    """Every asyncio import under ``gui/``."""
    sites: dict[str, list[int]] = {}
    for path in sorted((_PACKAGE_ROOT / "gui").rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "asyncio" or name.startswith("asyncio.") for name in names):
                sites.setdefault(relative, []).append(node.lineno)
    return sites


def test_only_async_runner_imports_asyncio_in_the_gui() -> None:
    sites = _asyncio_import_sites()
    assert set(sites) == {"gui/async_runner.py"}, (
        "only gui/async_runner.py may bridge asyncio into the GUI: " f"{sites}"
    )


def test_cli_front_ends_do_not_import_asyncua() -> None:
    front_ends = (
        "cli.py",
        "cli_compare.py",
        "cli_sequence.py",
        "cli_read.py",
        "cli_monitor.py",
        "cli_events.py",
        "cli_history.py",
        "cli_write.py",
        "cli_check.py",
        "cli_status.py",
        "cli_nodes.py",
        "cli_directory.py",
    )
    for front_end in front_ends:
        tree = ast.parse((_PACKAGE_ROOT / front_end).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(name == "asyncua" or name.startswith("asyncua.") for name in imported), (
            f"{front_end} must stay a Qt-free adapter over OpcUaService, not touch asyncua"
        )


def test_every_send_requires_an_authority() -> None:
    for name in (
        "send_prepared_write",
        "send_prepared_call",
        "write_variable",
        "call_method",
        "restore_value",
        "condition_action",
        "refresh_conditions",
    ):
        parameter = inspect.signature(getattr(write_service, name)).parameters.get("authorization")
        assert parameter is not None, f"{name} must state the authority it sends under"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}: authorization must be keyword-only"
        assert parameter.default is inspect.Parameter.empty, f"{name}: authorization must not have a default"
