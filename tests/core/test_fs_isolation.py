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
    isolated_argv,
    isolation_failed,
    isolation_support,
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
