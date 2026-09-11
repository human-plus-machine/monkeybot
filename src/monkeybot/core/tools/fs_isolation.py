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

``jailed_argv`` builds the same two mechanisms into a *deny-by-default*
profile instead: read is allowed everywhere except a set of ``deny`` roots
(the user's home directory, primarily), read+write is granted on top for a
small set of ``read_write`` roots (the workspace, artifacts, memory palace,
OS temp dirs), and read-only for ``read_only`` roots (skills, the interpreter
prefix, granted folders) even when those happen to sit inside a denied root —
which is the common case for a desktop app whose workspace and bundled
Python both live under the user's home directory. Validated empirically
against real `python3`, `git`, `uv`, and `bash` invocations under
`sandbox-exec` on macOS; the Linux mount-namespace path follows the same
allow-on-top-of-deny structure but has not been exercised on a real Linux
host as part of this change — see the fail-closed contract on
``jail_available`` below, which means a bug here degrades to "run_command
refuses to run" rather than a silent bypass.
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
            "hidden path contains seatbelt metacharacters and cannot be isolated: " + repr(path)
        )
    return path


def _sandbox_exec_profile(hidden: Sequence[str]) -> str:
    denies = "\n".join(
        f'(deny file-read* file-write* (subpath "{_seatbelt_subpath(path)}"))' for path in hidden
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


# Single-file, low-sensitivity exceptions to a `deny` root, needed because
# common tools read them unconditionally during startup — empirically found
# by running `git --version`/`git status`/`git commit` under a deny-$HOME
# seatbelt profile: git reads its own global identity/preferences before
# doing anything else, and fails hard (not gracefully) if that read is
# denied rather than simply absent. Deliberately narrow: only per-user
# *preferences* (name, email, aliases, ignore patterns), never credential
# material — `.git-credentials`, SSH keys, and `gh`'s `hosts.yml` stay
# denied, matching the credential-path philosophy already established in
# `computer/safety.py`'s denylist.
_HOME_DOTFILE_READ_EXCEPTIONS: tuple[str, ...] = (
    ".gitconfig",
    ".config/git/config",
    ".config/git/ignore",
)

# Always-needed device nodes: stdio redirection, /dev/null discards,
# randomness for anything that seeds a PRNG (uv, git object hashing tools,
# TLS). None of these leak filesystem contents.
_JAIL_DEVICE_NODES: tuple[str, ...] = (
    "/dev/null",
    "/dev/zero",
    "/dev/urandom",
    "/dev/random",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
)


@dataclass(frozen=True)
class JailRoots:
    """Deny-by-default filesystem policy for one command.

    ``deny`` roots (typically just the user's home directory) are unreadable
    and unwritable by default. Everywhere outside every ``deny`` root stays
    readable (the host's normal files aren't secret) but is never writable
    unless explicitly granted — the write restriction is unconditional, not
    scoped to ``deny``, so a command cannot write into arbitrary host
    locations it happens to have OS permission for.

    Two different kinds of grant sit on top of ``deny``, and they resolve a
    nested conflict oppositely on purpose:

    ``read_write`` / ``read_only``
        Specific, deliberate roots — the workspace, artifacts, the memory
        palace, skills, the interpreter's own install prefix, a folder the
        user explicitly granted. These *always* win over ``deny``, including
        when nested inside it — the normal case for a desktop app whose
        workspace and bundled Python both live under the user's home
        directory, or a folder grant, which by definition lives inside home.

    ``shared_write``
        Broad, generic infrastructure roots — OS temp directories — that
        many toolchains stage into but that nobody deliberately chose. These
        respect ``deny``: a memory palace explicitly relocated under
        ``/tmp`` (a real, supported deployment shape — see
        ``memory_hidden_paths``) must stay hidden even though ``/tmp``
        itself is writable. Putting temp dirs in ``read_write`` instead
        would silently reopen exactly that case.

    ``always_deny``
        Roots that must stay hidden no matter what — including nested
        inside ``read_write``/``read_only``/``shared_write``. This is the
        opposite precedence from plain ``deny``, which ``read_write``/
        ``read_only`` are deliberately allowed to override (a workspace
        living under the denied home directory is the common case). Used
        for the memory-off hide list (``TerminalExecutor._hidden_paths``):
        a memory palace can be configured to live *inside* the workspace
        (``MEMORY_PATH``/``MEMORY_STORAGE_URI`` pointing there), and folding
        it into ``deny`` would let the workspace's own ``read_write`` grant
        re-expose it.
    """

    read_write: tuple[Path, ...] = ()
    read_only: tuple[Path, ...] = ()
    shared_write: tuple[Path, ...] = ()
    deny: tuple[Path, ...] = ()
    always_deny: tuple[Path, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.read_write or self.read_only or self.shared_write or self.deny or self.always_deny
        )


def _resolved_strs(paths: Sequence[Path]) -> list[str]:
    return [str(Path(p).expanduser().resolve()) for p in paths]


def _seatbelt_jail_profile(roots: JailRoots) -> str:
    deny = _resolved_strs(roots.deny)
    read_write = _resolved_strs(roots.read_write)
    read_only = _resolved_strs(roots.read_only)
    shared_write = _resolved_strs(roots.shared_write)
    always_deny = _resolved_strs(roots.always_deny)
    deny_excludes = " ".join(f'(require-not (subpath "{_seatbelt_subpath(p)}"))' for p in deny)

    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target self))",
        "(allow mach-lookup)",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        "(allow ipc-posix-shm)",
    ]
    if deny:
        lines.append(f'(allow file-read* (require-all (subpath "/") {deny_excludes}))')
    else:
        lines.append('(allow file-read* (subpath "/"))')

    device_literals = " ".join(f'(literal "{p}")' for p in _JAIL_DEVICE_NODES)
    lines.append(f"(allow file-read* file-write* {device_literals})")

    for rel in _HOME_DOTFILE_READ_EXCEPTIONS:
        try:
            home_path = (Path.home() / rel).resolve()
        except RuntimeError:
            continue
        lines.append(f'(allow file-read* (literal "{_seatbelt_subpath(str(home_path))}"))')

    lines.append("(allow network*)")

    # Explicit, deliberate roots always win over `deny`, including nested
    # inside it (see JailRoots docstring).
    for p in read_write:
        lines.append(f'(allow file-read* file-write* (subpath "{_seatbelt_subpath(p)}"))')
    for p in read_only:
        lines.append(f'(allow file-read* (subpath "{_seatbelt_subpath(p)}"))')

    # Generic infrastructure roots respect `deny` — a memory palace
    # deliberately relocated under /tmp must stay hidden even though /tmp
    # itself is writable (see JailRoots docstring).
    for p in shared_write:
        subpath = _seatbelt_subpath(p)
        if deny:
            lines.append(
                f'(allow file-read* file-write* (require-all (subpath "{subpath}") {deny_excludes}))'
            )
        else:
            lines.append(f'(allow file-read* file-write* (subpath "{subpath}"))')

    # Emitted last so seatbelt's last-match-wins semantics make these win
    # over every allow above, including `read_write`/`read_only` nested
    # inside one of these roots (see JailRoots docstring's `always_deny`).
    for p in always_deny:
        subpath = _seatbelt_subpath(p)
        lines.append(f'(deny file-read* file-write* (subpath "{subpath}"))')

    return "\n".join(lines) + "\n"


