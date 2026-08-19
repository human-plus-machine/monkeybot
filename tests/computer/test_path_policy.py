"""Tests for the hard security boundary in ``computer/safety.py``.

These are the tests that matter most in this feature: everything here proves
that a broken or missing ``permissions.yaml`` cannot turn into an unsafe
filesystem/URL/app action, because none of these checks depend on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.computer import safety


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("MONKEYBOT_APP_HOME", raising=False)
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    return fake_home


class TestResolveUserPath:
    def test_allows_path_under_home(self, home: Path) -> None:
        target = home / "Downloads"
        target.mkdir()
        resolved = safety.resolve_user_path(str(target))
        assert resolved == target.resolve()

    def test_expands_tilde(self, home: Path) -> None:
        (home / "Desktop").mkdir()
        resolved = safety.resolve_user_path("~/Desktop")
        assert resolved == (home / "Desktop").resolve()

    def test_rejects_empty_path(self, home: Path) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path("")
        assert exc.value.kind == "validation"

    def test_rejects_path_outside_home(self, home: Path) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path("/etc/passwd")
        assert exc.value.kind == "policy"

    def test_rejects_dotdot_escape(self, home: Path) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path(str(home / ".." / ".." / "etc"))
        assert exc.value.kind == "policy"

    def test_rejects_symlink_escape(self, home: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope")
        link = home / "escape"
        link.symlink_to(outside)
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path(str(link / "secret.txt"))
        assert exc.value.kind == "policy"

    @pytest.mark.parametrize(
        "rel",
        [
            ".ssh",
            ".aws",
            ".gnupg",
            ".kube",
            ".docker",
            ".config/gcloud",
            ".monkeybot",
            "Library/Keychains",
            "Library/Application Support/Monkeybot",
        ],
    )
    def test_rejects_denied_subdirs(self, home: Path, rel: str) -> None:
        target = home / rel
        target.mkdir(parents=True)
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path(str(target))
        assert exc.value.kind == "policy"

    @pytest.mark.parametrize(
        "name",
        [
            ".env",
            ".env.local",
            "id_rsa",
            "id_rsa.pub",
            "secret.pem",
            "server.key",
            "credentials",
            "credentials.json",
            ".netrc",
            "backup.mobileprovision",
        ],
    )
    def test_rejects_denied_filenames(self, home: Path, name: str) -> None:
        target = home / name
        target.write_text("x")
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path(str(target))
        assert exc.value.kind == "policy"

    def test_rejects_app_home(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        app_home = home / "Library" / "Application Support" / "MonkeybotDesktopFixture"
        app_home.mkdir(parents=True)
        monkeypatch.setenv("MONKEYBOT_APP_HOME", str(app_home))
        target = app_home / "agents" / "x" / "monkeybot_config" / "approvals.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}")
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path(str(target))
        assert exc.value.kind == "policy"

    def test_rejects_own_agent_config_dir(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The agent must not be able to move/read its own permissions.yaml / approvals.json."""
        config_dir = home / "agent" / "monkeybot_config"
        config_dir.mkdir(parents=True)
        (config_dir / "permissions.yaml").write_text("default: allow\nrules: []\n")
        monkeypatch.setenv("MONKEYBOT_CONFIG", str(config_dir / "monkeybot.yaml"))
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path(str(config_dir / "permissions.yaml"))
        assert exc.value.kind == "policy"

    def test_must_exist_true_raises_when_missing(self, home: Path) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_user_path(str(home / "nope.txt"), must_exist=True)
        assert exc.value.kind == "validation"

    def test_must_exist_false_allows_missing(self, home: Path) -> None:
        resolved = safety.resolve_user_path(str(home / "new-file.txt"), must_exist=False)
        assert resolved == (home / "new-file.txt").resolve()


