"""Architecture guards: layer boundaries that must not regress.

``docs/architecture.md`` §2 declares the pure core Qt-free and asyncua-free: ``models``,
``config``, ``defaults``, ``project``, ``formatting``, ``operator_text``,
``sequence_document``, ``reporting``, ``compare`` and ``exporters``. Each module is
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

from opcua_explorer.opcua import write_service

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
#: The demo server writes its own local model and is never a client.
_SEND_EXEMPT_PREFIXES = ("demo/",)
_SEND_ATTRIBUTES = ("write_value", "call_method")


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


def test_every_send_requires_an_authority() -> None:
    for name in (
        "send_prepared_write",
        "send_prepared_call",
        "write_variable",
        "call_method",
        "restore_value",
        "acknowledge_condition",
    ):
        parameter = inspect.signature(getattr(write_service, name)).parameters.get("authorization")
        assert parameter is not None, f"{name} must state the authority it sends under"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}: authorization must be keyword-only"
        assert parameter.default is inspect.Parameter.empty, f"{name}: authorization must not have a default"
