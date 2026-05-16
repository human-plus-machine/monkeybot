"""Scaffold ``monkeybot_config/`` with defaults and placeholders for a new workspace."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# (template filename under templates/, output name under monkeybot_config/)
_BUNDLE: tuple[tuple[str, str], ...] = (
    ("monkeybot.example.yaml", "monkeybot.example.yaml"),
    ("mcp.json", "mcp.json"),
    ("command_allowlist.yaml", "command_allowlist.yaml"),
    ("AGENT.md", "AGENT.md"),
)


def _install_file(cfg_dir: Path, template_name: str, dest_name: str, *, force: bool) -> str:
    """Return status label: created | skipped | overwritten."""
    src = _TEMPLATES / template_name
    if not src.is_file():
        raise FileNotFoundError(f"missing packaged template: {src}")
    dest = cfg_dir / dest_name
    if dest.exists() and not force:
        return "skipped"
    existed = dest.exists()
    shutil.copyfile(src, dest)
    return "overwritten" if existed else "created"


def run_init(*, dest: Path, force: bool) -> int:
    cfg_dir = dest / "monkeybot_config"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    report: list[str] = []
    for tmpl, out_name in _BUNDLE:
        report.append(f"  monkeybot_config/{out_name}: {_install_file(cfg_dir, tmpl, out_name, force=force)}")

    active = cfg_dir / "monkeybot.yaml"
    example = _TEMPLATES / "monkeybot.example.yaml"
    if not active.exists() or force:
        existed = active.exists()
        shutil.copyfile(example, active)
        report.append(
            f"  monkeybot_config/monkeybot.yaml: "
            f"{'overwritten' if existed else 'created'} (from monkeybot.example.yaml)"
        )
    else:
        report.append("  monkeybot_config/monkeybot.yaml: skipped")

    memory = dest / "data" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    idx = memory / "INDEX.md"
    if not idx.exists() or force:
        existed = idx.exists()
        idx.write_text(
            "# Memory index\n\nAdd sections here or let memory tools populate this file.\n",
            encoding="utf-8",
        )
        report.append(f"  data/memory/INDEX.md: {'overwritten' if existed else 'created'}")
    else:
        report.append("  data/memory/INDEX.md: skipped")

    skills = dest / ".agents" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    report.append("  .agents/skills/: ensured (empty ok)")

    print(f"MonkeyBot scaffold under {dest.resolve()}:")
    print("\n".join(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Create monkeybot_config/ with defaults (monkeybot.yaml, MCP map, allowlist, AGENT.md).",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path.cwd(),
        help="Root directory to create monkeybot_config/ under (default: current working directory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scaffold files",
    )
    args = parser.parse_args(argv)
    root = args.dest.expanduser().resolve()
    if not root.is_dir():
        print(f"error: --dest is not a directory: {root}", file=sys.stderr)
        return 2
    return run_init(dest=root, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
