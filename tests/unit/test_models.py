"""The models package re-exports the public vocabulary and keeps clusters apart."""

from __future__ import annotations

import importlib

import opcua_explorer.models as models

#: One module per feature cluster (``docs/architecture.md`` §2).
_CLUSTERS = (
    "common",
    "discovery",
    "events",
    "functional_tests",
    "graph",
    "history",
    "monitor",
    "sequences",
    "server_status",
    "values",
    "writes",
)


def test_every_exported_name_resolves() -> None:
    missing = [name for name in models.__all__ if not hasattr(models, name)]
    assert missing == []


def test_public_vocabulary_is_available_from_the_package() -> None:
    for name in (
        "AddressSpace",
        "ConnectionInfo",
        "Sequence",
        "SequenceRun",
        "ServerStatusSnapshot",
        "TestConfig",
        "TestRun",
        "ValueReadResult",
        "WriteAuthority",
    ):
        assert hasattr(models, name), name


def test_feature_clusters_import_independently() -> None:
    for cluster in _CLUSTERS:
        importlib.import_module(f"opcua_explorer.models.{cluster}")


def test_event_start_has_no_server_status_serializer() -> None:
    """Regression: a misplaced ``ServerStatusSnapshot.to_dict`` once lived in ``EventStart``."""
    assert not hasattr(models.EventStart, "to_dict")
