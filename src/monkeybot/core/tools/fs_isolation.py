"""OS-level filesystem isolation for child processes.

Argument inspection cannot keep a secret from a shell or an interpreter. Once
``bash`` or ``python`` is running it can build any path at runtime — from a
variable, from stdin, from a glob — so no amount of argv parsing can decide
what a child is allowed to read. When the agent must not see a directory
(memory is disabled, but ``bash`` remains a legitimate tool), the directory is
removed from the child's *view of the filesystem* instead.

Mechanisms, in order of preference:

``namespace``
    Linux unprivileged user + mount namespace. An empty read-only tmpfs is
    mounted over each hidden directory, so the child sees an empty directory
    no matter how it spells the path. Mounts are made private first, so the
    host namespace is unaffected.

``sandbox-exec``
    macOS seatbelt profile denying every file operation on the hidden
    subpaths.

When neither is available the caller is told so and must decide; this module
never silently pretends a path is hidden.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# The bootstrap exits with this code when it cannot establish isolation, so a
# child never runs with a hidden path still visible.
ISOLATION_FAILURE_EXIT_CODE = 126
ISOLATION_ERROR_PREFIX = "monkeybot-isolation:"

_PROBE_TIMEOUT_SEC = 15.0

# Runs as its own single-threaded process: unshare(CLONE_NEWUSER) is rejected
# for multi-threaded callers, which rules out doing this in the gateway via a
# subprocess preexec_fn.
_LINUX_BOOTSTRAP = r"""
import ctypes, json, os, sys

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_REC = 16384
MS_PRIVATE = 1 << 18
PREFIX = "monkeybot-isolation:"


def _fail(message):
    sys.stderr.write(PREFIX + " " + message + "\n")
    raise SystemExit(126)


hidden = json.loads(sys.argv[1])
argv = sys.argv[2:]
if not argv:
    _fail("no command to execute")

libc = ctypes.CDLL(None, use_errno=True)
libc.unshare.argtypes = [ctypes.c_int]
libc.mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_void_p,
]

uid = os.getuid()
gid = os.getgid()
if libc.unshare(CLONE_NEWNS | CLONE_NEWUSER) != 0:
    _fail("unshare failed: " + os.strerror(ctypes.get_errno()))
try:
    with open("/proc/self/setgroups", "w") as fh:
        fh.write("deny")
except OSError:
    pass
try:
    with open("/proc/self/uid_map", "w") as fh:
        fh.write("0 %d 1" % uid)
    with open("/proc/self/gid_map", "w") as fh:
        fh.write("0 %d 1" % gid)
except OSError as exc:
    _fail("cannot map namespace user: " + str(exc))
if libc.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None) != 0:
    _fail("cannot detach mount namespace: " + os.strerror(ctypes.get_errno()))

for target in hidden:
    # Mount namespaces isolate mounts, not the directory tree: makedirs here
    # still creates the path on the host. Use owner-writable mode so a later
    # memory-on session can initialize the palace; never leave a 0o500 stub.
    # Mounting over the nearest existing ancestor would avoid creation but
    # would also hide siblings (workspace under the agent dir, or $HOME).
    if not os.path.isdir(target):
        try:
            os.makedirs(target, mode=0o700, exist_ok=True)
        except OSError as exc:
            _fail("cannot prepare hide mount point " + target + ": " + str(exc))
    flags = MS_RDONLY | MS_NOSUID | MS_NODEV
    if libc.mount(b"tmpfs", target.encode(), b"tmpfs", flags, b"size=0,mode=0500") != 0:
        _fail("cannot hide " + target + ": " + os.strerror(ctypes.get_errno()))

try:
    if os.sep in argv[0]:
        os.execv(argv[0], argv)
    else:
        os.execvp(argv[0], argv)
except OSError as exc:
    _fail("cannot exec " + argv[0] + ": " + str(exc))
