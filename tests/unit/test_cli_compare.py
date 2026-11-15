"""Unit tests for the headless comparison CLI (argument mapping, exit codes, no network)."""

from __future__ import annotations

import asyncio

import pytest

from opcua_explorer import cli_compare
from opcua_explorer.exporters import export_json
from opcua_explorer.models import AddressSpace, NamespaceEntry, NodeRecord

REFERENCE = "opc.tcp://old-device:4840"
CANDIDATE = "opc.tcp://new-device:4840"
NS0 = "http://opcfoundation.org/UA/"
PLANT = "urn:example:plant"


def _args(*extra: str):
    return cli_compare.build_parser().parse_args(["--reference", REFERENCE, "--candidate", CANDIDATE, *extra])


def _parse(*argv: str):
    return cli_compare.build_parser().parse_args([*argv, "--quiet"])


def _node(node_id: str, name: str, **fields) -> NodeRecord:
    values = {
        "node_class": "Variable",
        "data_type": "i=11",
        "value_rank": -1,
        "array_dimensions": [],
        "access_level_names": ["CurrentRead"],
        "user_access_level_names": ["CurrentRead"],
        "type_definition": "i=63",
        "display_name": name,
    }
    values.update(fields)
    return NodeRecord(
        node_id=node_id,
        browse_name=name,
        browse_name_namespace_index=1,
        namespace_uri=PLANT,
        **values,
    )


def _snapshot(endpoint: str, nodes: list[NodeRecord]) -> AddressSpace:
    return AddressSpace(
        endpoint_url=endpoint,
        nodes={record.node_id: record for record in nodes},
        namespaces=[NamespaceEntry(0, NS0), NamespaceEntry(1, PLANT)],
    )


class _FakeService:
    """Stands in for ``OpcUaService``; hands out pre-built snapshots in call order."""

    snapshots: list[AddressSpace] = []
    created: list[_FakeService] = []

    def __init__(self, trust_store=None) -> None:  # noqa: ANN001 - mirrors the real signature
        self.trust_store = trust_store
        self.settings = None
        self.roots: list[str] | None = None
        self.traversal = None
        self.shutdown_called = False
        _FakeService.created.append(self)

    async def connect(self, settings) -> None:  # noqa: ANN001 - mirrors the real signature
        self.settings = settings

    async def download(self, roots, config, *, progress=None):  # noqa: ANN001, ANN201 - mirrors the real service
        self.roots = list(roots)
        self.traversal = config
        return _FakeService.snapshots.pop(0)

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture
def fake_services(monkeypatch):
    _FakeService.snapshots = []
    _FakeService.created = []

    def install(*snapshots: AddressSpace) -> None:
        _FakeService.snapshots.extend(snapshots)
        monkeypatch.setattr(cli_compare, "OpcUaService", _FakeService)

    return install


# -- arguments -------------------------------------------------------------


def test_both_endpoints_are_required() -> None:
    with pytest.raises(cli_compare.CliError, match="Give --reference"):
        asyncio.run(cli_compare.run_compare(cli_compare.build_parser().parse_args(["--quiet"])))
    with pytest.raises(cli_compare.CliError, match="Give --candidate"):
        asyncio.run(
            cli_compare.run_compare(
                cli_compare.build_parser().parse_args(["--reference", REFERENCE, "--quiet"])
            )
        )


def test_a_side_cannot_be_endpoint_and_document_at_once() -> None:
    with pytest.raises(cli_compare.CliError, match="mutually exclusive"):
        cli_compare.source_of("reference", REFERENCE, "old.json")


def test_comparison_defaults_are_the_cautious_ones() -> None:
    config = cli_compare.comparison_config_from_args(_args())

    assert config.tolerance == "functional"
    assert config.match_by_browse_path is True
    assert config.compare_references is False
    assert config.value_node_ids == frozenset()
    assert config.ignore_node_ids == frozenset()
    assert config.float_tolerance == 0.0


