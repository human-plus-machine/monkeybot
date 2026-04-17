"""Unit tests for the generic ``Registry[T]`` 7-tier resolver."""

from __future__ import annotations

import os
import sys
import types
from typing import Any
from unittest.mock import patch

import pytest

from src.core.harness.extensions.errors import BackendConfigError, BackendNotFound
from src.core.harness.extensions.registry import Registry


class _Dummy:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _dummy_factory(**kwargs: Any) -> _Dummy:
    return _Dummy(**kwargs)


def _make_module(name: str, attrs: dict[str, Any]) -> types.ModuleType:
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    sys.modules[name] = module
    return module


def test_tier1_already_instance_returned_as_is() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    d = _Dummy()
    assert reg.resolve(d) is d


def test_tier2_string_import_path() -> None:
    _make_module("_reg_test_t2", {"MakeOne": _dummy_factory})
    reg: Registry[_Dummy] = Registry("checkpointer")
    got = reg.resolve("_reg_test_t2:MakeOne")
    assert isinstance(got, _Dummy)


def test_tier3_import_path_in_mapping_receives_kwargs() -> None:
    _make_module("_reg_test_t3", {"MakeOne": _dummy_factory})
    reg: Registry[_Dummy] = Registry("checkpointer")
    got = reg.resolve({"import_path": "_reg_test_t3:MakeOne", "alpha": 1, "backend": "ignored"})
    assert isinstance(got, _Dummy)
    assert got.kwargs == {"alpha": 1}


def test_tier4_programmatic_backend_name() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    reg.register("memory", _dummy_factory, source="programmatic", module="t4")
    got = reg.resolve({"backend": "memory", "url": "x"})
    assert got.kwargs == {"url": "x"}


def test_tier5_entry_point_gated_on_env_var() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    reg.register("redis", _dummy_factory, source="entry_point", module="t5")
    with patch.dict(os.environ, {"HARNESS_PLUGINS_FROM_ENTRY_POINTS": "0"}, clear=False), \
         pytest.raises(BackendNotFound):
        reg.resolve({"backend": "redis"})
    with patch.dict(os.environ, {"HARNESS_PLUGINS_FROM_ENTRY_POINTS": "1"}, clear=False):
        got = reg.resolve({"backend": "redis"})
    assert isinstance(got, _Dummy)


def test_tier6_builtin_resolution() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    reg.register("in_memory", _dummy_factory, source="builtin", module="t6")
    got = reg.resolve({"backend": "in_memory"})
    assert isinstance(got, _Dummy)


def test_tier7_default_when_backend_omitted() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer", default="in_memory")
    reg.register("in_memory", _dummy_factory, source="builtin", module="t7")
    got = reg.resolve({})
    assert isinstance(got, _Dummy)


def test_tier7_raises_without_default() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    with pytest.raises(BackendNotFound):
        reg.resolve({})


def test_unknown_backend_raises_backend_not_found() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    with pytest.raises(BackendNotFound):
        reg.resolve({"backend": "nope"})


def test_same_tier_programmatic_collision_raises() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    reg.register("a", _dummy_factory, source="programmatic", module="m")
    with pytest.raises(BackendConfigError):
        reg.register("a", _dummy_factory, source="programmatic", module="m")


def test_same_tier_programmatic_collision_overwrite_ok() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    reg.register("a", _dummy_factory, source="programmatic", module="m")

    def other(**kw: Any) -> _Dummy:
        return _Dummy(**kw)

    reg.register("a", other, source="programmatic", module="m2", overwrite=True)
    entry = reg.entry("a")
    assert entry is not None
    assert "m2" in entry.factory_qualname
    assert len(reg._shadowed_entries()) == 1


def test_cross_tier_higher_precedence_wins_loser_shadowed() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    reg.register("x", _dummy_factory, source="builtin", module="builtin")

    def other(**kw: Any) -> _Dummy:
        return _Dummy(**kw)

    reg.register("x", other, source="programmatic", module="prog")
    entry = reg.entry("x")
    assert entry is not None
    assert entry.source == "programmatic"
    assert any(e.source == "builtin" for e in reg._shadowed_entries())


def test_cross_tier_lower_precedence_is_shadowed_from_registration() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    reg.register("x", _dummy_factory, source="programmatic", module="prog")

    def other(**kw: Any) -> _Dummy:
        return _Dummy(**kw)

    reg.register("x", other, source="builtin", module="builtin")
    entry = reg.entry("x")
    assert entry is not None
    assert entry.source == "programmatic"
    shadowed = reg._shadowed_entries()
    assert any(e.source == "builtin" for e in shadowed)


def test_entry_points_ignored_when_env_var_unset() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")

    fake_eps = [
        types.SimpleNamespace(name="fake", load=lambda: _dummy_factory, module="fake.mod"),
    ]
    with patch.dict(os.environ, {"HARNESS_PLUGINS_FROM_ENTRY_POINTS": "0"}, clear=False), \
         patch("importlib.metadata.entry_points", return_value=fake_eps):
        entries = reg.entries()
    assert entries == []


def test_entry_points_loaded_when_env_var_set() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")

    fake_eps = [
        types.SimpleNamespace(name="fake", load=lambda: _dummy_factory, module="fake.mod"),
    ]
    with patch.dict(os.environ, {"HARNESS_PLUGINS_FROM_ENTRY_POINTS": "1"}, clear=False), \
         patch("importlib.metadata.entry_points", return_value=fake_eps):
        entries = reg.entries()
        got = reg.resolve({"backend": "fake"})
    assert any(e.name == "fake" for e in entries)
    assert isinstance(got, _Dummy)


def test_entry_point_failure_wraps_in_backend_config_error() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")

    def _boom() -> Any:
        raise RuntimeError("kaboom")

    fake_eps = [types.SimpleNamespace(name="bad", load=_boom, module="bad.mod")]
    with patch.dict(os.environ, {"HARNESS_PLUGINS_FROM_ENTRY_POINTS": "1"}, clear=False), \
         patch("importlib.metadata.entry_points", return_value=fake_eps), \
         pytest.raises(BackendConfigError):
        reg.entries()


def test_entry_point_group_mapping_uses_explicit_dict() -> None:
    from src.core.harness.extensions.registry import _ENTRY_POINT_GROUPS

    assert _ENTRY_POINT_GROUPS["identity_source"] == "emonk.identity_sources"
    assert _ENTRY_POINT_GROUPS["job_storage"] == "emonk.job_storage"
    assert _ENTRY_POINT_GROUPS["secret_resolver"] == "emonk.secret_resolvers"


def test_factory_kwargs_type_error_wrapped() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")

    def picky(only_one: str) -> _Dummy:
        return _Dummy(only_one=only_one)

    reg.register("picky", picky, source="programmatic", module="m")
    with pytest.raises(BackendConfigError):
        reg.resolve({"backend": "picky", "nope": "nope"})


def test_import_path_bad_format_raises() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    with pytest.raises(BackendConfigError):
        reg.resolve("no_colon_here")


def test_import_path_missing_module_raises() -> None:
    reg: Registry[_Dummy] = Registry("checkpointer")
    with pytest.raises(BackendConfigError):
        reg.resolve("definitely_not_a_module_xyz:Foo")
