"""Hard security boundary for the ``computer_*`` tools.

The ``computer_*`` tools are not ``run_command`` — they never pass through the
binary/path allowlist in ``core/tools/terminal.py`` or ``CommandTierInspector``.
``permissions.yaml`` (see ``core/tools/permission.py``) is a *soft*, user-editable
ask/allow layer; it decides whether to *prompt*, not whether an action is *safe*.
It is also fail-open (a missing or broken file disables prompting entirely), so
it must never be the only thing standing between the model and the filesystem.

Everything that must never be allowed regardless of approval lives here instead,
and cannot be bypassed by an "Always allow" rule or a broken config file:

- escaping the user's home directory (or an explicitly allowed extra root)
- touching credentials, keychains, browser profiles, or the app's own config
  (the last one specifically prevents the agent from editing its way to more
  permissions than it was granted)
- executing code via ``open`` — ``open foo.command`` / ``open -a Terminal x.sh``
  runs arbitrary code and would otherwise be a complete bypass of the
  ``run_command`` allowlist the rest of the harness depends on
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ErrorKind = Literal["validation", "policy", "runtime"]

MAX_CLIPBOARD_CHARS = 20_000
MAX_LIST_ENTRIES = 500
MAX_FIND_RESULTS = 200
DEFAULT_TIMEOUT_SEC = 10.0

_ENV_APP_HOME = "MONKEYBOT_APP_HOME"

# Roots the tools may operate under. Anything outside all of these is denied
# regardless of any other rule.
_EXTRA_ALLOWED_ROOTS: tuple[str, ...] = ("/Volumes",)

# Relative to the real user's home directory (``Path.home()``), resolved lazily
# so this module has no import-time filesystem dependency.
_DENIED_HOME_SUBDIRS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config/gcloud",
    ".monkeybot",
    "Library/Keychains",
    "Library/Cookies",
    "Library/Messages",
    "Library/Mail",
    "Library/Application Support/MobileSync",
    "Library/Application Support/com.apple.TCC",
    "Library/Group Containers",
    "Library/Containers/com.apple.Safari",
    "Library/Safari",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Monkeybot",
)

# Single-segment entries from _DENIED_HOME_SUBDIRS above (".ssh", ".aws", ...)
# are unambiguous credential-store directory names on their own, so they're
# denied wherever they appear in the tree — not only directly under home.
# ``~/Downloads/some-repo/.ssh`` is just as much a key store as ``~/.ssh``.
# Multi-segment entries (e.g. "Library/Keychains") stay location-specific:
# a bare folder named "Keychains" somewhere unrelated isn't the same thing.
_DENIED_BASENAMES_ANYWHERE: frozenset[str] = frozenset(
    d for d in _DENIED_HOME_SUBDIRS if "/" not in d
)

# fnmatch'd (case-insensitive) against the final path component only.
_DENIED_FILENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*.p12",
    "*.keychain*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "*.mobileprovision",
)

# Suffixes that make ``open`` execute code rather than merely display a file.
# ``open`` on any of these is equivalent to running it — refusing this closes
# the single biggest hole in a naive implementation of this tool family.
_EXEC_SUFFIXES: frozenset[str] = frozenset(
    {
        ".command",
        ".sh",
        ".bash",
        ".zsh",
        ".app",
        ".scpt",
        ".scptd",
        ".applescript",
        ".workflow",
        ".term",
        ".pkg",
        ".mpkg",
        ".dmg",
        ".jar",
        ".action",
        ".prefpane",
        ".osax",
        ".py",
        ".rb",
        ".pl",
        ".exe",
    }
)

# Apps that are themselves code-execution surfaces — never launched by name,
# even though they're ordinary installed applications.
_DENIED_APP_NAMES: frozenset[str] = frozenset(
    {
        "terminal",
        "iterm",
        "iterm2",
        "script editor",
        "automator",
        "console",
        "xcode",
        "activity monitor",
    }
)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})

_OPEN_BIN = "/usr/bin/open"
_PBCOPY_BIN = "/usr/bin/pbcopy"
_PBPASTE_BIN = "/usr/bin/pbpaste"


class ComputerToolError(Exception):
    """Raised by ``computer_*`` tool bodies; carries the harness error envelope."""

    def __init__(self, kind: ErrorKind, message: str, hint: str) -> None:
        self.kind = kind
        self.message = message
        self.hint = hint
        super().__init__(message)


def require_macos() -> None:
    if sys.platform != "darwin":
        raise ComputerToolError(
            "policy",
            "Computer-control tools are only supported on macOS.",
            "This gateway is not running on macOS; computer_* tools should not be enabled here.",
        )


def _expand_tilde(raw: str) -> Path:
    """Expand a leading ``~`` against :func:`Path.home` — never ``Path.expanduser()``.

    ``expanduser()`` resolves against ``os.path.expanduser`` (the ``HOME`` env var
    / pwd database), a *different* source of truth than ``Path.home()``, which
    every other check in this module (denylist roots, allowed roots) is built on.
    Using two different notions of "home" for the same path would let a
    discrepancy between them become a policy bypass; expanding by hand against
    the same ``Path.home()`` call closes that gap. Only ``~`` and ``~/...`` are
    expanded — ``~otheruser/...`` is left alone (and will simply fail the
    allowed-roots check, since it isn't this user's home).
    """
    if raw == "~":
        return Path.home()
    if raw.startswith("~/"):
        return Path.home() / raw[2:]
    return Path(raw)


def _app_home() -> Path | None:
    raw = os.environ.get(_ENV_APP_HOME)
    if not raw or not raw.strip():
        return None
    try:
        return _expand_tilde(raw.strip()).resolve()
    except OSError:
        return None


def _denied_dirs() -> tuple[Path, ...]:
    home = Path.home().resolve()
    dirs = [(home / rel).resolve() for rel in _DENIED_HOME_SUBDIRS]
    app_home = _app_home()
    if app_home is not None:
        dirs.append(app_home)
    # The agent's own config directory (permissions.yaml, approvals.json,
    # command_allowlist.yaml) must never be writable/readable by these tools —
    # otherwise "Always allow" is one `computer_move` away from "always allow
    # everything", regardless of what the user actually approved.
    config_dir = os.environ.get("MONKEYBOT_CONFIG")
    if config_dir:
        with contextlib.suppress(OSError):
            dirs.append(_expand_tilde(config_dir).resolve().parent)
    return tuple(dirs)


def _allowed_roots() -> tuple[Path, ...]:
    roots = [Path.home().resolve()]
    for raw in _EXTRA_ALLOWED_ROOTS:
        p = Path(raw)
        if p.exists():
            roots.append(p.resolve())
    return tuple(roots)


def is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _check_filename_denied(path: Path) -> None:
    name = path.name
    for pattern in _DENIED_FILENAME_PATTERNS:
        if fnmatch.fnmatch(name.lower(), pattern.lower()):
            raise ComputerToolError(
                "policy",
                f"Refusing to touch a credential-shaped file: {name!r}",
                "Files matching common secret/credential naming patterns are always denied.",
            )


def _check_denied_dirs(path: Path) -> None:
    for denied_root in _denied_dirs():
        if path == denied_root or is_within(path, denied_root):
            raise ComputerToolError(
                "policy",
                f"Path is inside a protected directory: {path}",
                "This directory holds credentials, browser data, or app-internal state and is always denied.",
            )
    for part in path.parts:
        if part in _DENIED_BASENAMES_ANYWHERE:
            raise ComputerToolError(
                "policy",
                f"Path passes through a protected directory name: {part!r}",
                "This directory name is always treated as a credential store, wherever it appears.",
            )


def resolve_user_path(raw: str, *, must_exist: bool = False) -> Path:
    """Expand, resolve, and hard-validate a user-supplied path.

    Rejects anything outside the home directory (or an allowed extra root like
    ``/Volumes``) after following symlinks, and anything under the denylisted
    subdirectories or matching a denylisted filename pattern.
    """
    if not raw or not raw.strip():
        raise ComputerToolError("validation", "path is required", "Pass a non-empty path.")

    resolved = _expand_tilde(raw.strip()).resolve()

    if not any(is_within(resolved, root) or resolved == root for root in _allowed_roots()):
        raise ComputerToolError(
            "policy",
            f"Path is outside allowed locations: {resolved}",
            "computer_* tools only operate within the user's home directory (or a mounted volume).",
        )

    _check_denied_dirs(resolved)
    _check_filename_denied(resolved)

    if must_exist and not resolved.exists():
        raise ComputerToolError(
            "validation",
            f"Path does not exist: {resolved}",
            "Check the path and try again.",
        )

    return resolved


def is_path_denied(path: Path) -> bool:
    """Non-raising check used to filter listing/search results.

    ``list_dir``/``find`` must filter denied entries out of their results, not
    merely refuse a denied *root* — otherwise listing a parent directory leaks
    the existence and names of things inside a denied subdirectory.
    """
    try:
        if not any(is_within(path, root) or path == root for root in _allowed_roots()):
            return True
        for denied_root in _denied_dirs():
            if path == denied_root or is_within(path, denied_root):
                return True
        for part in path.parts:
            if part in _DENIED_BASENAMES_ANYWHERE:
                return True
        for pattern in _DENIED_FILENAME_PATTERNS:
            if fnmatch.fnmatch(path.name.lower(), pattern.lower()):
                return True
    except (OSError, ValueError):
        return True
    return False


def check_not_exec_surface(path: Path) -> None:
    """Refuse a path that ``open`` would execute rather than merely display."""
    if path.suffix.lower() in _EXEC_SUFFIXES:
        raise ComputerToolError(
            "policy",
            f"Refusing to open an executable file type: {path.suffix}",
            "computer_open only displays/launches documents, never runs scripts, apps, or installers.",
        )
    try:
        if path.is_file() and os.access(path, os.X_OK):
            raise ComputerToolError(
                "policy",
                f"Refusing to open an executable file: {path.name}",
                "The file has the executable bit set.",
            )
    except OSError:
        pass


def check_app_name_allowed(app_name: str) -> None:
    normalized = app_name.strip().lower().removesuffix(".app")
    if normalized in _DENIED_APP_NAMES:
        raise ComputerToolError(
            "policy",
            f"Refusing to launch {app_name!r}: this app is itself a code-execution surface.",
            "Terminal, script editors, and similar apps cannot be launched by computer_* tools.",
        )


def validate_url(raw: str) -> str:
    if not raw or not raw.strip():
        raise ComputerToolError("validation", "url is required", "Pass a non-empty URL.")
    url = raw.strip()
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ComputerToolError(
            "policy",
            f"URL scheme not allowed: {scheme!r}",
            f"Only {', '.join(sorted(_ALLOWED_URL_SCHEMES))} URLs may be opened.",
        )
    return url


def resolve_app_bundle(app_name: str) -> Path:
    if not app_name or not app_name.strip():
        raise ComputerToolError("validation", "app_name is required", "Pass an application name.")
    check_app_name_allowed(app_name)
    name = app_name.strip()
    if "/" in name or ".." in name:
        raise ComputerToolError(
            "validation", "app_name must be a plain application name", "No paths or '..'."
        )
    if not name.endswith(".app"):
        name = f"{name}.app"
    for root in (Path("/Applications"), Path.home() / "Applications"):
        if not root.exists():
            continue
        candidate = (root / name).resolve()
        if (
            candidate.exists()
            and candidate.suffix == ".app"
            and is_within(candidate, root.resolve())
        ):
            return candidate
    raise ComputerToolError(
        "validation",
        f"No installed application found matching {app_name!r}",
        "Check the application name and that it is installed in /Applications.",
    )


@dataclass(frozen=True)
class RunResult:
    stdout: str
    stderr: str
    returncode: int


def run_argv(
    argv: list[str], *, input_text: str | None = None, timeout: float = DEFAULT_TIMEOUT_SEC
) -> RunResult:
    """Execute an absolute-path binary with a fixed argv. Never a shell string."""
    if not argv or not argv[0].startswith("/"):
        raise ComputerToolError(
            "runtime",
            "internal error: computer tool built a non-absolute argv",
            "This is a bug in the tool implementation, not user input.",
        )
    try:
        proc = subprocess.run(  # noqa: S603 - argv is a fixed absolute-path list, shell=False
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ComputerToolError(
            "runtime", f"Command timed out after {timeout}s", "Try again or check system state."
        ) from e
    except OSError as e:
        raise ComputerToolError(
            "runtime", f"Failed to execute: {e}", "Check the binary is present."
        ) from e
    return RunResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


def open_path(path: Path, *, reveal: bool = False) -> None:
    check_not_exec_surface(path)
    argv = [_OPEN_BIN, "-R", str(path)] if reveal else [_OPEN_BIN, str(path)]
    result = run_argv(argv)
    if result.returncode != 0:
        raise ComputerToolError(
            "runtime",
            f"open failed: {result.stderr.strip() or result.returncode}",
            "Check the path exists.",
        )


def open_path_with_app(path: Path, app_name: str) -> None:
    check_not_exec_surface(path)
    bundle = resolve_app_bundle(app_name)
    result = run_argv([_OPEN_BIN, "-a", str(bundle), str(path)])
    if result.returncode != 0:
        raise ComputerToolError(
            "runtime",
            f"open failed: {result.stderr.strip() or result.returncode}",
            "Check the path and app.",
        )


def open_url(url: str) -> None:
    result = run_argv([_OPEN_BIN, url])
    if result.returncode != 0:
        raise ComputerToolError(
            "runtime",
            f"open failed: {result.stderr.strip() or result.returncode}",
            "Check the URL.",
        )


def open_app(bundle: Path) -> None:
    result = run_argv([_OPEN_BIN, "-a", str(bundle)])
    if result.returncode != 0:
        raise ComputerToolError(
            "runtime",
            f"open failed: {result.stderr.strip() or result.returncode}",
            "Check the app is installed.",
        )


def read_clipboard() -> str:
    result = run_argv([_PBPASTE_BIN])
    if result.returncode != 0:
        raise ComputerToolError(
            "runtime", f"pbpaste failed: {result.stderr.strip()}", "Check clipboard access."
        )
    text = result.stdout
    if len(text) > MAX_CLIPBOARD_CHARS:
        text = text[:MAX_CLIPBOARD_CHARS]
    return text


def write_clipboard(text: str) -> None:
    result = run_argv([_PBCOPY_BIN], input_text=text)
    if result.returncode != 0:
        raise ComputerToolError(
            "runtime", f"pbcopy failed: {result.stderr.strip()}", "Check clipboard access."
        )


# Directories directly under home that must never be trashed wholesale.
_PROTECTED_TOP_LEVEL_DIRS: frozenset[str] = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "library",
        "applications",
        "movies",
        "music",
        "pictures",
        "public",
    }
)
_TRASH_MAX_ITEMS = 10_000


def _count_items(path: Path, cap: int) -> int:
    if path.is_file():
        return 1
    count = 0
    for _root, dirs, files in os.walk(path):
        count += len(dirs) + len(files)
        if count > cap:
            return count
    return count


def check_trashable(path: Path) -> None:
    home = Path.home().resolve()
    if path == home:
        raise ComputerToolError(
            "policy", "Refusing to trash the home directory.", "Pick a specific item."
        )
    if path.parent == home and path.name.lower() in _PROTECTED_TOP_LEVEL_DIRS:
        raise ComputerToolError(
            "policy",
            f"Refusing to trash a top-level folder: {path.name}",
            "Trash a specific file or subfolder instead.",
        )
    if path.parent == path or str(path) in {"/", str(Path("/Volumes"))}:
        raise ComputerToolError(
            "policy", "Refusing to trash a volume root.", "Pick a specific item."
        )
    if _count_items(path, _TRASH_MAX_ITEMS) > _TRASH_MAX_ITEMS:
        raise ComputerToolError(
            "policy",
            f"Refusing to trash more than {_TRASH_MAX_ITEMS} items at once.",
            "Trash a narrower path.",
        )


def trash_path(path: Path) -> Path:
    """Move ``path`` into ``~/.Trash``, never a hard delete.

    Prefers ``send2trash`` when available (proper Finder trash semantics,
    "Put Back" support); falls back to a plain move into ``~/.Trash`` — files
    on other mounted volumes are moved cross-device rather than left in place.
    """
    check_trashable(path)
    try:
        from send2trash import send2trash

        send2trash(str(path))
        return path
    except ImportError:
        pass

    home_trash = (Path.home() / ".Trash").resolve()
    home_trash.mkdir(exist_ok=True)
    dest = home_trash / path.name
    if dest.exists():
        dest = home_trash / f"{path.stem}-{int(time.time())}{path.suffix}"
    if _cross_device(path, dest):
        _move_cross_device(path, dest)
    else:
        path.rename(dest)
    return dest


def _cross_device(src: Path, dest: Path) -> bool:
    try:
        return src.stat().st_dev != dest.parent.stat().st_dev
    except OSError:
        return False


def _move_cross_device(src: Path, dest: Path) -> None:
    import shutil

    shutil.move(str(src), str(dest))


def _str_arg(args: dict[str, object], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) and value.strip() else None


def precheck_policy(tool: str, args: dict[str, object]) -> ComputerToolError | None:
    """Run the hard policy checks a call *would* fail, without executing anything.

    Used by ``computer/permissions.py`` so a call to e.g. ``computer_open`` on
    ``~/.ssh/id_rsa`` is denied *before* the user ever sees an approval card —
    matching the design intent that these hard limits can't be bypassed by
    approving anything, and shouldn't even look askable. Only ``ComputerToolError``
    with ``kind == "policy"`` is returned; validation problems (missing args, a
    path that doesn't exist yet) are not policy violations and are left for the
    tool itself to report after approval, same as any other bad tool call.

    Deliberately duplicates a fraction of each tool's own validation rather than
    executing anything — this function must never touch the filesystem beyond
    ``resolve()``/``exists()``/``stat()`` (no ``open``, no subprocess), since it
    runs for *every* call of a tool, approved or not.
    """

    def _policy_error_from_path(
        raw: str, *, exec_surface: bool, trashable: bool
    ) -> ComputerToolError | None:
        try:
            resolved = resolve_user_path(raw)
            if exec_surface:
                check_not_exec_surface(resolved)
            if trashable:
                check_trashable(resolved)
        except ComputerToolError as e:
            return e if e.kind == "policy" else None
        return None

    def _policy_error_from_app(app_name: str) -> ComputerToolError | None:
        try:
            check_app_name_allowed(app_name)
        except ComputerToolError as e:
            return e if e.kind == "policy" else None
        return None

    if tool == "computer_open":
        path = _str_arg(args, "path")
        app = _str_arg(args, "app")
        if path is not None:
            err = _policy_error_from_path(path, exec_surface=True, trashable=False)
            if err is not None:
                return err
        if app is not None:
            err = _policy_error_from_app(app)
            if err is not None:
                return err
        return None
    if tool == "computer_open_url":
        url = _str_arg(args, "url")
        if url is None:
            return None
        try:
            validate_url(url)
        except ComputerToolError as e:
            return e if e.kind == "policy" else None
        return None
    if tool == "computer_open_app":
        app = _str_arg(args, "app")
        if app is None:
            return None
        return _policy_error_from_app(app)
    if tool in ("computer_list_dir", "computer_find"):
        path = _str_arg(args, "path")
        if path is None:
            return None
        return _policy_error_from_path(path, exec_surface=False, trashable=False)
    if tool == "computer_move":
        for key in ("path", "destination"):
            value = _str_arg(args, key)
            if value is None:
                continue
            err = _policy_error_from_path(value, exec_surface=False, trashable=False)
            if err is not None:
                return err
        return None
    if tool == "computer_trash":
        path = _str_arg(args, "path")
        if path is None:
            return None
        return _policy_error_from_path(path, exec_surface=False, trashable=True)
    return None