def test_comparison_flags_map_to_the_shared_config() -> None:
    config = cli_compare.comparison_config_from_args(
        _args(
            "--tolerance",
            "strict",
            "--compare-reference-names",
            "--no-browse-path-match",
            "--compare-value",
            "ns=1;s=Setpoint",
            "--ignore",
            "i=2258",
            "--float-tolerance",
            "0.25",
            "--max-differences",
            "5",
        )
    )

    assert config.tolerance == "strict"
    assert config.compare_references is True
    assert config.match_by_browse_path is False
    assert config.value_node_ids == frozenset({"ns=1;s=Setpoint"})
    assert config.compare_value_payload is True
    assert config.ignore_node_ids == frozenset({"i=2258"})
    assert config.float_tolerance == 0.25
    assert config.max_differences == 5


def test_candidate_settings_reuse_the_identity() -> None:
    args = _args()
    settings = cli_compare.settings_from_args(
        cli_compare._side_args(args, endpoint=REFERENCE, server_certificate=None)
    )
    candidate = cli_compare.candidate_settings(settings, args)

    assert candidate.endpoint_url == CANDIDATE
    assert candidate.auth_mode == settings.auth_mode
    assert candidate.auto_reconnect is False
    assert candidate.server_certificate_der is None


def test_candidate_side_can_override_the_security_settings() -> None:
    args = _args(
        "--candidate-policy",
        "Basic256Sha256",
        "--candidate-mode",
        "SignAndEncrypt",
        "--client-certificate",
        "client.der",
        "--client-key",
        "client.pem",
    )
    settings = cli_compare.settings_from_args(
        cli_compare._side_args(args, endpoint=REFERENCE, server_certificate=None)
    )
    candidate = cli_compare.candidate_settings(settings, args)

    assert settings.security_policy_uri.endswith("#None")
    assert candidate.security_policy_uri.endswith("#Basic256Sha256")
    assert candidate.security_mode == "SignAndEncrypt"
    assert candidate.client_certificate_path == "client.der"


# -- exit codes ------------------------------------------------------------


def test_identical_snapshots_exit_zero_and_close_both_sessions(fake_services) -> None:
    nodes = [_node("ns=1;s=Temperature", "Temperature")]
    fake_services(
        _snapshot(REFERENCE, nodes),
        _snapshot(CANDIDATE, nodes),
    )

    code = asyncio.run(cli_compare.run_compare(_args("--quiet")))

    reference, candidate = _FakeService.created
    assert code == cli_compare.EXIT_OK
    assert reference.roots == ["i=85"]
    assert candidate.roots == ["i=85"]
    assert reference.shutdown_called and candidate.shutdown_called
    assert candidate.settings.endpoint_url == CANDIDATE


def test_differences_exit_three_and_write_reports(fake_services, tmp_path, capsys) -> None:
    fake_services(
        _snapshot(REFERENCE, [_node("ns=1;s=Temperature", "Temperature")]),
        _snapshot(CANDIDATE, [_node("ns=1;s=Temperature", "Temperature", node_class="Object", data_type=None)]),
    )
    json_path = tmp_path / "nested" / "diff.json"
    html_path = tmp_path / "diff.html"

    code = asyncio.run(
        cli_compare.run_compare(
            _args("--quiet", "--json", str(json_path), "--report", str(html_path))
        )
    )

    assert code == cli_compare.EXIT_DIFFERENCES
    assert json_path.exists() and html_path.exists()
    document = json_path.read_text(encoding="utf-8")
    assert '"format": "opcua-comparison"' in document
    assert "address-space comparison" in html_path.read_text(encoding="utf-8")
    assert "differences found" in capsys.readouterr().out


def test_values_are_read_when_a_node_is_listed_for_value_comparison(fake_services) -> None:
    nodes = [_node("ns=1;s=Temperature", "Temperature")]
    fake_services(_snapshot(REFERENCE, nodes), _snapshot(CANDIDATE, nodes))

    asyncio.run(cli_compare.run_compare(_args("--quiet", "--compare-value", "ns=1;s=Temperature")))

    reference = _FakeService.created[0]
    assert reference.traversal.include_values is True


