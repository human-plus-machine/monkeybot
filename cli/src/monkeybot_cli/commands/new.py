"""monkeybot new — scaffold a bot workspace."""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
from importlib import resources
from pathlib import Path

import yaml

_TEMPLATES_PKG = "monkeybot_cli.templates"
_BUNDLE: tuple[tuple[str, str], ...] = (
    ("monkeybot.example.yaml", "monkeybot.example.yaml"),
    ("mcp.json", "mcp.json"),
    ("command_allowlist.yaml", "command_allowlist.yaml"),
    ("AGENT.md", "AGENT.md"),
)


def _template_path(name: str) -> Path:
    ref = resources.files(_TEMPLATES_PKG) / name
    with resources.as_file(ref) as p:
        return Path(p)


def _install_file(cfg_dir: Path, template_name: str, dest_name: str, *, force: bool) -> str:
    src = _template_path(template_name)
    dest = cfg_dir / dest_name
    if dest.exists() and not force:
        return "skipped"
    existed = dest.exists()
    shutil.copyfile(src, dest)
    return "overwritten" if existed else "created"


def _ensure_workspace(dest: Path, *, force: bool) -> list[str]:
    """Create workspace/ sandbox and workspace/skills -> ../skills symlink."""
    lines: list[str] = []
    workspace = dest / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    gitkeep = workspace / ".gitkeep"
    if not gitkeep.exists() or force:
        gitkeep.touch(exist_ok=True)
        lines.append(f"  workspace/.gitkeep: {'overwritten' if force and gitkeep.exists() else 'created'}")
    else:
        lines.append("  workspace/.gitkeep: skipped")

    skills_target = dest / "skills"
    skills_target.mkdir(parents=True, exist_ok=True)
    link = workspace / "skills"
    expected = (dest / "skills").resolve()

    if link.is_symlink():
        if link.resolve() == expected:
            lines.append("  workspace/skills: skipped (symlink ok)")
            return lines
        link.unlink()

    if link.exists() and not link.is_symlink():
        if not force:
            lines.append("  workspace/skills: skipped (path exists)")
            return lines
        if link.is_dir():
            shutil.rmtree(link)
        else:
            link.unlink()

    try:
        link.symlink_to("../skills", target_is_directory=True)
        lines.append("  workspace/skills: symlink -> ../skills")
    except OSError:
        readme = workspace / "SKILLS_README.txt"
        readme.write_text(
            "Could not create workspace/skills symlink on this platform.\n"
            "Run: bash scripts/setup-workspace.sh\n"
            "Or copy/symlink skills/ into workspace/skills manually.\n",
            encoding="utf-8",
        )
        lines.append("  workspace/skills: symlink failed (see workspace/SKILLS_README.txt)")

    return lines


def _install_setup_script(dest: Path, *, force: bool) -> str:
    scripts = dest / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    dest_script = scripts / "setup-workspace.sh"
    if dest_script.exists() and not force:
        return "skipped"
    existed = dest_script.exists()
    shutil.copyfile(_template_path("setup-workspace.sh"), dest_script)
    dest_script.chmod(dest_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return "overwritten" if existed else "created"


def _write_active_config(cfg_dir: Path, *, provider: str | None, model: str | None, force: bool) -> str:
    active = cfg_dir / "monkeybot.yaml"
    example = _template_path("monkeybot.example.yaml")
    if active.exists() and not force:
        if provider or model:
            doc = yaml.safe_load(active.read_text(encoding="utf-8")) or {}
            if not isinstance(doc, dict):
                doc = {}
            model_sec = doc.setdefault("model", {})
            if isinstance(model_sec, dict):
                if provider:
                    model_sec["provider"] = provider
                if model:
                    model_sec["name"] = model
            active.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
            return "updated (provider/model)"
        return "skipped"
    existed = active.exists()
    shutil.copyfile(example, active)
    if provider or model:
        doc = yaml.safe_load(active.read_text(encoding="utf-8")) or {}
        if isinstance(doc, dict):
            model_sec = doc.setdefault("model", {})
            if isinstance(model_sec, dict):
                if provider:
                    model_sec["provider"] = provider
                if model:
                    model_sec["name"] = model
            active.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return "overwritten" if existed else "created"


def run_new(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser().resolve()
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    if not dest.is_dir():
        print(f"error: --dest is not a directory: {dest}", file=sys.stderr)
        return 2

    provider = args.provider
    model = args.model
    if not args.yes:
        if provider is None:
            try:
                provider = input("Model provider [gemini]: ").strip() or "gemini"
            except EOFError:
                provider = "gemini"
        if model is None:
            try:
                model = input("Model name [gemini-3-flash]: ").strip() or "gemini-3-flash"
            except EOFError:
                model = "gemini-3-flash"

    cfg_dir = dest / "monkeybot_config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    for tmpl, out_name in _BUNDLE:
        report.append(f"  monkeybot_config/{out_name}: {_install_file(cfg_dir, tmpl, out_name, force=args.force)}")
    report.append(f"  monkeybot_config/monkeybot.yaml: {_write_active_config(cfg_dir, provider=provider, model=model, force=args.force)}")

    memory = dest / "data" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    idx = memory / "INDEX.md"
    if not idx.exists() or args.force:
        idx.write_text("# Memory index\n\nAdd sections here or let memory tools populate this file.\n", encoding="utf-8")
        report.append(f"  data/memory/INDEX.md: {'overwritten' if args.force and idx.exists() else 'created'}")
    else:
        report.append("  data/memory/INDEX.md: skipped")

    skills = dest / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    report.append("  skills/: ensured")

    report.extend(_ensure_workspace(dest, force=args.force))
    report.append(f"  scripts/setup-workspace.sh: {_install_setup_script(dest, force=args.force)}")

    env_example = dest / ".env.example"
    if not env_example.exists() or args.force:
        shutil.copyfile(_template_path("env.example"), env_example)
        report.append(f"  .env.example: {'overwritten' if args.force else 'created'}")
    else:
        report.append("  .env.example: skipped")

    print(f"MonkeyBot scaffold under {dest}:")
    print("\n".join(report))
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("new", help="Scaffold monkeybot_config/ and workspace dirs")
    p.add_argument("--dest", type=Path, default=Path.cwd(), help="Root directory for the bot")
    p.add_argument("--provider", help="model.provider value for monkeybot.yaml")
    p.add_argument("--model", help="model.name value for monkeybot.yaml")
    p.add_argument("--force", action="store_true", help="Overwrite existing scaffold files")
    p.add_argument("--yes", "-y", action="store_true", help="Skip interactive prompts")
    p.set_defaults(func=run_new)
