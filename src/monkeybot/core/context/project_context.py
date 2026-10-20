"""Project instructions (``AGENTS.md``) and a deterministic repo overview for coding mode.

Both land in the *stable* system-prompt prefix, so the output must be byte-identical
for an unchanged workspace: sorted, no timestamps, no git state (that arrives per turn
via ``core.coding.git_context_hook``), and no listing of files an agent routinely
creates mid-task (only fixed well-known root files and directories), so ordinary
edits do not rewrite the prefix and open a new context epoch.

Filesystem surprises degrade to a skip, not a failed turn: symlink loops, paths that
resolve outside the workspace, and directory names that would break a line are ignored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from monkeybot.core.config.settings import DEFAULT_PROJECT_INSTRUCTIONS_FILES
from monkeybot.core.prompts.headings import VOLATILE_SECTION_HEADINGS, heading_marker

MAX_INSTRUCTIONS_CHARS = 12_000
_MAX_INSTRUCTIONS_BYTES = MAX_INSTRUCTIONS_CHARS * 4
_MAX_OVERVIEW_ENTRIES = 80
_MAX_SUBDIRS_PER_KEY_DIR = 12
_MAX_JSON_BYTES = 256_000

# Directories never shown (same spirit as grep/glob noise dirs).
_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "venv",
        "dist",
        "build",
        "target",
        "vendor",
        "coverage",
        "htmlcov",
        "site-packages",
    }
)
# Directories whose child *directories* (packages/modules) are worth one more level.
_KEY_DIRS = frozenset(
    {"src", "lib", "app", "apps", "packages", "pkg", "cmd", "internal", "crates", "services"}
)
# Root files shown by name; anything else at the root is omitted on purpose.
_ROOT_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "tsconfig.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
)

_VOLATILE_TITLES = tuple(
    heading_marker(h).strip().removeprefix("## ") for h in VOLATILE_SECTION_HEADINGS
)
_VOLATILE_TITLE_LINE = re.compile(
    r"^## (" + "|".join(re.escape(t) for t in _VOLATILE_TITLES) + r")[ \t]*$",
    re.MULTILINE,
)


def _neutralize_volatile_headings(text: str) -> str:
    """Demote ``## Skills`` etc. so the cache splitter cannot mistake them for the volatile tail."""
    return _VOLATILE_TITLE_LINE.sub(r"### \1", text)


def _contained(root: Path, candidate: Path) -> Path | None:
    """Resolved ``candidate`` when it stays inside ``root``.

    ``Path.resolve`` raises ``RuntimeError`` on a symlink loop (not ``OSError``), which
    must not fail the turn.
    """
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved


def _line_safe_name(name: str) -> bool:
    """False when ``name`` could inject a new line or a NUL into the cached prefix."""
    return bool(name) and not any(char in name for char in "\n\r\x00")


def _instruction_file(root: Path, name: str) -> Path | None:
    """Instruction file under ``root``, or ``None`` when ``name`` escapes or is unusable.

    Absolute paths and ``..`` are not special-cased: ``_contained`` keeps whatever
    still resolves inside the workspace, and skips what does not.
    """
    if not _line_safe_name(name):
        return None
    resolved = _contained(root, root / name)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


def _regular_file(root: Path, name: str) -> Path | None:
    resolved = _contained(root, root / name)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


