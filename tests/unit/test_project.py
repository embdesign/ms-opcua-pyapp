"""Project files: round-trip, validation and the no-secrets guarantee."""

from __future__ import annotations

import json

import pytest

from opcua_explorer.config import ConnectionProfile
from opcua_explorer.models import MethodCallTarget, WriteTarget
from opcua_explorer.project import (
    PROJECT_FORMAT,
    PROJECT_VERSION,
    ExportState,
    MonitorState,
    ProjectError,
    ProjectFile,
    ProjectSession,
    SequenceState,
    TestsState,
)


def _project() -> ProjectFile:
    return ProjectFile(
        sessions=[
            ProjectSession(
                name="PSU8600",
                connection=ConnectionProfile(
                    name="PSU8600",
                    endpoint_url="opc.tcp://192.168.20.4:4840",
                    auth_mode="username",
                    username="operator",
                    auto_reconnect=False,
                    connect_timeout=5.0,
                    request_timeout=7.5,
                ),
                start_node_id="ns=2;s=Plant",
                export=ExportState(
                    scope="subtree",
                    subtree_node_id="ns=2;s=Plant",
                    max_nodes=123,
                    max_depth=4,
                    concurrency=2,
                    batch_size=11,
                    max_references_per_node=7,
                    include_values=True,
                    include_description=False,
                    namespaces=[0, 2],
                    include_dependencies=False,
                    max_dependency_nodes=50,
                    schema_path="/tmp/nodeset.xsd",
                ),
                monitor=MonitorState(
                    node_ids=["ns=2;s=Temperature"],
                    publishing_interval_ms=750.0,
                    sampling_interval_ms=300.0,
                    keep_rows=500,
                ),
                tests=TestsState(
                    variable_node_id="ns=2;s=Speed",
                    latency_samples=3,
                    include_channel_recovery=True,
                    write_targets=[
                        WriteTarget(node_id="ns=2;s=Speed", value_text="42", restore_previous_value=True)
                    ],
                    method_calls=[
                        MethodCallTarget(
                            object_node_id="ns=2;s=Motor",
                            method_node_id="ns=2;s=Start",
                            arguments=["1450"],
                        )
                    ],
                ),
                sequence=SequenceState(
                    path="/plants/psu-setpoint.opcua-sequence.json", inputs={"voltage": "24.0"}
                ),
            ),
            ProjectSession(
                name="Second",
                connection=ConnectionProfile(name="Second", endpoint_url="opc.tcp://127.0.0.1:4840"),
            ),
        ]
    )


def test_roundtrip_preserves_configuration_and_editor_state(tmp_path) -> None:
    path = tmp_path / "plant.opcua-project.json"
    _project().save(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format"] == PROJECT_FORMAT
    assert raw["version"] == PROJECT_VERSION

    loaded = ProjectFile.load(path)
    assert [session.name for session in loaded.sessions] == ["PSU8600", "Second"]
    first = loaded.sessions[0]
    assert first.connection.endpoint_url == "opc.tcp://192.168.20.4:4840"
    assert first.connection.username == "operator"
    assert first.connection.auto_reconnect is False
    assert first.connection.connect_timeout == 5.0
    assert first.start_node_id == "ns=2;s=Plant"
    assert first.export == _project().sessions[0].export
    assert first.monitor == _project().sessions[0].monitor
    assert first.tests == _project().sessions[0].tests
    assert first.sequence == _project().sessions[0].sequence
    assert first.sequence.inputs["voltage"] == "24.0"
    assert loaded.sessions[1].connection.endpoint_url == "opc.tcp://127.0.0.1:4840"


def test_file_never_contains_secrets_or_the_write_master_switch(tmp_path) -> None:
    path = tmp_path / "plant.opcua-project.json"
    _project().save(path)
    text = path.read_text(encoding="utf-8")
    assert '"password"' not in text
    assert "private_key_password" not in text
    assert "server_certificate_der" not in text
    assert "allow_writes" not in text, "guarded operations stay opt-in for every run"


def test_load_refuses_a_file_that_carries_secret_fields(tmp_path) -> None:
    path = tmp_path / "tampered.opcua-project.json"
    payload = _project().to_dict()
    payload["password"] = "hunter2"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProjectError) as excinfo:
        ProjectFile.load(path)
    assert "secret" in str(excinfo.value).lower()
    assert "hunter2" not in str(excinfo.value)


def test_a_version_1_project_loads_with_an_empty_sequence_state(tmp_path) -> None:
    path = tmp_path / "older.opcua-project.json"
    payload = _project().to_dict()
    payload["version"] = 1
    for session in payload["sessions"]:
        session.pop("sequence", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = ProjectFile.load(path)

    assert loaded.sessions[0].sequence.path == ""
    assert loaded.sessions[0].sequence.inputs == {}


def test_load_rejects_newer_versions_and_empty_projects(tmp_path) -> None:
    newer = tmp_path / "newer.opcua-project.json"
    newer.write_text(
        json.dumps({"format": PROJECT_FORMAT, "version": PROJECT_VERSION + 1, "sessions": []}),
        encoding="utf-8",
    )
    with pytest.raises(ProjectError, match="newer"):
        ProjectFile.load(newer)

    empty = tmp_path / "empty.opcua-project.json"
    empty.write_text(
        json.dumps({"format": PROJECT_FORMAT, "version": PROJECT_VERSION, "sessions": []}),
        encoding="utf-8",
    )
    with pytest.raises(ProjectError, match="no sessions"):
        ProjectFile.load(empty)


def test_load_rejects_broken_json_and_foreign_files(tmp_path) -> None:
    broken = tmp_path / "broken.opcua-project.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectError, match="not valid JSON"):
        ProjectFile.load(broken)

    foreign = tmp_path / "foreign.opcua-project.json"
    foreign.write_text(json.dumps({"format": "something-else", "version": 1}), encoding="utf-8")
    with pytest.raises(ProjectError, match="not an OPC UA Explorer project"):
        ProjectFile.load(foreign)

    listing = tmp_path / "list.opcua-project.json"
    listing.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ProjectError):
        ProjectFile.load(listing)


def test_unknown_keys_are_ignored_and_cannot_inject_fields(tmp_path) -> None:
    path = tmp_path / "future.opcua-project.json"
    payload = _project().to_dict()
    payload["sessions"][0]["unknown_field"] = "ignored"
    payload["sessions"][0]["connection"]["server_certificate_der"] = "AABB"
    payload["sessions"][0]["tests"]["allow_writes"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = ProjectFile.load(path)
    session = loaded.sessions[0]
    assert not hasattr(session.connection, "server_certificate_der")
    assert not hasattr(session.tests, "allow_writes")


def test_secure_profile_without_certificate_is_rejected(tmp_path) -> None:
    project = ProjectFile(
        sessions=[
            ProjectSession(
                name="Secure",
                connection=ConnectionProfile(
                    name="Secure",
                    endpoint_url="opc.tcp://secure:4840",
                    security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
                    security_mode="SignAndEncrypt",
                ),
            )
        ]
    )
    path = tmp_path / "secure.opcua-project.json"
    with pytest.raises(ProjectError, match="certificate"):
        project.save(path)
    assert not path.exists(), "a rejected project must not leave a partial file"
