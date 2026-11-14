"""Unit tests for the Qt/asyncua-free formatting helpers."""

from __future__ import annotations

from opcua_explorer.formatting import flatten_for_csv, safe_repr


def test_flatten_for_csv_keeps_scalars_and_compacts_structures() -> None:
    assert flatten_for_csv(None) == ""
    assert flatten_for_csv("plain") == "plain"
    assert flatten_for_csv(True) == "True"
    assert flatten_for_csv({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_flatten_for_csv_uses_str_for_unknown_objects() -> None:
    class Custom:
        def __str__(self) -> str:
            return "custom"

    assert flatten_for_csv(Custom()) == '"custom"'


def test_safe_repr_is_bounded_and_never_raises() -> None:
    class Broken:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    assert "unrepresentable" in safe_repr(Broken())
    text = safe_repr(list(range(100)), limit=10)
    assert len(text) == 10 and text.endswith("...")