def test_a_failed_candidate_connection_still_closes_the_reference(monkeypatch) -> None:
    class _FailingCandidate(_FakeService):
        async def connect(self, settings) -> None:  # noqa: ANN001 - mirrors the real signature
            raise cli_compare.CliError("candidate unreachable")

    nodes = [_node("ns=1;s=Temperature", "Temperature")]
    _FakeService.snapshots = [_snapshot(REFERENCE, nodes), _snapshot(CANDIDATE, nodes)]
    _FakeService.created = []
    monkeypatch.setattr(cli_compare, "OpcUaService", _FailingCandidate)

    with pytest.raises(cli_compare.CliError):
        asyncio.run(cli_compare.run_compare(_args("--quiet")))

    assert all(service.shutdown_called for service in _FakeService.created)


# -- saved documents as comparison sources ---------------------------------


def _write_document(path, nodes: list[NodeRecord], *, endpoint: str = "opc.tcp://saved:4840") -> str:  # noqa: ANN001
    export_json(_snapshot(endpoint, nodes), path)
    return str(path)


class _OfflineService:
    """Fails loudly if a saved-document comparison ever opens a session."""

    created: list[_OfflineService] = []

    def __init__(self, trust_store=None) -> None:  # noqa: ANN001 - mirrors the real signature
        self.trust_store = trust_store
        self.shutdown_called = False
        _OfflineService.created.append(self)

    async def connect(self, settings) -> None:  # noqa: ANN001 - mirrors the real signature
        raise AssertionError("a saved document must not open a session")

    async def download(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201 - mirrors the real service
        raise AssertionError("a saved document must not download anything")

    async def shutdown(self) -> None:
        self.shutdown_called = True


def test_two_saved_documents_compare_without_any_connection(tmp_path, monkeypatch) -> None:
    nodes = [_node("ns=1;s=Temperature", "Temperature")]
    reference = _write_document(tmp_path / "old.json", nodes)
    candidate = _write_document(tmp_path / "new.json", nodes)
    _OfflineService.created = []
    monkeypatch.setattr(cli_compare, "OpcUaService", _OfflineService)

    code = asyncio.run(
        cli_compare.run_compare(_parse("--reference-file", reference, "--candidate-file", candidate))
    )

    assert code == cli_compare.EXIT_OK
    assert all(service.shutdown_called for service in _OfflineService.created)


def test_a_saved_document_can_be_the_reference_of_a_live_candidate(fake_services, tmp_path) -> None:
    nodes = [_node("ns=1;s=Temperature", "Temperature")]
    reference = _write_document(tmp_path / "old.json", nodes, endpoint=REFERENCE)
    fake_services(_snapshot(CANDIDATE, nodes))

    code = asyncio.run(
        cli_compare.run_compare(_parse("--reference-file", reference, "--candidate", CANDIDATE))
    )

    assert code == cli_compare.EXIT_OK
    idle, live = _FakeService.created
    assert idle.settings is None  # the document side never connected
    assert live.settings.endpoint_url == CANDIDATE
    assert live.roots == ["i=85"]


def test_saved_documents_with_differences_exit_three(tmp_path, monkeypatch) -> None:
    reference = _write_document(tmp_path / "old.json", [_node("ns=1;s=Temperature", "Temperature")])
    candidate = _write_document(
        tmp_path / "new.json",
        [_node("ns=1;s=Temperature", "Temperature", node_class="Object", data_type=None)],
    )
    monkeypatch.setattr(cli_compare, "OpcUaService", _OfflineService)

    code = asyncio.run(
        cli_compare.run_compare(_parse("--reference-file", reference, "--candidate-file", candidate))
    )

    assert code == cli_compare.EXIT_DIFFERENCES


def test_certificate_flags_are_refused_for_a_document_side(tmp_path) -> None:
    reference = _write_document(tmp_path / "old.json", [_node("ns=1;s=Temperature", "Temperature")])

    with pytest.raises(cli_compare.CliError, match="needs a live --reference"):
        asyncio.run(
            cli_compare.run_compare(
                _parse(
                    "--reference-file",
                    reference,
                    "--candidate",
                    CANDIDATE,
                    "--reference-server-certificate",
                    "server.der",
                )
            )
        )
