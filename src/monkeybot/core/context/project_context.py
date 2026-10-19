"""Project instructions (``AGENTS.md``) and a deterministic repo overview for coding mode.

Both land in the *stable* system-prompt prefix, so the output must be byte-identical
for an unchanged workspace: sorted, no timestamps, no git state (that arrives per turn
via ``core.coding.git_context_hook``), and no listing of files an agent routinely
creates mid-task (only fixed well-known root files and directories), so ordinary
edits do not rewrite the prefix and open a new context epoch.
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


def load_project_instructions(
    workspace_root: Path,
    names: Sequence[str] = DEFAULT_PROJECT_INSTRUCTIONS_FILES,
) -> str:
    """Contents of the first instruction file found under ``workspace_root``, or ``""``.

    ``names`` is tried in order. A file that resolves outside the workspace (a symlink
    escape) is skipped. Text beyond ``MAX_INSTRUCTIONS_CHARS`` is cut with a note that
    says where to read the rest.
    """
    root = Path(workspace_root).resolve()
    for name in names:
        try:
            path = (root / name).resolve()
            path.relative_to(root)
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                raw = handle.read(_MAX_INSTRUCTIONS_BYTES + 1)
        except (OSError, ValueError):
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if len(text) > MAX_INSTRUCTIONS_CHARS:
            text = (
                text[:MAX_INSTRUCTIONS_CHARS].rstrip()
                + f"\n\n… [truncated at {MAX_INSTRUCTIONS_CHARS} chars; read_file {name} for the rest]"
            )
        return f"Source: {name}\n\n{_neutralize_volatile_headings(text)}"
    return ""


def _read_json(path: Path) -> dict[str, object]:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_make_test_target(path: Path) -> bool:
    try:
        return (
            re.search(r"^test\s*:", path.read_text(encoding="utf-8", errors="replace"), re.M)
            is not None
        )
    except OSError:
        return False


def _toolchains(root: Path) -> list[str]:
    """One ``language (markers) — tests: `cmd``` line per detected toolchain."""
    lines: list[str] = []

    def present(*names: str) -> list[str]:
        return [n for n in names if (root / n).is_file()]

    python = present("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "uv.lock")
    if python:
        test_cmd = "uv run pytest -q" if "uv.lock" in python else "python -m pytest -q"
        lines.append(f"python ({', '.join(python)}) — tests: `{test_cmd}`")

    node = present(
        "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "tsconfig.json"
    )
    if "package.json" in node:
        runner = "pnpm" if "pnpm-lock.yaml" in node else "yarn" if "yarn.lock" in node else "npm"
        scripts = _read_json(root / "package.json").get("scripts")
        has_test = isinstance(scripts, dict) and "test" in scripts
        suffix = f" — tests: `{runner} test`" if has_test else ""
        lines.append(f"node ({', '.join(node)}){suffix}")

    if (root / "Cargo.toml").is_file():
        lines.append("rust (Cargo.toml) — tests: `cargo test`")
    if (root / "go.mod").is_file():
        lines.append("go (go.mod) — tests: `go test ./...`")
    if (root / "Makefile").is_file():
        suffix = " — tests: `make test`" if _has_make_test_target(root / "Makefile") else ""
        lines.append(f"make (Makefile){suffix}")
    return lines


def _child_dirs(path: Path) -> list[str]:
    try:
        names = [
            p.name
            for p in path.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name not in _SKIP_DIRS
        ]
    except OSError:
        return []
    return sorted(names, key=str.casefold)


def repo_overview(workspace_root: Path) -> str:
    """Toolchain guess plus a short, sorted layout of ``workspace_root``; ``""`` if unremarkable."""
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        return ""
    lines = _toolchains(root)
    entries: list[str] = []
    for name in _child_dirs(root):
        entries.append(f"{name}/")
        if name in _KEY_DIRS:
            for sub in _child_dirs(root / name)[:_MAX_SUBDIRS_PER_KEY_DIR]:
                entries.append(f"  {sub}/")
    entries.extend(name for name in _ROOT_FILES if (root / name).is_file())
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
    return "\n".join(out)