def _read_capped(path: Path, limit: int) -> tuple[bytes, bool] | None:
    """Read at most ``limit`` bytes. The bool is true when the file continues past them."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    return raw[:limit], len(raw) > limit


def load_project_instructions(
    workspace_root: Path,
    names: Sequence[str] = DEFAULT_PROJECT_INSTRUCTIONS_FILES,
) -> str:
    """Contents of the first instruction file found under ``workspace_root``, or ``""``.

    ``names`` is tried in order. A file that resolves outside the workspace (a symlink
    escape or loop) is skipped. Text beyond ``MAX_INSTRUCTIONS_CHARS`` — or a file whose
    byte window is larger than we will read — is cut with a note that says where to read
    the rest. A blank window on a longer file still counts as that file, so a leading
    pad cannot hide it and fall through to the next name.
    """
    try:
        root = Path(workspace_root).resolve()
    except (OSError, RuntimeError):
        return ""
    if not root.is_dir():
        return ""
    for name in names:
        path = _instruction_file(root, name)
        if path is None:
            continue
        loaded = _read_capped(path, _MAX_INSTRUCTIONS_BYTES)
        if loaded is None:
            continue
        raw, byte_truncated = loaded
        text = raw.decode("utf-8", errors="replace").strip()
        char_truncated = len(text) > MAX_INSTRUCTIONS_CHARS
        if char_truncated:
            text = text[:MAX_INSTRUCTIONS_CHARS].rstrip()
        if not text and not byte_truncated:
            continue
        if byte_truncated or char_truncated:
            text = (
                text.rstrip()
                + f"\n\n… [truncated at {MAX_INSTRUCTIONS_CHARS} chars; read_file {name} for the rest]"
            )
        return f"Source: {name}\n\n{_neutralize_volatile_headings(text)}"
    return ""


def _read_json(path: Path) -> dict[str, object]:
    loaded = _read_capped(path, _MAX_JSON_BYTES)
    if loaded is None:
        return {}
    raw, truncated = loaded
    if truncated:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_make_test_target(path: Path) -> bool:
    loaded = _read_capped(path, _MAX_JSON_BYTES)
    if loaded is None:
        return False
    raw, _truncated = loaded
    text = raw.decode("utf-8", errors="replace")
    return re.search(r"^test\s*:", text, re.M) is not None


def _toolchains(root: Path) -> list[str]:
    """One ``language (markers) — tests: `cmd``` line per detected toolchain."""
    lines: list[str] = []

    def present(*names: str) -> list[str]:
        return [n for n in names if _regular_file(root, n) is not None]

    python = present("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "uv.lock")
    if python:
        test_cmd = "uv run pytest -q" if "uv.lock" in python else "python -m pytest -q"
        lines.append(f"python ({', '.join(python)}) — tests: `{test_cmd}`")

    node = present(
        "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "tsconfig.json"
    )
    if "package.json" in node:
        runner = "pnpm" if "pnpm-lock.yaml" in node else "yarn" if "yarn.lock" in node else "npm"
        package_json = _regular_file(root, "package.json")
        scripts = _read_json(package_json).get("scripts") if package_json is not None else None
        has_test = isinstance(scripts, dict) and "test" in scripts
        suffix = f" — tests: `{runner} test`" if has_test else ""
        lines.append(f"node ({', '.join(node)}){suffix}")

    if _regular_file(root, "Cargo.toml") is not None:
        lines.append("rust (Cargo.toml) — tests: `cargo test`")
    if _regular_file(root, "go.mod") is not None:
        lines.append("go (go.mod) — tests: `go test ./...`")
    makefile = _regular_file(root, "Makefile")
    if makefile is not None:
        suffix = " — tests: `make test`" if _has_make_test_target(makefile) else ""
        lines.append(f"make (Makefile){suffix}")
    return lines


def _child_dirs(root: Path, path: Path) -> list[str]:
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    names: list[str] = []
    for child in children:
        name = child.name
        if name.startswith(".") or name in _SKIP_DIRS or not _line_safe_name(name):
            continue
        resolved = _contained(root, child)
        if resolved is None or not resolved.is_dir():
            continue
        names.append(name)
    return sorted(names, key=str.casefold)


def repo_overview(workspace_root: Path) -> str:
    """Toolchain guess plus a short, sorted layout of ``workspace_root``; ``""`` if unremarkable."""
    try:
        root = Path(workspace_root).resolve()
    except (OSError, RuntimeError):
        return ""
    if not root.is_dir():
        return ""
    lines = _toolchains(root)
    entries: list[str] = []
    for name in _child_dirs(root, root):
        entries.append(f"{name}/")
        if name not in _KEY_DIRS:
            continue
        child = _contained(root, root / name)
        if child is None:
            continue
        for sub in _child_dirs(root, child)[:_MAX_SUBDIRS_PER_KEY_DIR]:
            entries.append(f"  {sub}/")
    entries.extend(name for name in _ROOT_FILES if _regular_file(root, name) is not None)
    shown = entries[:_MAX_OVERVIEW_ENTRIES]
    if not lines and not shown:
        return ""
    out: list[str] = []
    if lines:
        out.append("Toolchain: " + "; ".join(lines))
    if shown:
        out.append("Layout (workspace root):")
        out.extend(f"  {entry}" for entry in shown)
        if len(entries) > len(shown):
            out.append(f"  … (+{len(entries) - len(shown)} more)")
    return _neutralize_volatile_headings("\n".join(out))
