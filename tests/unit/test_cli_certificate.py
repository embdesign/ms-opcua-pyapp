"""Unit tests for ``opcua-certificate`` (local files only, no server)."""

from __future__ import annotations

import json
from pathlib import Path

from opcua_explorer import cli_certificate


def _argv(tmp_path, *extra: str) -> list[str]:
    return [
        "--application-uri",
        "urn:opcua-explorer:test:client",
        "--common-name",
        "line1-client",
        "--output-dir",
        str(tmp_path),
        *extra,
    ]


def test_the_command_writes_a_certificate_and_prints_its_fingerprint(tmp_path, capsys) -> None:
    code = cli_certificate.main(_argv(tmp_path))

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "Self-signed client certificate" in captured.out
    assert "SHA-256:" in captured.out
    assert "nothing was installed" in captured.out.lower()
    assert len(list(Path(tmp_path).glob("*.pem"))) == 2


def test_json_output_carries_the_document_version_and_paths(tmp_path, capsys) -> None:
    code = cli_certificate.main(_argv(tmp_path, "--json"))

    captured = capsys.readouterr()
    assert code == 0, captured.err
    document = json.loads(captured.out)
    assert document["format"] == "opcua-certificate"
    assert document["format_version"] == 1
    assert document["mode"] == "self-signed"
    assert Path(document["certificate_path"]).exists()
    assert Path(document["key_path"]).exists()


def test_a_request_names_the_csr_and_not_a_certificate(tmp_path, capsys) -> None:
    code = cli_certificate.main(_argv(tmp_path, "--mode", "csr", "--json"))

    captured = capsys.readouterr()
    assert code == 0, captured.err
    document = json.loads(captured.out)
    assert document["certificate_path"] is None
    assert document["request_path"].endswith(".csr.pem")


def test_existing_files_exit_with_three_and_write_nothing(tmp_path, capsys) -> None:
    assert cli_certificate.main(_argv(tmp_path, "--quiet")) == 0
    capsys.readouterr()

    code = cli_certificate.main(_argv(tmp_path, "--quiet"))

    captured = capsys.readouterr()
    assert code == 3
    assert "already exist" in captured.err
    assert "--overwrite" not in captured.out


def test_overwrite_is_explicit_and_works(tmp_path, capsys) -> None:
    assert cli_certificate.main(_argv(tmp_path, "--quiet")) == 0
    assert cli_certificate.main(_argv(tmp_path, "--quiet", "--overwrite")) == 0
    capsys.readouterr()

    assert len(list(Path(tmp_path).glob("*.pem"))) == 2


def test_a_missing_application_uri_is_a_usage_error(tmp_path, capsys) -> None:
    code = cli_certificate.main(
        ["--common-name", "x", "--output-dir", str(tmp_path), "--application-uri", "  "]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "application URI" in captured.err
    assert list(Path(tmp_path).glob("*.pem")) == []