"""


@dataclass(frozen=True)
class IsolationSupport:
    """Which isolation mechanism this host can actually use.

    ``detail`` explains the outcome and is safe to log or surface in an error.
    """

    mechanism: str
    detail: str

    @property
    def available(self) -> bool:
        return self.mechanism != "none"


_SEATBELT_PATH_METACHARS = frozenset('"()\\')


def _seatbelt_subpath(path: str) -> str:
    """Quote a path for a seatbelt ``(subpath "...")`` form.

    Reject profile metacharacters rather than inventing an escape dialect:
    a crafted ``MEMPALACE_PALACE_PATH`` must not close the deny early and
    re-open ``(allow ...)``. Rejection is fail-closed (caller refuses exec).
    """
    if _SEATBELT_PATH_METACHARS.intersection(path):
        raise ValueError(
            "hidden path contains seatbelt metacharacters and cannot be isolated: "
            + repr(path)
        )
    return path


def _sandbox_exec_profile(hidden: Sequence[str]) -> str:
    denies = "\n".join(
        f'(deny file-read* file-write* (subpath "{_seatbelt_subpath(path)}"))'
        for path in hidden
    )
    return f"(version 1)\n(allow default)\n{denies}\n"


def _namespace_argv(executable: str, args: Sequence[str], hidden: Sequence[str]) -> list[str]:
    # -S/-E keep the bootstrap's own startup minimal; it needs only the stdlib
    # and must not be reconfigured by the environment it is sandboxing.
    return [
        sys.executable,
        "-S",
        "-E",
        "-c",
        _LINUX_BOOTSTRAP,
        json.dumps(list(hidden)),
        executable,
        *args,
    ]


def _sandbox_exec_argv(executable: str, args: Sequence[str], hidden: Sequence[str]) -> list[str]:
    return ["sandbox-exec", "-p", _sandbox_exec_profile(hidden), executable, *args]


def _probe(mechanism: str) -> bool:
    """Verify a mechanism really hides a directory before we rely on it."""
    with tempfile.TemporaryDirectory(prefix="monkeybot-isolation-probe-") as tmp:
        secret_dir = Path(tmp) / "secret"
        secret_dir.mkdir()
        secret_file = secret_dir / "probe.txt"
        secret_file.write_text("probe", encoding="utf-8")
        check = "import os, sys; sys.exit(0 if not os.path.exists(sys.argv[1]) else 3)"
        inner = [sys.executable, "-c", check, str(secret_file.resolve())]
        hidden = [secret_dir]
        argv_exec, argv_rest = isolated_argv(
            inner[0],
            inner[1:],
            hidden,
            support=IsolationSupport(mechanism, "probe"),
        )
        argv = [argv_exec, *argv_rest]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0


def _detect_support() -> IsolationSupport:
    if sys.platform.startswith("linux"):
        if _probe("namespace"):
            return IsolationSupport("namespace", "linux user + mount namespace")
        return IsolationSupport(
            "none",
            "unprivileged user namespaces are unavailable on this host",
        )
    if sys.platform == "darwin":
        if _probe("sandbox-exec"):
            return IsolationSupport("sandbox-exec", "macos seatbelt profile")
        return IsolationSupport("none", "sandbox-exec is unavailable on this host")
    return IsolationSupport("none", f"no supported mechanism for platform {sys.platform!r}")


_cached_support: IsolationSupport | None = None


def isolation_support() -> IsolationSupport:
    """Detect (once per process) how this host can hide directories.

    Probing runs a real child process, so callers in async code should offload
    the first call to a thread.
    """
    global _cached_support
    if _cached_support is None:
        _cached_support = _detect_support()
        logger.info(
            "filesystem isolation support: %s (%s)",
            _cached_support.mechanism,
            _cached_support.detail,
        )
    return _cached_support


def reset_isolation_support_cache() -> None:
    """Forget the probe result (tests that simulate other hosts)."""
    global _cached_support
    _cached_support = None


def isolated_argv(
    executable: str,
    args: Sequence[str],
    hidden_paths: Sequence[Path | str],
    *,
    support: IsolationSupport,
) -> tuple[str, list[str]]:
    """Rewrite an argv so the child cannot see ``hidden_paths``.

    Hidden paths are resolved so macOS seatbelt subpaths match the real
    location (``/var/folders`` → ``/private/var/folders``). Missing paths are
    still passed through: the child must not be able to observe the directory
    even if it is created between planning and exec.
    """
    if not support.available:
        raise ValueError(f"isolation is unavailable: {support.detail}")
    hidden = [str(Path(path).expanduser().resolve()) for path in hidden_paths]
    if not hidden:
        return executable, list(args)
    if support.mechanism == "namespace":
        argv = _namespace_argv(executable, args, hidden)
    else:
        argv = _sandbox_exec_argv(executable, args, hidden)
    return argv[0], argv[1:]


def isolation_failed(exit_code: int, stderr: str) -> bool:
    """Whether a finished child aborted because isolation could not be set up."""
    return exit_code == ISOLATION_FAILURE_EXIT_CODE and ISOLATION_ERROR_PREFIX in stderr


def memory_hidden_paths(workspace_root: Path) -> tuple[Path, ...]:
    """Directories that must be invisible to children when memory is disabled.

    Covers the agent's own palace, any palace pointed at by the environment,
    and the shared MemPalace home that holds identity and config.
    """
    resolved_workspace = workspace_root.expanduser().resolve()
    agent_palace = (workspace_root / ".." / "memory").expanduser().resolve()
    # Standard agent layout: <agent>/workspace with palace at <agent>/memory. Mac
    # workspace overrides remap onto .../workspaces/<id>/memory, so ../memory
    # collapses back to the workspace itself and must not be treated as a palace.
    candidates: list[Path] = []
    if agent_palace != resolved_workspace:
        candidates.append(agent_palace)
    try:
        candidates.append(Path.home() / ".mempalace")
    except RuntimeError:
        logger.debug("no home directory to hide; skipping the shared MemPalace home")
    for key in ("MEMPALACE_PALACE_PATH", "MEMORY_PATH", "MEMORY_STORAGE_URI"):
        raw = os.environ.get(key, "").strip()
        # Remote palaces (gcs://, s3://) have no local directory to hide.
        if not raw or ("://" in raw and not raw.startswith("local://")):
            continue
        candidates.append(Path(raw.removeprefix("local://")).expanduser())
    resolved: list[Path] = []
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path not in resolved:
            resolved.append(path)
    return tuple(resolved)