class TestIsPathDenied:
    def test_denies_outside_home(self, home: Path) -> None:
        assert safety.is_path_denied(Path("/etc/passwd"))

    def test_denies_credential_filename(self, home: Path) -> None:
        assert safety.is_path_denied(home / "id_rsa")

    def test_allows_ordinary_file(self, home: Path) -> None:
        assert not safety.is_path_denied(home / "notes.txt")

    def test_denies_denylisted_subdir(self, home: Path) -> None:
        assert safety.is_path_denied(home / ".ssh" / "config")


class TestExecSurfaceAndAppGuards:
    @pytest.mark.parametrize(
        "suffix", [".command", ".sh", ".app", ".scpt", ".workflow", ".pkg", ".dmg", ".py"]
    )
    def test_refuses_exec_suffixes(self, home: Path, suffix: str) -> None:
        target = home / f"thing{suffix}"
        target.write_text("x")
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.check_not_exec_surface(target)
        assert exc.value.kind == "policy"

    def test_allows_ordinary_document(self, home: Path) -> None:
        target = home / "report.pdf"
        target.write_text("x")
        safety.check_not_exec_surface(target)  # does not raise

    def test_refuses_executable_bit(self, home: Path) -> None:
        target = home / "script_no_suffix"
        target.write_text("#!/bin/sh\necho hi\n")
        target.chmod(0o755)
        with pytest.raises(safety.ComputerToolError):
            safety.check_not_exec_surface(target)

    @pytest.mark.parametrize(
        "name",
        ["Terminal", "terminal.app", "iTerm", "iTerm2", "Script Editor", "Automator", "Xcode"],
    )
    def test_refuses_denied_apps(self, name: str) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.check_app_name_allowed(name)
        assert exc.value.kind == "policy"

    def test_allows_ordinary_app_name(self) -> None:
        safety.check_app_name_allowed("Notes")  # does not raise


class TestValidateUrl:
    @pytest.mark.parametrize("url", ["https://example.com", "http://example.com", "mailto:a@b.com"])
    def test_allows(self, url: str) -> None:
        assert safety.validate_url(url) == url

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/passwd", "javascript:alert(1)", "x-custom-scheme://do-a-thing", "ftp://x"],
    )
    def test_rejects(self, url: str) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.validate_url(url)
        assert exc.value.kind == "policy"

    def test_rejects_empty(self) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.validate_url("")
        assert exc.value.kind == "validation"


class TestResolveAppBundle:
    def test_resolves_from_home_applications(self, home: Path) -> None:
        apps_dir = home / "Applications"
        apps_dir.mkdir()
        (apps_dir / "Notes.app").mkdir()
        resolved = safety.resolve_app_bundle("Notes")
        assert resolved == (apps_dir / "Notes.app").resolve()

    def test_rejects_denied_app_by_name(self, home: Path) -> None:
        apps_dir = home / "Applications"
        apps_dir.mkdir()
        (apps_dir / "Terminal.app").mkdir()
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_app_bundle("Terminal")
        assert exc.value.kind == "policy"

    def test_rejects_path_traversal_in_app_name(self, home: Path) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_app_bundle("../../Applications/Terminal")
        assert exc.value.kind == "validation"

    def test_missing_app_is_validation_error(self, home: Path) -> None:
        apps_dir = home / "Applications"
        apps_dir.mkdir()
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.resolve_app_bundle("NoSuchApp")
        assert exc.value.kind == "validation"