def _sandbox_exec_jail_argv(executable: str, args: Sequence[str], roots: JailRoots) -> list[str]:
    return ["sandbox-exec", "-p", _seatbelt_jail_profile(roots), executable, *args]


# Same single-threaded-bootstrap constraint as `_LINUX_BOOTSTRAP` (unshare
# rejects multi-threaded callers). Structure, in order:
#   1. unshare + uid/gid map + detach mount propagation — identical to the
#      hide-only bootstrap.
#   2. Bind-mount "/" onto itself, then remount that bind read-only,
#      recursively. A plain mount cannot have its flags changed to read-only
#      directly; making it a bind mount of itself first is the standard
#      (bubblewrap-style) two-step this requires.
#   3. Mount an empty read-only tmpfs over each `deny` root (same primitive
#      as the hide-only bootstrap).
#   4. Bind-mount each `read_write` root onto itself (a fresh bind mount is
#      read-write regardless of the read-only tree it sits inside or the
#      tmpfs it may be nested under), and each `read_only` root onto itself
#      then remounted read-only (bind-mounting with MS_RDONLY set on the
#      initial call is ignored by the kernel; it needs the same two-step).
# Unvalidated on a real Linux host as part of this change (see module
# docstring) — every mount() call is checked and calls `_fail()` (exit 126,
# the isolation-failure marker) on error, so a bug here means "refuses to
# run", never a silent bypass.
_LINUX_JAIL_BOOTSTRAP = r"""
import ctypes, json, os, sys

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_REC = 16384
MS_BIND = 4096
MS_REMOUNT = 32
MS_PRIVATE = 1 << 18
PREFIX = "monkeybot-isolation:"


def _fail(message):
    sys.stderr.write(PREFIX + " " + message + "\n")
    raise SystemExit(126)


spec = json.loads(sys.argv[1])
deny = spec["deny"]
read_write = spec["read_write"]
read_only = spec["read_only"]
shared_write = spec["shared_write"]
always_deny = spec["always_deny"]
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

if libc.mount(b"/", b"/", None, MS_BIND | MS_REC, None) != 0:
    _fail("cannot self-bind root: " + os.strerror(ctypes.get_errno()))
if libc.mount(None, b"/", None, MS_REMOUNT | MS_BIND | MS_REC | MS_RDONLY, None) != 0:
    _fail("cannot make root read-only: " + os.strerror(ctypes.get_errno()))

for target in deny:
    if not os.path.isdir(target):
        try:
            os.makedirs(target, mode=0o700, exist_ok=True)
        except OSError as exc:
            _fail("cannot prepare deny mount point " + target + ": " + str(exc))
    flags = MS_RDONLY | MS_NOSUID | MS_NODEV
    if libc.mount(b"tmpfs", target.encode(), b"tmpfs", flags, b"size=0,mode=0500") != 0:
        _fail("cannot deny " + target + ": " + os.strerror(ctypes.get_errno()))

for target in read_write:
    if not os.path.isdir(target):
        try:
            os.makedirs(target, mode=0o700, exist_ok=True)
        except OSError as exc:
            _fail("cannot prepare read_write mount point " + target + ": " + str(exc))
    enc = target.encode()
    if libc.mount(enc, enc, None, MS_BIND | MS_REC, None) != 0:
        _fail("cannot expose read_write " + target + ": " + os.strerror(ctypes.get_errno()))

for target in read_only:
    if not os.path.isdir(target):
        continue
    enc = target.encode()
    if libc.mount(enc, enc, None, MS_BIND | MS_REC, None) != 0:
        _fail("cannot expose read_only " + target + ": " + os.strerror(ctypes.get_errno()))
    if libc.mount(None, enc, None, MS_REMOUNT | MS_BIND | MS_REC | MS_RDONLY, None) != 0:
        _fail("cannot make read_only " + target + ": " + os.strerror(ctypes.get_errno()))

# shared_write roots (OS temp dirs) are generic infrastructure, not a
# deliberate grant: expose them for write, then re-apply the deny mounts so
# any deny root nested inside one (e.g. a memory palace relocated under
# /tmp) stays hidden. Mount order determines precedence, so the re-hide
# below — applied after shared_write is exposed — is what makes deny win
# for exactly that nested case, matching the seatbelt profile's
# require-not-deny treatment of shared_write.
for target in shared_write:
    if not os.path.isdir(target):
        try:
            os.makedirs(target, mode=0o700, exist_ok=True)
        except OSError as exc:
            _fail("cannot prepare shared_write mount point " + target + ": " + str(exc))
    enc = target.encode()
    if libc.mount(enc, enc, None, MS_BIND | MS_REC, None) != 0:
        _fail("cannot expose shared_write " + target + ": " + os.strerror(ctypes.get_errno()))

for target in deny:
    nested_under_shared = any(
        target == shared or target.startswith(shared.rstrip("/") + "/") for shared in shared_write
    )
    if not nested_under_shared:
        continue
    flags = MS_RDONLY | MS_NOSUID | MS_NODEV
    if libc.mount(b"tmpfs", target.encode(), b"tmpfs", flags, b"size=0,mode=0500") != 0:
        _fail("cannot re-deny " + target + ": " + os.strerror(ctypes.get_errno()))

# always_deny must win unconditionally, including nesting inside
# read_write/read_only/shared_write above — mount order determines
# precedence, so mounting these last (unlike the nested-only re-deny for
# `deny` above) is what makes them stick regardless of where they sit.
for target in always_deny:
    if not os.path.isdir(target):
        continue
    flags = MS_RDONLY | MS_NOSUID | MS_NODEV
    if libc.mount(b"tmpfs", target.encode(), b"tmpfs", flags, b"size=0,mode=0500") != 0:
        _fail("cannot deny " + target + ": " + os.strerror(ctypes.get_errno()))

try:
    if os.sep in argv[0]:
        os.execv(argv[0], argv)
    else:
        os.execvp(argv[0], argv)
except OSError as exc:
    _fail("cannot exec " + argv[0] + ": " + str(exc))
"""


