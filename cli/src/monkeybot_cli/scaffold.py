"""Workspace scaffolding from CLI-packaged ``scaffold_defaults``."""

from __future__ import annotations

import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

import yaml

from monkeybot_cli.compat import COMPATIBLE_CORE_RANGE
from monkeybot_cli.extras_catalog import normalize_extra_token, provider_extra_name

_DEFAULTS_PKG: Final = "monkeybot_cli.scaffold_defaults"
# Matches packaged monkeybot.example.yaml default when ``--provider`` is omitted.
_DEFAULT_PROVIDER: Final = "gemini"

# (filename in packaged defaults, output name under <dest>/monkeybot_config/)
_CONFIG_BUNDLE: Final[tuple[tuple[str, str], ...]] = (
    ("monkeybot.example.yaml", "monkeybot.example.yaml"),
    ("mcp.json", "mcp.json"),
    ("command_allowlist.yaml", "command_allowlist.yaml"),
    ("permissions.yaml", "permissions.yaml"),
    ("AGENT.md", "AGENT.md"),
    ("otel-collector.example.yaml", "otel-collector.example.yaml"),
    ("opensandbox.docker.toml", "opensandbox.docker.toml"),
)

_MEMORY_INDEX: Final = (
    "# Memory Index\n\nAdd sections here or let memory tools populate this file.\n"
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
    example_text = (resources.files(_DEFAULTS_PKG) / "monkeybot.example.yaml").read_text(
        encoding="utf-8"
    )
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
    memory = dest / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for sub in ("episodic", "semantic", "procedural", "working", "raw"):
        d = memory / sub
        d.mkdir(parents=True, exist_ok=True)
        keep = d / ".gitkeep"
        if not keep.exists() or force:
            keep.touch(exist_ok=True)
            lines.append(f"  memory/{sub}/: ensured")
    idx = memory / "INDEX.md"
    if not idx.exists() or force:
        existed = idx.exists()
        idx.write_text(_MEMORY_INDEX, encoding="utf-8")
        lines.append(f"  memory/INDEX.md: {'overwritten' if existed else 'created'}")
    else:
        lines.append("  memory/INDEX.md: skipped")
    return lines or ["  memory/: ensured"]


def ensure_workspace(dest: Path, *, force: bool) -> list[str]:
    """Create the writable workspace and a separate trusted skills root."""
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

    for rel in (
        "browser/playbooks",
        "browser/Screenshots",
        "generated-media/images",
    ):
        path = workspace / rel
        path.mkdir(parents=True, exist_ok=True)
        lines.append(f"  workspace/{rel}/: ensured")

    dest.joinpath("skills").mkdir(parents=True, exist_ok=True)
    return lines


def install_browser_skill(dest: Path, *, force: bool) -> str:
    """Install the bundled, trusted browser procedure into ``skills/browser``."""
    target = dest / "skills" / "browser" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    return _install_file(target, resources.files(_DEFAULTS_PKG) / "browser" / "SKILL.md", force=force)


def install_image_generator_skill(dest: Path, *, force: bool) -> list[str]:
    """Install the Vertex image-generator skill (SKILL.md + generate_image.py)."""
    skill_dir = dest / "skills" / "image-generator"
    skill_dir.mkdir(parents=True, exist_ok=True)
    src = resources.files(_DEFAULTS_PKG) / "image-generator"
    lines: list[str] = []
    for name in ("SKILL.md", "generate_image.py"):
        status = _install_file(skill_dir / name, src / name, force=force)
        lines.append(f"  skills/image-generator/{name}: {status}")
    return lines


def install_loop_skill(dest: Path, *, force: bool) -> str:
    """Install the bundled, trusted scheduled-loop procedure into ``skills/loop``."""
    target = dest / "skills" / "loop" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    return _install_file(target, resources.files(_DEFAULTS_PKG) / "loop" / "SKILL.md", force=force)


def install_env_example(dest: Path, *, force: bool) -> str:
    env_example = dest / ".env.example"
    if env_example.exists() and not force:
        return "skipped"
    existed = env_example.exists()
    _install_file(env_example, resources.files(_DEFAULTS_PKG) / "env.example", force=True)
    return "overwritten" if existed else "created"


def install_container_files(dest: Path, *, force: bool) -> list[str]:
    """Install a deployable agent image definition, never the repo demo image."""
    files = (("Dockerfile", "Dockerfile"), ("dockerignore", ".dockerignore"))
    lines: list[str] = []
    for source, target_name in files:
        target = dest / target_name
        status = _install_file(target, resources.files(_DEFAULTS_PKG) / source, force=force)
        lines.append(f"  {target_name}: {status}")
    return lines


def _sanitize_project_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9._-]+", "-", raw.strip().lower()).strip("-._")
    return name or "agent"


