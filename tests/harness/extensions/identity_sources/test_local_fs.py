"""ID-C-01 … ID-C-05 for :class:`LocalFSIdentitySource`.

Each test uses a per-principal subdirectory populated on-the-fly inside
``tmp_path`` so no shared state leaks between cases. Acts as the "known
good" baseline alongside ``MockIdentitySource`` in the contract suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness.events import Principal
from src.core.harness.extensions import IdentityNotFound, LocalFSIdentitySource
from src.core.harness.extensions.values import MemoryPatch

pytestmark = pytest.mark.asyncio


def _seed(root: Path, principal_id: str) -> Path:
    principal_dir = root / principal_id
    principal_dir.mkdir(parents=True, exist_ok=True)
    (principal_dir / "SOUL.md").write_text("soul body")
    (principal_dir / "RULES.md").write_text("rules body")
    (principal_dir / "IDENTITY.md").write_text("identity body")
    (principal_dir / "USER.md").write_text("user body")
    (principal_dir / "INDEX.md").write_text("index body")
    (principal_dir / "MEMORY.md").write_text("memory body")
    (principal_dir / "HEARTBEAT.md").write_text("heartbeat body")
    return principal_dir


async def test_id_c_01_load_known_principal(tmp_path: Path) -> None:
    """ID-C-01: ``load`` returns a populated :class:`LoadedIdentity`."""
    _seed(tmp_path, "alice")
    source = LocalFSIdentitySource(dir=str(tmp_path))
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.principal_id == "alice"
    assert identity.soul == "soul body"
    assert identity.rules == "rules body"
    assert identity.source_backend == "local_fs"


async def test_id_c_02_unknown_principal_raises(tmp_path: Path) -> None:
    """ID-C-02: unknown principals raise :class:`IdentityNotFound`."""
    source = LocalFSIdentitySource(dir=str(tmp_path))
    with pytest.raises(IdentityNotFound):
        await source.load(principal=Principal(kind="user", id="nobody"))


async def test_id_c_03_write_memory_round_trip(tmp_path: Path) -> None:
    """ID-C-03: ``write_memory`` updates are visible on the next ``load``."""
    _seed(tmp_path, "alice")
    source = LocalFSIdentitySource(dir=str(tmp_path))
    await source.write_memory(
        principal=Principal(kind="user", id="alice"),
        patch=MemoryPatch(target="MEMORY.md", operation="replace", content="hello"),
    )
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.memory == "hello"


async def test_id_c_04_load_is_idempotent(tmp_path: Path) -> None:
    """ID-C-04: repeated loads return equivalent identity projections."""
    _seed(tmp_path, "alice")
    source = LocalFSIdentitySource(dir=str(tmp_path))
    first = await source.load(principal=Principal(kind="user", id="alice"), session_id="s1")
    second = await source.load(principal=Principal(kind="user", id="alice"), session_id="s1")
    assert first.principal_id == second.principal_id
    assert first.soul == second.soul
    assert first.rules == second.rules


async def test_id_c_05_system_prompt_block_composes(tmp_path: Path) -> None:
    """ID-C-05: :meth:`LoadedIdentity.system_prompt_block` composes all sections."""
    _seed(tmp_path, "alice")
    source = LocalFSIdentitySource(dir=str(tmp_path))
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    block = identity.system_prompt_block()
    for label in ("SOUL", "IDENTITY", "USER", "INDEX", "RULES", "MEMORY", "HEARTBEAT"):
        assert f"# === {label} ===" in block


async def test_append_patch_concatenates(tmp_path: Path) -> None:
    """``MemoryPatch.operation='append'`` concatenates to the existing file."""
    _seed(tmp_path, "alice")
    source = LocalFSIdentitySource(dir=str(tmp_path))
    await source.write_memory(
        principal=Principal(kind="user", id="alice"),
        patch=MemoryPatch(target="MEMORY.md", operation="append", content=" appended"),
    )
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.memory == "memory body appended"


async def test_delete_patch_removes_file(tmp_path: Path) -> None:
    """``MemoryPatch.operation='delete'`` wipes the heartbeat file."""
    _seed(tmp_path, "alice")
    source = LocalFSIdentitySource(dir=str(tmp_path))
    await source.write_memory(
        principal=Principal(kind="user", id="alice"),
        patch=MemoryPatch(target="HEARTBEAT.md", operation="delete", content=None),
    )
    identity = await source.load(principal=Principal(kind="user", id="alice"))
    assert identity.heartbeat == ""
