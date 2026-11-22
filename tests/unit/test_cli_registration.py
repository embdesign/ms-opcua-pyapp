"""Guards against drift between the headless front ends and what the docs promise.

Every CLI is a *front end* over `OpcUaService`, so the set of commands is an
interface of the product: a module without an entry point is unreachable, an entry
point without a module breaks the installed console script, and a command nobody
documents does not exist for the operator. Each of those has happened in other
projects; these tests make it a two-line failure instead of a discovery in the field.
"""

from __future__ import annotations

import importlib
import inspect
import tomllib
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "opcua_explorer"
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_README = Path(__file__).resolve().parents[2] / "README.md"
_AGENTS = Path(__file__).resolve().parents[2] / "AGENTS.md"

#: `cli.py` holds the shared argument/connection helpers and is reachable through
#: `opcua-export`, so it is not a naming template for the others.
_SHARED_HELPER = "cli.py"


def _scripts() -> dict[str, str]:
    with _PYPROJECT.open("rb") as handle:
        return dict(tomllib.load(handle)["project"]["scripts"])


def _cli_modules() -> set[str]:
    return {path.name for path in _PACKAGE_ROOT.glob("cli*.py")} - {_SHARED_HELPER, "__init__.py"}


def test_every_cli_module_has_an_entry_point() -> None:
    modules = _cli_modules()
    assert modules, "no CLI modules found - the guard would be vacuous"

    registered = {
        target.split(":")[0].rsplit(".", 1)[-1] + ".py"
        for target in _scripts().values()
        if target.startswith("opcua_explorer.cli")
    } - {_SHARED_HELPER}
    assert registered == modules, (
        "every headless front end needs a [project.scripts] entry point (and the other way round): "
        f"unregistered={sorted(modules - registered)} missing_module={sorted(registered - modules)}"
    )


@pytest.mark.parametrize("name,target", sorted(_scripts().items()))
def test_every_entry_point_resolves_to_a_callable_main(name: str, target: str) -> None:
    module_name, _, attribute = target.partition(":")
    module = importlib.import_module(module_name)
    entry = getattr(module, attribute, None)
    assert callable(entry), f"{name} points at {target}, which is not callable"
    if module_name.startswith("opcua_explorer.cli"):
        # A CLI's `main` takes an optional argv list so tests can drive it.
        parameters = list(inspect.signature(entry).parameters)
        assert parameters[:1] == ["argv"], f"{name}: main() must accept argv for headless tests"


def test_every_command_is_named_in_the_readme_and_agents_file() -> None:
    readme = _README.read_text(encoding="utf-8")
    agents = _AGENTS.read_text(encoding="utf-8")
    missing_readme = [name for name in _scripts() if name not in readme]
    missing_agents = [
        name for name in _scripts() if name.startswith("opcua-") and name not in agents
    ]
    assert not missing_readme, f"README.md does not mention: {missing_readme}"
    assert not missing_agents, f"AGENTS.md does not mention: {missing_agents}"


def test_the_cli_modules_are_listed_as_qt_free_front_ends() -> None:
    agents = _AGENTS.read_text(encoding="utf-8")
    missing = sorted(module for module in _cli_modules() if module not in agents)
    assert not missing, f"AGENTS.md does not list these front ends: {missing}"
