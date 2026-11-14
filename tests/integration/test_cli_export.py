"""Headless ``opcua-export`` end to end: the CLI writes importable NodeSet2 files.

Every test drives the real command entry point against the demo server, so the
CLI path (argument mapping → service → traversal → dependency closure → writer →
self-validation) is exercised exactly as an operator would use it.

The demo server runs on the test's own event loop, therefore the CLI (which owns
its loop through ``asyncio.run``) is executed in a worker thread: that is how it
behaves as a real command and it keeps the server responsive.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from opcua_explorer import cli
from opcua_explorer.demo.demo_server import DEMO_EXTRA_NAMESPACE, DEMO_NAMESPACE, find_free_port

NS0_URI = "http://opcfoundation.org/UA/"


def _argv(tmp_path: Path, demo_model, *extra: str) -> list[str]:
    return [
        "export",
        "--endpoint",
        demo_model.endpoint,
        "--trust-store",
        str(tmp_path / "trust.json"),
        "--quiet",
        *extra,
    ]


async def _run(tmp_path: Path, demo_model, *extra: str) -> int:
    return await asyncio.to_thread(cli.main, _argv(tmp_path, demo_model, *extra))


def _namespace_uris(path: Path) -> list[str]:
    root = ET.parse(path).getroot()  # noqa: S314 - file produced by this application
    return [element.text or "" for element in root.findall("{*}NamespaceUris/{*}Uri")]


def _identifiers(path: Path, uri: str) -> set[str]:
    """Identifiers of the written nodes that belong to ``uri``.

    The exporter re-maps the session's namespace indexes onto the file's own
    ``NamespaceUris`` table, so the NodeId index in the file is *not* the index
    the server used in this session. Resolving through the URI table is the only
    correct way to compare them.
    """
    root = ET.parse(path).getroot()  # noqa: S314 - file produced by this application
    uris = _namespace_uris(path)
    identifiers: set[str] = set()
    for element in root:
        node_id = element.attrib.get("NodeId")
        if not node_id:
            continue
        match = re.match(r"ns=(\d+);(.+)", node_id)
        if match is None:
            node_uri, identifier = NS0_URI, node_id
        else:
            index, identifier = int(match.group(1)), match.group(2)
            node_uri = NS0_URI if index == 0 else uris[index - 1]
        if node_uri == uri:
            identifiers.add(identifier)
    return identifiers


async def test_cli_exports_the_objects_folder_with_preserved_namespaces(tmp_path, demo_model) -> None:
    target = tmp_path / "objects.NodeSet2.xml"

    assert await _run(tmp_path, demo_model, "--output", str(target)) == cli.EXIT_OK

    assert target.is_file()
    root = ET.parse(target).getroot()  # noqa: S314 - file produced by this application
    assert root.tag.endswith("UANodeSet")
    # NodeIds keep their identifiers, and the URI table declares the namespaces used.
    demo_nodes = _identifiers(target, DEMO_NAMESPACE)
    assert "s=Plant" in demo_nodes
    assert "s=Temperature" in demo_nodes
    assert "urn:opcua-explorer:demo" in _namespace_uris(target)
    # Namespace 0 is referenced, never redefined in the file.
    assert _identifiers(target, NS0_URI) == set()
    # Every NodeId index in the file is a declared namespace index.
    declared = len(_namespace_uris(target)) + 1
    for element in root:
        node_id = element.attrib.get("NodeId", "")
        if node_id.startswith("ns="):
            assert 0 <= int(node_id[3 : node_id.index(";")]) < declared


async def test_cli_start_node_scope_keeps_the_parent_chain(tmp_path, demo_model) -> None:
    target = tmp_path / "motor.NodeSet2.xml"

    code = await _run(tmp_path, demo_model, "--node", demo_model.node_ids["motor"], "--output", str(target))

    assert code == cli.EXIT_OK
    demo_nodes = _identifiers(target, DEMO_NAMESPACE)
    assert "s=Motor" in demo_nodes
    # Ancestors are pulled in as dependencies, otherwise ParentNodeId would dangle.
    assert "s=Line1" in demo_nodes
    assert "s=Plant" in demo_nodes


async def test_cli_namespace_scope_excludes_other_namespaces(tmp_path, demo_model) -> None:
    target = tmp_path / "one-namespace.NodeSet2.xml"

    code = await _run(tmp_path, demo_model, "--namespace", str(demo_model.namespace_index), "--output", str(target))

    assert code == cli.EXIT_OK
    assert _identifiers(target, DEMO_NAMESPACE)
    assert _identifiers(target, DEMO_EXTRA_NAMESPACE) == set()


async def test_cli_writes_a_manifest_with_counts_and_validation(tmp_path, demo_model) -> None:
    target = tmp_path / "objects.NodeSet2.xml"
    manifest = tmp_path / "reports" / "objects.json"

    code = await _run(tmp_path, demo_model, "--output", str(target), "--manifest", str(manifest))

    assert code == cli.EXIT_OK
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["format"] == "opcua-nodeset2-export"
    assert document["nodes_written"] > 0
    assert document["validation"]["well_formed"] is True
    assert document["validation"]["structural_errors"] == []
    assert "password" not in json.dumps(document).lower()


async def test_cli_reports_a_custom_data_type_it_cannot_describe(tmp_path, demo_model) -> None:
    target = tmp_path / "objects-and-types.NodeSet2.xml"
    manifest = tmp_path / "objects-and-types.json"

    code = await _run(
        tmp_path,
        demo_model,
        "--node",
        "i=85",
        "--node",
        "i=86",
        "--output",
        str(target),
        "--manifest",
        str(manifest),
    )

    assert code == cli.EXIT_OK
    document = json.loads(manifest.read_text(encoding="utf-8"))
    reported = {entry["node_id"] for entry in document["unsupported_data_types"]}
    assert demo_model.node_ids["custom_data_type"] in reported
    assert "s=MeasurementType" not in _identifiers(target, DEMO_NAMESPACE)


async def test_cli_reports_nothing_to_write_for_a_namespace_zero_only_export(tmp_path, demo_model, capsys) -> None:
    target = tmp_path / "empty.NodeSet2.xml"

    code = await _run(tmp_path, demo_model, "--namespace", "0", "--output", str(target))

    assert code == cli.EXIT_FAILED
    assert not target.exists()
    assert "Nothing to write" in capsys.readouterr().err


def test_cli_fails_cleanly_when_the_endpoint_is_unreachable(tmp_path, capsys) -> None:
    target = tmp_path / "unreachable.NodeSet2.xml"
    port = find_free_port()

    code = cli.main(
        [
            "export",
            "--endpoint",
            f"opc.tcp://127.0.0.1:{port}",
            "--output",
            str(target),
            "--trust-store",
            str(tmp_path / "trust.json"),
            "--quiet",
            "--timeout",
            "2",
            "--connect-timeout",
            "2",
        ]
    )

    assert code == cli.EXIT_FAILED
    assert not target.exists()
    assert "error:" in capsys.readouterr().err


async def test_cli_reads_the_password_from_an_environment_variable(tmp_path, demo_model, monkeypatch) -> None:
    target = tmp_path / "authenticated.NodeSet2.xml"
    monkeypatch.setenv("CLI_TEST_PASSWORD", "demo-pass")

    code = await _run(
        tmp_path,
        demo_model,
        "--user",
        "demo",
        "--password-env",
        "CLI_TEST_PASSWORD",
        "--output",
        str(target),
    )

    assert code == cli.EXIT_OK
    assert target.is_file()


async def test_cli_refuses_a_user_name_without_a_password_source(tmp_path, demo_model, monkeypatch, capsys) -> None:
    target = tmp_path / "no-password.NodeSet2.xml"
    monkeypatch.delenv(cli.PASSWORD_ENV, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    code = await _run(tmp_path, demo_model, "--user", "demo", "--output", str(target))

    assert code == cli.EXIT_USAGE
    assert not target.exists()
    assert cli.PASSWORD_ENV in capsys.readouterr().err


async def test_cli_reports_a_missing_schema_file_instead_of_claiming_validation(tmp_path, demo_model) -> None:
    target = tmp_path / "objects.NodeSet2.xml"
    manifest = tmp_path / "objects.json"

    code = await _run(
        tmp_path,
        demo_model,
        "--output",
        str(target),
        "--manifest",
        str(manifest),
        "--schema",
        str(tmp_path / "not-there.xsd"),
    )

    assert code == cli.EXIT_OK
    document = json.loads(manifest.read_text(encoding="utf-8"))
    validation = document["validation"]
    assert validation["schema_status"] == "skipped"
    assert validation["schema_error"]
    assert validation["status"] == "not-validated"
