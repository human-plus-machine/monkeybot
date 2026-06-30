"""Workspace scaffolding from packaged ``monkeybot_config`` defaults."""

from __future__ import annotations

import stat
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

import yaml

_DEFAULTS_PKG: Final = "monkeybot.monkeybot_config"
_SCAFFOLD_PKG: Final = "monkeybot.scaffold"

# (filename in packaged defaults, output name under <dest>/monkeybot_config/)
_CONFIG_BUNDLE: Final[tuple[tuple[str, str], ...]] = (
    ("monkeybot.example.yaml", "monkeybot.example.yaml"),
    ("mcp.json", "mcp.json"),
    ("command_allowlist.yaml", "command_allowlist.yaml"),
    ("AGENT.md", "AGENT.md"),
    ("env.example", "env.example"),
    ("otel-collector.example.yaml", "otel-collector.example.yaml"),
)

_MEMORY_INDEX: Final = (
    "# Memory index\n\nAdd sections here or let memory tools populate this file.\n"
)


def _install_file(dest: Path, src: Traversable, *, force: bool) -> str:
    # ponytail: read_bytes() avoids resources.as_file() temp-file lifetime issue in zip distributions
    if dest.exists() and not force:
        return "skipped"
    existed = dest.exists()
    dest.write_bytes(src.read_bytes())
    return "overwritten" if existed else "created"


def install_config_bundle(cfg_dir: Path, *, force: bool) -> list[str]:
    """Copy packaged defaults into ``cfg_dir``; return report lines."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for src_name, dest_name in _CONFIG_BUNDLE:
        status = _install_file(
            cfg_dir / dest_name,
            resources.files(_DEFAULTS_PKG) / src_name,
            force=force,
        )
        lines.append(f"  monkeybot_config/{dest_name}: {status}")
    return lines


def write_active_config(
    cfg_dir: Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    force: bool = False,
) -> str:
    """Create or update ``monkeybot.yaml`` from the packaged example."""
    active = cfg_dir / "monkeybot.yaml"
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
    example_text = (resources.files(_DEFAULTS_PKG) / "monkeybot.example.yaml").read_text(encoding="utf-8")
    active.write_text(example_text, encoding="utf-8")
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
    if existed:
        return "overwritten"
    if provider or model:
        return "created"
    return "created (from monkeybot.example.yaml)"


def ensure_memory(dest: Path, *, force: bool) -> list[str]:
    memory = dest / "data" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    idx = memory / "INDEX.md"
    if not idx.exists() or force:
        existed = idx.exists()
        idx.write_text(_MEMORY_INDEX, encoding="utf-8")
        return [f"  data/memory/INDEX.md: {'overwritten' if existed else 'created'}"]
    return ["  data/memory/INDEX.md: skipped"]


def ensure_workspace(dest: Path, *, force: bool) -> list[str]:
    """Create workspace/ sandbox and workspace/skills -> ../skills symlink."""
    lines: list[str] = []
    workspace = dest / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    gitkeep = workspace / ".gitkeep"
    if not gitkeep.exists() or force:
        gitkeep.touch(exist_ok=True)
        lines.append(
            f"  workspace/.gitkeep: {'overwritten' if force and gitkeep.exists() else 'created'}"
        )
    else:
        lines.append("  workspace/.gitkeep: skipped")

    dest.joinpath("skills").mkdir(parents=True, exist_ok=True)
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


def install_setup_script(dest: Path, *, force: bool) -> str:
    scripts = dest / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    dest_script = scripts / "setup-workspace.sh"
    if dest_script.exists() and not force:
        return "skipped"
    existed = dest_script.exists()
    dest_script.write_bytes((resources.files(_SCAFFOLD_PKG) / "setup-workspace.sh").read_bytes())
    dest_script.chmod(dest_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return "overwritten" if existed else "created"


def install_env_example(dest: Path, *, force: bool) -> str:
    env_example = dest / ".env.example"
    if env_example.exists() and not force:
        return "skipped"
    existed = env_example.exists()
    _install_file(env_example, resources.files(_DEFAULTS_PKG) / "env.example", force=True)
    return "overwritten" if existed else "created"


def run_new(
    *,
    dest: Path,
    force: bool,
    provider: str | None = None,
    model: str | None = None,
) -> list[str]:
    """Full scaffold: config bundle, workspace, env example, and setup script."""
    cfg_dir = dest / "monkeybot_config"
    report = install_config_bundle(cfg_dir, force=force)
    report.append(
        f"  monkeybot_config/monkeybot.yaml: "
        f"{write_active_config(cfg_dir, provider=provider, model=model, force=force)}"
    )
    report.extend(ensure_memory(dest, force=force))
    report.extend(ensure_workspace(dest, force=force))
    report.append(f"  scripts/setup-workspace.sh: {install_setup_script(dest, force=force)}")
    report.append(f"  .env.example: {install_env_example(dest, force=force)}")
    return report
