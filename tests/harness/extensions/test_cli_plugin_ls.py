"""Unit tests for ``emonk-harness plugin ls``."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from typing import Any

import pytest

from src.core.harness.extensions.base import Checkpointer
from src.core.harness.extensions.cli import plugin_ls


def _args(**kwargs: Any) -> argparse.Namespace:
    defaults = {"kind": None, "source": None, "strict": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _mk_factory(name: str) -> Any:
    def factory(**kw: Any) -> object:
        return object()

    factory.__qualname__ = name
    factory.__module__ = f"test_mod_{name}"
    return factory


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Snapshot and restore the Checkpointer registry around each test."""
    reg = Checkpointer.registry
    entries = dict(reg._entries)
    factories = dict(reg._factories)
    shadowed = list(reg._shadowed)
    ep_loaded = reg._ep_loaded
    yield
    reg._entries = entries
    reg._factories = factories
    reg._shadowed = shadowed
    reg._ep_loaded = ep_loaded


def test_plugin_ls_returns_zero_and_valid_json() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = plugin_ls(_args())
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert "plugins" in data
    assert "collisions" in data
    assert isinstance(data["plugins"], list)


def test_plugin_ls_filters_by_kind() -> None:
    Checkpointer.registry.register(
        "dummy-for-kind-filter",
        _mk_factory("dummy"),
        source="programmatic",
        module="test",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = plugin_ls(_args(kind="memory_store"))
    assert rc == 0
    data = json.loads(buf.getvalue())
    kinds = {p["kind"] for p in data["plugins"]}
    assert "checkpointer" not in kinds


def test_plugin_ls_filters_by_source() -> None:
    Checkpointer.registry.register(
        "prog-source",
        _mk_factory("prog"),
        source="programmatic",
        module="test",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = plugin_ls(_args(source="programmatic"))
    assert rc == 0
    data = json.loads(buf.getvalue())
    for p in data["plugins"]:
        assert p["source"] == "programmatic"


def test_plugin_ls_strict_fails_on_collision() -> None:
    Checkpointer.registry.register(
        "collide",
        _mk_factory("first"),
        source="builtin",
        module="b",
    )
    Checkpointer.registry.register(
        "collide",
        _mk_factory("second"),
        source="programmatic",
        module="p",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = plugin_ls(_args(strict=True))
    data = json.loads(buf.getvalue())
    assert data["collisions"], "expected at least one collision"
    assert rc == 1


def test_plugin_ls_non_strict_does_not_fail_on_collision() -> None:
    Checkpointer.registry.register(
        "collide-nonstrict",
        _mk_factory("first"),
        source="builtin",
        module="b",
    )
    Checkpointer.registry.register(
        "collide-nonstrict",
        _mk_factory("second"),
        source="programmatic",
        module="p",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = plugin_ls(_args(strict=False))
    assert rc == 0


def test_linter_main_plugin_ls_wires_through() -> None:
    from src.core.harness.linter import main

    rc = main(["plugin", "ls"])
    assert rc == 0