def _namespace_jail_argv(executable: str, args: Sequence[str], roots: JailRoots) -> list[str]:
    spec = {
        "deny": _resolved_strs(roots.deny),
        "read_write": _resolved_strs(roots.read_write),
        "read_only": _resolved_strs(roots.read_only),
        "shared_write": _resolved_strs(roots.shared_write),
        "always_deny": _resolved_strs(roots.always_deny),
    }
    return [
        sys.executable,
        "-S",
        "-E",
        "-c",
        _LINUX_JAIL_BOOTSTRAP,
        json.dumps(spec),
        executable,
        *args,
    ]


def jailed_argv(
    executable: str,
    args: Sequence[str],
    roots: JailRoots,
    *,
    support: IsolationSupport,
) -> tuple[str, list[str]]:
    """Rewrite an argv to run under a deny-by-default filesystem jail.

    Raises ``ValueError`` when ``support`` is unavailable or any root
    contains seatbelt metacharacters — callers should treat that the same as
    a failed isolation attempt (see each caller's own fail-open/fail-closed
    policy; this function itself has no opinion on that).
    """
    if not support.available:
        raise ValueError(f"isolation is unavailable: {support.detail}")
    if roots.is_empty():
        return executable, list(args)
    if support.mechanism == "namespace":
        argv = _namespace_jail_argv(executable, args, roots)
    else:
        argv = _sandbox_exec_jail_argv(executable, args, roots)
    return argv[0], argv[1:]
