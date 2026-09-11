"""Tests for OS-level filesystem isolation used to hide disabled memory."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from monkeybot.core.tools import fs_isolation
from monkeybot.core.tools.fs_isolation import (
    ISOLATION_ERROR_PREFIX,
    ISOLATION_FAILURE_EXIT_CODE,
    IsolationSupport,
    JailRoots,
    isolated_argv,
    isolation_failed,
    isolation_support,
    jailed_argv,
    memory_hidden_paths,
    reset_isolation_support_cache,
)


@pytest.fixture(autouse=True)
def _clear_support_cache():
    reset_isolation_support_cache()
    yield
    reset_isolation_support_cache()


class TestIsolationSupport:
    def test_support_is_probed_once_and_cached(self, monkeypatch):
        calls: list[int] = []

        def fake_detect() -> IsolationSupport:
            calls.append(1)
            return IsolationSupport("namespace", "fake")

        monkeypatch.setattr(fs_isolation, "_detect_support", fake_detect)

        assert isolation_support().mechanism == "namespace"
        assert isolation_support().mechanism == "namespace"
        assert len(calls) == 1

    def test_unsupported_platform_reports_unavailable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "sunos5")

        support = fs_isolation._detect_support()

        assert not support.available
        assert "sunos5" in support.detail

    def test_failed_probe_reports_unavailable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(fs_isolation, "_probe", lambda mechanism: False)

        support = fs_isolation._detect_support()

        assert not support.available
        assert "user namespaces" in support.detail


class TestIsolatedArgv:
    def test_namespace_argv_runs_bootstrap_with_hidden_paths(self):
        support = IsolationSupport("namespace", "test")

        executable, args = isolated_argv("/bin/cat", ["file"], [Path("/palace")], support=support)

        assert executable == sys.executable
        assert "-c" in args
        bootstrap = args[args.index("-c") + 1]
        assert "unshare" in bootstrap
        assert "/palace" in args[args.index("-c") + 2]
        assert args[-2:] == ["/bin/cat", "file"]

    def test_sandbox_exec_argv_denies_hidden_subpaths(self):
        support = IsolationSupport("sandbox-exec", "test")

        executable, args = isolated_argv("/bin/cat", ["file"], [Path("/palace")], support=support)

        assert executable == "sandbox-exec"
        assert args[0] == "-p"
        assert '(deny file-read* file-write* (subpath "/palace"))' in args[1]
        assert args[-2:] == ["/bin/cat", "file"]

    def test_sandbox_exec_rejects_seatbelt_metacharacters(self):
        support = IsolationSupport("sandbox-exec", "test")

        with pytest.raises(ValueError, match="seatbelt metacharacters"):
            isolated_argv(
                "/bin/cat",
                ["file"],
                [Path('/tmp/evil") (allow default)')],
                support=support,
            )

    def test_hidden_paths_are_resolved_before_wrapping(self, tmp_path):
        support = IsolationSupport("sandbox-exec", "test")
        hidden = tmp_path / "palace"
        hidden.mkdir()

        _, args = isolated_argv("/bin/cat", ["file"], [hidden], support=support)

        resolved = str(hidden.resolve())
        assert f'(subpath "{resolved}")' in args[1]

    def test_no_hidden_paths_is_a_passthrough(self):
        support = IsolationSupport("namespace", "test")

        assert isolated_argv("/bin/cat", ["file"], [], support=support) == ("/bin/cat", ["file"])

    def test_unavailable_support_refuses_to_pretend(self):
        with pytest.raises(ValueError, match="isolation is unavailable"):
            isolated_argv("/bin/cat", [], [Path("/palace")], support=IsolationSupport("none", "x"))


class TestIsolationFailureDetection:
    def test_marked_failure_is_detected(self):
        assert isolation_failed(ISOLATION_FAILURE_EXIT_CODE, f"{ISOLATION_ERROR_PREFIX} unshare")

    def test_unrelated_exit_code_is_not_a_failure(self):
        assert not isolation_failed(1, f"{ISOLATION_ERROR_PREFIX} unshare")

    def test_unrelated_stderr_is_not_a_failure(self):
        assert not isolation_failed(ISOLATION_FAILURE_EXIT_CODE, "permission denied")


class TestMemoryHiddenPaths:
    def test_covers_agent_palace_and_mempalace_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)
        monkeypatch.delenv("MEMORY_PATH", raising=False)
        monkeypatch.delenv("MEMORY_STORAGE_URI", raising=False)
        workspace = tmp_path / "agent" / "workspace"
        workspace.mkdir(parents=True)

        hidden = memory_hidden_paths(workspace)

        assert (tmp_path / "agent" / "memory").resolve() in hidden
        assert (Path.home() / ".mempalace").resolve() in hidden

    def test_includes_environment_configured_palace(self, tmp_path, monkeypatch):
        external = tmp_path / "elsewhere" / "palace"
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(external))
        monkeypatch.setenv("MEMORY_STORAGE_URI", f"local://{external}")
        workspace = tmp_path / "agent" / "workspace"
        workspace.mkdir(parents=True)

        hidden = memory_hidden_paths(workspace)

        assert external.resolve() in hidden
        assert len([path for path in hidden if path == external.resolve()]) == 1

    def test_ignores_remote_storage_uris(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_STORAGE_URI", "s3://bucket/palace")
        workspace = tmp_path / "agent" / "workspace"
        workspace.mkdir(parents=True)

        hidden = memory_hidden_paths(workspace)

        assert not any("bucket" in str(path) for path in hidden)

    def test_mac_workspace_override_does_not_hide_workspace(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)
        monkeypatch.delenv("MEMORY_PATH", raising=False)
        monkeypatch.delenv("MEMORY_STORAGE_URI", raising=False)
        workspace = tmp_path / "workspaces" / "ws1" / "memory"
        workspace.mkdir(parents=True)

        hidden = memory_hidden_paths(workspace)

        assert workspace.resolve() not in hidden
        assert (Path.home() / ".mempalace").resolve() in hidden


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="namespace bootstrap is linux-only",
)
class TestLinuxBootstrap:
    @pytest.fixture(autouse=True)
    def _require_live_namespace(self):
        """Skip when this host cannot actually map a user+mount namespace.

        Tests force ``IsolationSupport("namespace", ...)`` so they exercise the
        bootstrap path, but GitHub Actions (and other locked-down hosts) often
        reject ``uid_map`` writes. Probing first avoids false failures.
        """
        reset_isolation_support_cache()
        support = isolation_support()
        if support.mechanism != "namespace":
            pytest.skip(support.detail)

    def _run(self, hidden: list[str], inner: list[str]) -> subprocess.CompletedProcess[str]:
        executable, args = isolated_argv(
            inner[0],
            inner[1:],
            [Path(path) for path in hidden],
            support=IsolationSupport("namespace", "test"),
        )
        return subprocess.run([executable, *args], capture_output=True, text=True, timeout=60)

    def test_bootstrap_hides_directory_from_child(self, tmp_path):
        secret_dir = tmp_path / "memory"
        secret_dir.mkdir()
        (secret_dir / "private.txt").write_text("PRIVATE-CONTENT", encoding="utf-8")

        proc = self._run(
            [str(secret_dir)],
            ["/bin/sh", "-c", f"cat {secret_dir}/private.txt"],
        )

        assert proc.returncode == 0
        assert "PRIVATE-CONTENT" not in proc.stdout
        assert (secret_dir / "private.txt").read_text(encoding="utf-8") == "PRIVATE-CONTENT"

    def test_bootstrap_reports_failure_instead_of_running_command(self, tmp_path):
        proc = self._run([str(tmp_path)], ["/nonexistent/binary"])

        assert proc.returncode == ISOLATION_FAILURE_EXIT_CODE
        assert ISOLATION_ERROR_PREFIX in proc.stderr
        assert isolation_failed(proc.returncode, proc.stderr)

    def test_bootstrap_hides_directory_that_does_not_exist_yet(self, tmp_path):
        secret_dir = tmp_path / "memory"
        check = (
            "import os, sys; "
            "path = sys.argv[1]; "
            "sys.exit(0 if os.path.isdir(path) and not os.listdir(path) else 4)"
        )

        proc = self._run(
            [str(secret_dir)],
            [sys.executable, "-c", check, str(secret_dir)],
        )

        assert proc.returncode == 0
        # Mount namespaces share the directory tree with the host: preparing
        # the mount point creates secret_dir on the host. It must stay
        # owner-writable so a later memory-on session can initialize into it.
        assert secret_dir.is_dir()
        assert secret_dir.stat().st_mode & 0o200
        (secret_dir / "init.txt").write_text("ok", encoding="utf-8")


class TestJailRoots:
    def test_is_empty(self):
        assert JailRoots().is_empty()
        assert not JailRoots(deny=(Path("/home/x"),)).is_empty()
        assert not JailRoots(read_write=(Path("/ws"),)).is_empty()
        assert not JailRoots(read_only=(Path("/skills"),)).is_empty()
        assert not JailRoots(shared_write=(Path("/tmp"),)).is_empty()
        assert not JailRoots(always_deny=(Path("/ws/palace"),)).is_empty()


class TestJailedArgv:
    def test_empty_roots_is_a_passthrough(self):
        support = IsolationSupport("namespace", "test")
        assert jailed_argv("/bin/cat", ["file"], JailRoots(), support=support) == (
            "/bin/cat",
            ["file"],
        )

    def test_unavailable_support_refuses_to_pretend(self):
        roots = JailRoots(deny=(Path("/home/x"),))
        with pytest.raises(ValueError, match="isolation is unavailable"):
            jailed_argv("/bin/cat", [], roots, support=IsolationSupport("none", "x"))

    def test_sandbox_exec_profile_denies_home_and_allows_workspace(self):
        support = IsolationSupport("sandbox-exec", "test")
        roots = JailRoots(
            deny=(Path("/Users/x"),),
            read_write=(Path("/Users/x/agent/workspace"),),
            read_only=(Path("/Users/x/agent/skills"),),
        )

        executable, args = jailed_argv("/bin/bash", ["-c", "true"], roots, support=support)

        assert executable == "sandbox-exec"
        profile = args[1]
        assert "(deny default)" in profile
        assert '(require-not (subpath "/Users/x"))' in profile
        assert '(allow file-read* file-write* (subpath "/Users/x/agent/workspace"))' in profile
        assert '(allow file-read* (subpath "/Users/x/agent/skills"))' in profile
        assert args[-2:] == ["/bin/bash", "-c"] or args[-3:] == ["/bin/bash", "-c", "true"]

    def test_sandbox_exec_profile_allows_gitconfig_by_name(self, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))
        support = IsolationSupport("sandbox-exec", "test")
        roots = JailRoots(deny=(Path("/Users/x"),))

        _, args = jailed_argv("/bin/true", [], roots, support=support)

        profile = args[1]
        assert '(allow file-read* (literal "/Users/x/.gitconfig"))' in profile

    def test_sandbox_exec_profile_rejects_metacharacters(self):
        support = IsolationSupport("sandbox-exec", "test")
        roots = JailRoots(deny=(Path('/tmp/evil") (allow default) (deny file-read*'),))
        with pytest.raises(ValueError, match="seatbelt metacharacters"):
            jailed_argv("/bin/true", [], roots, support=support)

    def test_namespace_argv_carries_full_spec(self):
        support = IsolationSupport("namespace", "test")
        roots = JailRoots(
            deny=(Path("/home/x"),),
            read_write=(Path("/home/x/ws"),),
            read_only=(Path("/home/x/skills"),),
        )

        executable, args = jailed_argv("/bin/true", [], roots, support=support)

        assert executable == sys.executable
        bootstrap = args[args.index("-c") + 1]
        assert "unshare" in bootstrap
        spec_json = args[args.index("-c") + 2]
        assert "/home/x/ws" in spec_json
        assert "/home/x/skills" in spec_json
        assert "/home/x" in spec_json

    def test_always_deny_is_emitted_last_in_seatbelt_profile(self):
        """`always_deny` must be able to override `read_write`/`read_only`
        allows nested inside it — seatbelt is last-match-wins, so the deny
        line has to come after those allows in the generated profile."""
        support = IsolationSupport("sandbox-exec", "test")
        roots = JailRoots(
            deny=(Path("/Users/x"),),
            read_write=(Path("/Users/x/agent/workspace"),),
            always_deny=(Path("/Users/x/agent/workspace/palace"),),
        )

        _, args = jailed_argv("/bin/true", [], roots, support=support)

        profile = args[1]
        read_write_idx = profile.index(
            '(allow file-read* file-write* (subpath "/Users/x/agent/workspace"))'
        )
        always_deny_idx = profile.index(
            '(deny file-read* file-write* (subpath "/Users/x/agent/workspace/palace"))'
        )
        assert always_deny_idx > read_write_idx

    def test_namespace_argv_carries_always_deny(self):
        support = IsolationSupport("namespace", "test")
        roots = JailRoots(
            deny=(Path("/home/x"),),
            read_write=(Path("/home/x/ws"),),
            always_deny=(Path("/home/x/ws/palace"),),
        )

        _, args = jailed_argv("/bin/true", [], roots, support=support)

        spec_json = args[args.index("-c") + 2]
        assert "/home/x/ws/palace" in spec_json


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt jail is macOS-only")
class TestMacJailBootstrap:
    """Real `sandbox-exec` subprocess tests — see fs_isolation.py's module
    docstring: this exact profile shape was validated by hand against real
    python3/git/bash invocations before being written into code."""

    def _run(self, roots: JailRoots, argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        executable, args = jailed_argv(
            argv[0], argv[1:], roots, support=IsolationSupport("sandbox-exec", "test")
        )
        return subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=30, cwd=str(cwd)
        )

    def test_denied_home_file_is_unreadable(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        secret = home / "secret.txt"
        secret.write_text("HOME-SECRET", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        proc = self._run(roots, ["/bin/cat", str(secret)], cwd=workspace)

        assert proc.returncode != 0
        assert "HOME-SECRET" not in proc.stdout
        assert secret.read_text(encoding="utf-8") == "HOME-SECRET"

    def test_read_write_root_inside_deny_root_is_usable(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "in.txt").write_text("workspace-content", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        read = self._run(roots, ["/bin/cat", "in.txt"], cwd=workspace)
        assert read.returncode == 0
        assert "workspace-content" in read.stdout

        write = self._run(
            roots, ["/bin/bash", "-c", "echo written > out.txt"], cwd=workspace
        )
        assert write.returncode == 0
        assert (workspace / "out.txt").read_text(encoding="utf-8").strip() == "written"

    def test_read_only_root_rejects_write(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        skills = home / "agent" / "skills"
        workspace.mkdir(parents=True)
        skills.mkdir(parents=True)
        (skills / "s.py").write_text("# skill", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,), read_only=(skills,))
        read = self._run(roots, ["/bin/cat", str(skills / "s.py")], cwd=workspace)
        assert read.returncode == 0
        assert "# skill" in read.stdout

        write = self._run(
            roots, ["/bin/bash", "-c", f"echo x > {skills}/new.py"], cwd=workspace
        )
        assert write.returncode != 0
        assert not (skills / "new.py").exists()

    def test_write_outside_every_allowed_root_is_denied(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        proc = self._run(
            roots, ["/bin/bash", "-c", f"echo pwned > {outside}/pwned.txt"], cwd=workspace
        )

        assert proc.returncode != 0
        assert not (outside / "pwned.txt").exists()

    def test_home_expansion_escape_is_closed(self, tmp_path, monkeypatch):
        """Regression test for the exact reported bug: a shell expression that
        only resolves `$HOME` at exec time (defeating argv-string screening)
        must still be blocked by the OS-level jail, not by argument parsing."""
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        desktop = home / "Desktop"
        desktop.mkdir()
        (desktop / "secret.pdf").write_text("PDF-BYTES", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        executable, args = jailed_argv(
            "/bin/bash",
            ["-c", 'cp "$HOME/Desktop/secret.pdf" .'],
            roots,
            support=IsolationSupport("sandbox-exec", "test"),
        )
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(workspace),
            env={**__import__("os").environ, "HOME": str(home)},
        )

        assert proc.returncode != 0
        assert not (workspace / "secret.pdf").exists()

    def test_python_interpreter_starts_under_the_jail(self, tmp_path):
        """Regression test for the interpreter-bootstrap EPERM crash found
        while validating this profile: Python's own startup scans sys.path,
        and denying (rather than hiding) a stray entry under `deny` must not
        turn into an uncaught PermissionError before user code even runs."""
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        proc = self._run(roots, [sys.executable, "-c", "print('jail-ok')"], cwd=workspace)

        assert proc.returncode == 0
        assert "jail-ok" in proc.stdout

    def test_git_commit_works_inside_jail(self, tmp_path):
        """Regression test: git reads ~/.gitconfig unconditionally on
        startup; without the narrow _HOME_DOTFILE_READ_EXCEPTIONS carve-out
        this fails even for `git --version`."""
        if subprocess.run(["which", "git"], capture_output=True).returncode != 0:
            pytest.skip("git not installed")
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "in.txt").write_text("x", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        script = (
            "git init -q && "
            "git -c user.email=a@b.c -c user.name=a add in.txt && "
            "git -c user.email=a@b.c -c user.name=a commit -q -m test && "
            "git log --oneline"
        )
        proc = self._run(roots, ["/bin/bash", "-c", script], cwd=workspace)

        assert proc.returncode == 0, proc.stderr
        assert "test" in proc.stdout

    def test_network_still_works_under_the_jail(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        proc = self._run(
            roots,
            [sys.executable, "-c", "import socket; socket.gethostbyname('localhost')"],
            cwd=workspace,
        )
        assert proc.returncode == 0, proc.stderr

    def test_shared_write_root_is_usable(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        shared = tmp_path / "shared-tmp"
        shared.mkdir()

        roots = JailRoots(deny=(home,), read_write=(workspace,), shared_write=(shared,))
        proc = self._run(
            roots, ["/bin/bash", "-c", f"echo x > {shared}/out.txt"], cwd=workspace
        )

        assert proc.returncode == 0, proc.stderr
        assert (shared / "out.txt").read_text(encoding="utf-8").strip() == "x"

    def test_deny_root_nested_inside_shared_write_stays_hidden(self, tmp_path):
        """Regression test: a memory palace deliberately relocated under a
        shared_write root (e.g. a FaaS deployment putting it under /tmp)
        must stay hidden even though the shared_write root itself is
        writable — see JailRoots' docstring on why this is a separate
        category from read_write."""
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        shared = tmp_path / "shared-tmp"
        palace = shared / "faas" / "memory"
        palace.mkdir(parents=True)
        (palace / "secret.txt").write_text("PALACE-SECRET", encoding="utf-8")

        roots = JailRoots(
            deny=(home, palace), read_write=(workspace,), shared_write=(shared,)
        )
        read = self._run(roots, ["/bin/cat", str(palace / "secret.txt")], cwd=workspace)
        assert read.returncode != 0
        assert "PALACE-SECRET" not in read.stdout

        # The rest of the shared_write root remains usable.
        write = self._run(
            roots, ["/bin/bash", "-c", f"echo x > {shared}/scratch.txt"], cwd=workspace
        )
        assert write.returncode == 0
        assert (shared / "scratch.txt").exists()

    def test_always_deny_root_nested_inside_read_write_stays_hidden(self, tmp_path):
        """Regression test: a memory palace can be configured to live
        *inside* the workspace (`MEMORY_PATH`/`MEMORY_STORAGE_URI` pointing
        there). Plain `deny` is deliberately allowed to lose to `read_write`
        when nested inside it (the common case: a workspace under the
        denied home directory) — but `always_deny` must win regardless,
        since it's used for paths that must never be exposed no matter
        where they end up."""
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        palace = workspace / "memory-palace"
        palace.mkdir(parents=True)
        (palace / "secret.txt").write_text("PALACE-SECRET", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,), always_deny=(palace,))
        read = self._run(roots, ["/bin/cat", str(palace / "secret.txt")], cwd=workspace)
        assert read.returncode != 0
        assert "PALACE-SECRET" not in read.stdout

        # The rest of the workspace remains usable.
        (workspace / "in.txt").write_text("workspace-content", encoding="utf-8")
        other = self._run(roots, ["/bin/cat", "in.txt"], cwd=workspace)
        assert other.returncode == 0
        assert "workspace-content" in other.stdout


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="namespace jail bootstrap is linux-only",
)
class TestLinuxJailBootstrap:
    """Mirrors TestLinuxBootstrap's skip/probe discipline. Not exercised on
    the darwin host this change was developed and validated on — see the
    module docstring's fail-closed rationale."""

    @pytest.fixture(autouse=True)
    def _require_live_namespace(self):
        reset_isolation_support_cache()
        support = isolation_support()
        if support.mechanism != "namespace":
            pytest.skip(support.detail)

    def _run(self, roots: JailRoots, argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        executable, args = jailed_argv(
            argv[0], argv[1:], roots, support=IsolationSupport("namespace", "test")
        )
        return subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=60, cwd=str(cwd)
        )

    def test_denied_home_file_is_unreadable(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        secret = home / "secret.txt"
        secret.write_text("HOME-SECRET", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        proc = self._run(roots, ["/bin/cat", str(secret)], cwd=workspace)

        assert proc.returncode != 0
        assert "HOME-SECRET" not in proc.stdout

    def test_read_write_root_inside_deny_root_is_usable(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "in.txt").write_text("content", encoding="utf-8")

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        proc = self._run(roots, ["/bin/cat", "in.txt"], cwd=workspace)

        assert proc.returncode == 0
        assert "content" in proc.stdout

    def test_write_outside_every_allowed_root_is_denied(self, tmp_path):
        home = tmp_path / "home"
        workspace = home / "agent" / "workspace"
        workspace.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()

        roots = JailRoots(deny=(home,), read_write=(workspace,))
        proc = self._run(
            roots, ["/bin/bash", "-c", f"echo pwned > {outside}/pwned.txt"], cwd=workspace
        )

        assert proc.returncode != 0
        assert not (outside / "pwned.txt").exists()
