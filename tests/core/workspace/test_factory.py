"""Tests for :func:`~monkeybot.core.workspace.factory.create_workspace_storage`."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from monkeybot.core.workspace.factory import create_workspace_storage
from monkeybot.core.workspace.local import LocalWorkspaceStorage


def test_local_scheme_returns_local_storage(tmp_path: Path) -> None:
    p = tmp_path / "d"
    p.mkdir()
    st = create_workspace_storage("local://" + str(p))
    assert isinstance(st, LocalWorkspaceStorage)


def test_bare_path_returns_local_storage(tmp_path: Path) -> None:
    p = tmp_path / "d"
    p.mkdir()
    st = create_workspace_storage(str(p))
    assert isinstance(st, LocalWorkspaceStorage)


def test_gcs_uri_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _hook(name: str, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "monkeybot.core.workspace.gcs":
            raise ImportError("simulated missing google-cloud-storage")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _hook)
    with pytest.raises(ImportError):
        create_workspace_storage("gcs://my-bucket/prefix")


def test_s3_uri_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _hook(name: str, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "monkeybot.core.workspace.s3":
            raise ImportError("simulated missing boto3")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _hook)
    with pytest.raises(ImportError):
        create_workspace_storage("s3://my-bucket/prefix")
