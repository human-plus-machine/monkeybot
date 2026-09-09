"""Tests for WorkspaceFileService's extra_read_roots — the read-only grant
mechanism behind PathGrantInspector (see path_grant_inspector.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService


def _svc(workspace: Path, *, extra_read_roots: list[Path] | None = None) -> WorkspaceFileService:
    return WorkspaceFileService(workspace, extra_read_roots=extra_read_roots)


class TestReadFileWithGrant:
    def test_absolute_path_rejected_without_a_grant(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "Desktop"
        outside.mkdir()
        (outside / "x.pdf").write_text("PDF-BYTES", encoding="utf-8")
        svc = _svc(workspace)

        with pytest.raises(WorkspaceError) as exc:
            svc.read_file(str(outside / "x.pdf"))
        assert exc.value.code == "path_needs_grant"

    def test_granted_folder_is_readable_in_place(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        target = desktop / "x.pdf"
        target.write_text("PDF-BYTES", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        result = svc.read_file(str(target))

        assert "PDF-BYTES" in result["content"]
        # Nothing moved: the file is exactly where it was, and the workspace
        # gained nothing.
        assert target.exists()
        assert list(workspace.iterdir()) == []
        # No repo-relative form exists for an external file — report the
        # real absolute path instead of raising.
        assert result["path"] == str(target.resolve())

    def test_read_file_denies_a_credential_file_in_a_granted_folder(self, tmp_path: Path) -> None:
        """Regression test: `PathGrantInspector` denies asking for a
        credential path at grant *time*, but it isn't the only way to reach
        an already-durable grant — `core/bootstrap.py`'s pattern-BC harness
        runs with `inspectors=[]` while still wiring a `grants_path` into
        this executor. `read_file`/`load_file` must apply the same
        credential filter `glob`/`grep` apply to their results, not rely
        entirely on the inspector having run."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / ".env").write_text("AWS_SECRET_ACCESS_KEY=leaked", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        with pytest.raises(WorkspaceError) as exc:
            svc.read_file(str(desktop / ".env"))
        assert exc.value.code == "credential_denied"

    def test_grant_covers_a_second_file_in_the_same_folder(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / "a.txt").write_text("A", encoding="utf-8")
        (desktop / "b.txt").write_text("B", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        assert "A" in svc.read_file(str(desktop / "a.txt"))["content"]
        assert "B" in svc.read_file(str(desktop / "b.txt"))["content"]

    def test_grant_does_not_cover_a_sibling_folder(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        documents = tmp_path / "Documents"
        desktop.mkdir()
        documents.mkdir()
        (documents / "x.txt").write_text("x", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        with pytest.raises(WorkspaceError) as exc:
            svc.read_file(str(documents / "x.txt"))
        assert exc.value.code == "path_needs_grant"

    def test_symlink_inside_granted_folder_cannot_escape_it(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET", encoding="utf-8")
        (desktop / "link.txt").symlink_to(secret)
        svc = _svc(workspace, extra_read_roots=[desktop])

        with pytest.raises(WorkspaceError) as exc:
            svc.read_file(str(desktop / "link.txt"))
        assert exc.value.code == "path_needs_grant"

    def test_write_file_ignores_extra_read_roots(self, tmp_path: Path) -> None:
        """A read grant must never widen what write_file can touch."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        svc = _svc(workspace, extra_read_roots=[desktop])

        with pytest.raises(WorkspaceError) as exc:
            svc.write_file(str(desktop / "new.txt"), "hi")
        assert exc.value.code == "invalid_path"
        assert not (desktop / "new.txt").exists()

    def test_sync_extra_read_roots_replaces_the_granted_set(self, tmp_path: Path) -> None:
        """The caller (``CoreToolExecutor.execute``) always passes the full
        current set — this turn's grants plus the durable store's contents —
        so ``sync`` assigns rather than merges: a grant revoked in Settings
        must stop being readable on the very next call, not persist for the
        life of the service (see ``sync_extra_read_roots`` docstring)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        documents = tmp_path / "Documents"
        desktop.mkdir()
        documents.mkdir()
        (desktop / "a.txt").write_text("A", encoding="utf-8")
        (documents / "b.txt").write_text("B", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        svc.sync_extra_read_roots([documents])

        assert "B" in svc.read_file(str(documents / "b.txt"))["content"]
        with pytest.raises(WorkspaceError) as exc:
            svc.read_file(str(desktop / "a.txt"))
        assert exc.value.code == "path_needs_grant"

    def test_sync_extra_read_roots_can_clear_all_grants(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / "a.txt").write_text("A", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        svc.sync_extra_read_roots([])

        with pytest.raises(WorkspaceError) as exc:
            svc.read_file(str(desktop / "a.txt"))
        assert exc.value.code == "path_needs_grant"


class TestGlobAndGrepWithGrant:
    def test_glob_root_can_be_a_granted_folder(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / "a.pdf").write_text("x", encoding="utf-8")
        (desktop / "b.txt").write_text("x", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        result = svc.glob_paths("*.pdf", root=str(desktop))

        assert result["paths"] == [str((desktop / "a.pdf").resolve())]

    def test_glob_root_outside_grant_is_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        documents = tmp_path / "Documents"
        desktop.mkdir()
        documents.mkdir()
        svc = _svc(workspace, extra_read_roots=[desktop])

        with pytest.raises(WorkspaceError) as exc:
            svc.glob_paths("*", root=str(documents))
        assert exc.value.code == "path_needs_grant"

    def test_grep_root_can_be_a_granted_folder(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / "notes.txt").write_text("TODO: ship it", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        result = svc.grep("TODO", root=str(desktop))

        assert result["match_count"] == 1

    def test_glob_excludes_denied_files_in_a_granted_folder(self, tmp_path: Path) -> None:
        """A folder grant is a directory grant by construction: granting
        Desktop must not also expose Desktop/.env — glob must filter denied
        entries out of its results, not just gate the granted root."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / ".env").write_text("AWS_SECRET_ACCESS_KEY=leaked", encoding="utf-8")
        (desktop / "notes.txt").write_text("hello", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        result = svc.glob_paths("*", root=str(desktop))

        assert result["paths"] == [str((desktop / "notes.txt").resolve())]

    def test_grep_excludes_denied_files_in_a_granted_folder(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / ".env").write_text("AWS_SECRET_ACCESS_KEY=leaked", encoding="utf-8")
        (desktop / "notes.txt").write_text("AWS_SECRET_ACCESS_KEY mentioned here too")
        svc = _svc(workspace, extra_read_roots=[desktop])

        result = svc.grep("AWS_SECRET_ACCESS_KEY", root=str(desktop))

        assert result["match_count"] == 1
        assert result["matches"][0]["path"] == str((desktop / "notes.txt").resolve())

    def test_grep_excludes_denied_subdir_in_a_granted_folder(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        ssh_dir = desktop / ".ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / "id_rsa").write_text("SECRETKEY", encoding="utf-8")
        svc = _svc(workspace, extra_read_roots=[desktop])

        result = svc.grep("SECRETKEY", root=str(desktop))

        assert result["match_count"] == 0

    def test_run_command_cwd_resolution_ignores_extra_read_roots(self, tmp_path: Path) -> None:
        """_resolve_root_dir (cwd for run_command) must stay workspace-only —
        only _resolve_read_root_dir (glob/grep) is grant-aware."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        svc = _svc(workspace, extra_read_roots=[desktop])

        with pytest.raises(WorkspaceError) as exc:
            svc._resolve_root_dir(str(desktop))
        assert exc.value.code == "invalid_path"