def collect_extras(
    *,
    provider: str | None = None,
    extras: list[str] | None = None,
) -> list[str]:
    """Return unique package extras: primary provider first, then extras."""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(extra: str | None) -> None:
        if not extra or extra in seen:
            return
        seen.add(extra)
        ordered.append(extra)

    effective = provider if provider is not None else _DEFAULT_PROVIDER
    _add(provider_extra_name(effective))
    for raw in extras or ():
        token = normalize_extra_token(raw)
        if token is not None:
            _add(token)
    return ordered


def monkeybot_requirement(
    *,
    provider: str | None = None,
    extras: list[str] | None = None,
) -> str:
    """Return a PyPI ``monkeybot`` / ``monkeybot[a,b]`` requirement string."""
    ordered = collect_extras(provider=provider, extras=extras)
    if ordered:
        return f"monkeybot[{','.join(ordered)}]{COMPATIBLE_CORE_RANGE}"
    return f"monkeybot{COMPATIBLE_CORE_RANGE}"


def monkeybot_dep_for_provider(provider: str | None) -> str:
    """Backward-compatible wrapper: provider-only requirement."""
    return monkeybot_requirement(provider=provider)


def write_agent_pyproject(
    dest: Path,
    *,
    provider: str | None = None,
    extras: list[str] | None = None,
    force: bool = False,
) -> str:
    """Write agent-project ``pyproject.toml`` with a PyPI ``monkeybot[…]`` dep."""
    path = dest / "pyproject.toml"
    if path.exists() and not force:
        return "skipped"
    existed = path.exists()
    # Sandbox + web search ship enabled in every generated agent config.
    dep = monkeybot_requirement(
        provider=provider,
        extras=["sandbox", "web-search", *(extras or [])],
    )
    name = _sanitize_project_name(dest.name)
    path.write_text(
        (
            "[project]\n"
            f'name = "{name}"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            f'  "{dep}",\n'
            '  "monkeybot-browser-mcp>=0.2.0,<1",\n'
            "]\n"
            "\n[tool.uv]\n"
            "package = false\n"
        ),
        encoding="utf-8",
    )
    return "overwritten" if existed else "created"


def run_new(
    *,
    dest: Path,
    force: bool,
    provider: str | None = None,
    model: str | None = None,
    extras: list[str] | None = None,
) -> list[str]:
    """Full scaffold: config bundle, empty skills root, writable state, and image files.

    Capability skills (``browser``, ``image-generator``, ``loop``) are packaged
    under ``scaffold_defaults`` but not installed into new agents for now —
    the Mac Main Agent loads them from ``~/.monkeybot/.internal/skills``
    instead. Re-enable via ``install_*_skill`` when custom agents should get
    them again.
    """
    cfg_dir = dest / "monkeybot_config"
    report = install_config_bundle(cfg_dir, force=force)
    report.append(
        f"  monkeybot_config/monkeybot.yaml: "
        f"{write_active_config(cfg_dir, provider=provider, model=model, force=force)}"
    )
    report.extend(ensure_memory(dest, force=force))
    report.extend(ensure_workspace(dest, force=force))
    report.append("  skills/: ensured (empty — capability skills deferred)")
    report.append(f"  .env.example: {install_env_example(dest, force=force)}")
    report.extend(install_container_files(dest, force=force))
    report.append(
        f"  pyproject.toml: "
        f"{write_agent_pyproject(dest, provider=provider, extras=extras, force=force)}"
    )
    return report