class TestCheckTrashable:
    def test_refuses_home_itself(self, home: Path) -> None:
        with pytest.raises(safety.ComputerToolError):
            safety.check_trashable(home)

    @pytest.mark.parametrize(
        "name", ["Desktop", "Documents", "Downloads", "Library", "Applications"]
    )
    def test_refuses_top_level_protected_dirs(self, home: Path, name: str) -> None:
        target = home / name
        target.mkdir()
        with pytest.raises(safety.ComputerToolError):
            safety.check_trashable(target)

    def test_allows_ordinary_file(self, home: Path) -> None:
        target = home / "Downloads" / "file.txt"
        target.parent.mkdir()
        target.write_text("x")
        safety.check_trashable(target)  # does not raise

    def test_refuses_too_many_items(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        big_dir = home / "Downloads" / "big"
        big_dir.mkdir(parents=True)
        (big_dir / "a.txt").write_text("x")
        monkeypatch.setattr(safety, "_TRASH_MAX_ITEMS", 0)
        with pytest.raises(safety.ComputerToolError):
            safety.check_trashable(big_dir)


class TestTrashPath:
    def test_moves_into_home_trash_not_deleted(self, home: Path) -> None:
        target = home / "Downloads" / "file.txt"
        target.parent.mkdir()
        target.write_text("hello")
        dest = safety.trash_path(target)
        assert not target.exists()
        assert dest.exists()
        assert dest.read_text() == "hello"
        assert dest.parent == (home / ".Trash").resolve()

    def test_collision_gets_unique_name(self, home: Path) -> None:
        trash_dir = home / ".Trash"
        trash_dir.mkdir()
        (trash_dir / "file.txt").write_text("already here")
        target = home / "Downloads" / "file.txt"
        target.parent.mkdir()
        target.write_text("new")
        dest = safety.trash_path(target)
        assert dest != trash_dir / "file.txt"
        assert dest.read_text() == "new"
        assert (trash_dir / "file.txt").read_text() == "already here"


class TestRunArgv:
    def test_rejects_non_absolute_binary(self) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.run_argv(["open", "/tmp"])
        assert exc.value.kind == "runtime"

    def test_runs_absolute_binary(self) -> None:
        result = safety.run_argv(["/bin/echo", "hi"])
        assert result.returncode == 0
        assert result.stdout.strip() == "hi"

    def test_timeout_raises(self) -> None:
        with pytest.raises(safety.ComputerToolError) as exc:
            safety.run_argv(["/bin/sleep", "2"], timeout=0.05)
        assert exc.value.kind == "runtime"


class TestOpenHelpersUseSafeArgv:
    """Verify argv shape without ever invoking the real ``open``/``pbcopy``/``pbpaste``."""

    def test_open_path_argv(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = home / "Downloads"
        target.mkdir()
        captured: dict[str, object] = {}

        def fake_run_argv(argv, **kwargs):
            captured["argv"] = argv
            return safety.RunResult(stdout="", stderr="", returncode=0)

        monkeypatch.setattr(safety, "run_argv", fake_run_argv)
        safety.open_path(target)
        assert captured["argv"] == ["/usr/bin/open", str(target)]

    def test_open_path_reveal_argv(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = home / "Downloads"
        target.mkdir()
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            safety,
            "run_argv",
            lambda argv, **kw: (captured.__setitem__("argv", argv), safety.RunResult("", "", 0))[1],
        )
        safety.open_path(target, reveal=True)
        assert captured["argv"] == ["/usr/bin/open", "-R", str(target)]

    def test_open_path_refuses_exec_surface(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = home / "script.command"
        target.write_text("x")
        monkeypatch.setattr(safety, "run_argv", lambda *a, **k: pytest.fail("must not exec"))
        with pytest.raises(safety.ComputerToolError):
            safety.open_path(target)

    def test_write_clipboard_uses_pbcopy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_run_argv(argv, *, input_text=None, timeout=safety.DEFAULT_TIMEOUT_SEC):
            captured["argv"] = argv
            captured["input_text"] = input_text
            return safety.RunResult("", "", 0)

        monkeypatch.setattr(safety, "run_argv", fake_run_argv)
        safety.write_clipboard("hello clipboard")
        assert captured["argv"] == ["/usr/bin/pbcopy"]
        assert captured["input_text"] == "hello clipboard"

    def test_read_clipboard_uses_pbpaste_and_caps_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            safety,
            "run_argv",
            lambda argv, **k: safety.RunResult("x" * (safety.MAX_CLIPBOARD_CHARS + 100), "", 0),
        )
        text = safety.read_clipboard()
        assert len(text) == safety.MAX_CLIPBOARD_CHARS
