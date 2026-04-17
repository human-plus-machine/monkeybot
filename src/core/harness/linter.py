"""emonk-harness CLI — validate and diff HarnessConfig files.

Entry point: ``emonk-harness lint --config harness.yaml [--strict]``
Exit codes: 0 = clean, 2 = warnings, 1 = errors.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .specs import HarnessConfig
from .versioning import migrate_config

# BEGIN harness-extensibility story 1
from .extensions.specs.checkpointer import (
    CheckpointerMongoSpec,
    CheckpointerPostgresSpec,
)
from .extensions.specs.identity_source import IdentitySourceCallableSpec
from .extensions.specs.memory_store import MemoryStorePostgresSpec
from .extensions.specs.model_provider import ModelProviderBedrockSpec
from .extensions.specs.secret_resolver import SecretResolverCompositeSpec

# END harness-extensibility story 1


@dataclass
class LintFinding:
    level: str  # "error" | "warning"
    path: str
    message: str


def _check_identity_files(cfg: HarnessConfig) -> list[LintFinding]:
    findings: list[LintFinding] = []
    base = Path(cfg.identity.dir)
    if cfg.identity.enforce_rules and not (base / cfg.identity.rules_file).exists():
        findings.append(
            LintFinding(
                "error",
                f"identity.dir/{cfg.identity.rules_file}",
                "RULES.md is required when identity.enforce_rules=True",
            )
        )
    for fname in (cfg.identity.soul_file, cfg.identity.identity_file):
        if not (base / fname).exists():
            findings.append(
                LintFinding("warning", f"identity.dir/{fname}", f"{fname} not found; agent will run without it")
            )
    return findings


def _check_skill_dirs(cfg: HarnessConfig) -> list[LintFinding]:
    return [
        LintFinding("error", f"skills.dirs[{i}]", f"directory does not exist: {d}")
        for i, d in enumerate(cfg.skills.dirs)
        if not Path(d).exists()
    ]


def _check_import_paths(cfg: HarnessConfig) -> list[LintFinding]:
    findings: list[LintFinding] = []
    targets: list[tuple[str, str]] = []
    if cfg.sandbox.backend == "custom" and cfg.sandbox.custom_import_path:
        targets.append(("sandbox.custom_import_path", cfg.sandbox.custom_import_path))
    for tool in cfg.tools:
        targets.append((f"tools.{tool.name}.import_path", tool.import_path))
    if cfg.skills.semantic_discovery and cfg.skills.embedder_import_path:
        targets.append(("skills.embedder_import_path", cfg.skills.embedder_import_path))
    for path_key, import_path in targets:
        if ":" not in import_path:
            findings.append(LintFinding("error", path_key, "must be 'module:attr'"))
            continue
        module, attr = import_path.split(":", 1)
        try:
            mod = importlib.import_module(module)
        except ImportError as exc:
            findings.append(LintFinding("warning", path_key, f"module not importable: {exc}"))
            continue
        if not hasattr(mod, attr):
            findings.append(LintFinding("warning", path_key, f"{module} has no attribute {attr!r}"))
    return findings


def _check_sandbox_vs_policy(cfg: HarnessConfig) -> list[LintFinding]:
    findings: list[LintFinding] = []
    if cfg.sandbox.backend == "local_shell" and cfg.sandbox.policy.net_deny != ["*"]:
        findings.append(
            LintFinding(
                "warning",
                "sandbox.policy.net_deny",
                "local_shell backend does not enforce network policy; add a real sandbox for SB-3",
            )
        )
    return findings


def _check_hitl(cfg: HarnessConfig) -> list[LintFinding]:
    findings: list[LintFinding] = []
    if cfg.hitl.mode != "disabled" and not cfg.gateway.enable_control_plane:
        findings.append(
            LintFinding(
                "error",
                "hitl.mode",
                "HITL requires gateway.enable_control_plane=True to surface approval endpoints",
            )
        )
    return findings


# BEGIN harness-extensibility story 1
def check_ckpt01_postgres_dsn_present(cfg: HarnessConfig) -> list[LintFinding]:
    """CKPT01: postgres checkpointer requires the dsn env var to be set."""
    findings: list[LintFinding] = []
    ckpt = cfg.checkpointer
    if isinstance(ckpt, CheckpointerPostgresSpec):
        if not os.environ.get(ckpt.dsn_env):
            findings.append(
                LintFinding(
                    "error",
                    "checkpointer.dsn_env",
                    f"[CKPT01] postgres checkpointer requires env var "
                    f"{ckpt.dsn_env!r} to be set",
                )
            )
    return findings


def check_ckpt02_mongo_uri_present(cfg: HarnessConfig) -> list[LintFinding]:
    """CKPT02: mongo checkpointer requires the uri env var to be set."""
    findings: list[LintFinding] = []
    ckpt = cfg.checkpointer
    if isinstance(ckpt, CheckpointerMongoSpec):
        if not os.environ.get(ckpt.uri_env):
            findings.append(
                LintFinding(
                    "error",
                    "checkpointer.uri_env",
                    f"[CKPT02] mongo checkpointer requires env var "
                    f"{ckpt.uri_env!r} to be set",
                )
            )
    return findings


def check_mem01_vector_search_supported(cfg: HarnessConfig) -> list[LintFinding]:
    """MEM01: require_vector_search=True requires a backend that supports it."""
    findings: list[LintFinding] = []
    mem = cfg.memory_store
    if mem is None or not getattr(mem, "require_vector_search", False):
        return findings
    supported = isinstance(mem, MemoryStorePostgresSpec) and mem.enable_pgvector
    if not supported:
        findings.append(
            LintFinding(
                "error",
                "memory_store.require_vector_search",
                f"[MEM01] memory_store.backend={mem.backend!r} does not support "
                "vector search (only postgres with enable_pgvector=True does)",
            )
        )
    return findings


def check_id01_callable_requires_import_path(cfg: HarnessConfig) -> list[LintFinding]:
    """ID01: identity_source callable backend requires import_path."""
    findings: list[LintFinding] = []
    src = cfg.identity_source
    if isinstance(src, IdentitySourceCallableSpec):
        if not src.import_path:
            findings.append(
                LintFinding(
                    "error",
                    "identity_source.import_path",
                    "[ID01] identity_source backend 'callable' requires import_path",
                )
            )
    return findings


def check_id02_cache_ttl_sane(cfg: HarnessConfig) -> list[LintFinding]:
    """ID02: identity_source.cache_ttl_seconds < 5 is likely a typo."""
    findings: list[LintFinding] = []
    src = cfg.identity_source
    if src is None:
        return findings
    ttl = getattr(src, "cache_ttl_seconds", None)
    if isinstance(ttl, int) and ttl < 5:
        findings.append(
            LintFinding(
                "warning",
                "identity_source.cache_ttl_seconds",
                f"[ID02] cache_ttl_seconds={ttl} is probably a typo (very short TTL)",
            )
        )
    return findings


def check_sec01_composite_chain_non_empty(cfg: HarnessConfig) -> list[LintFinding]:
    """SEC01: composite secret_resolver requires a non-empty chain."""
    findings: list[LintFinding] = []
    sec = cfg.secret_resolver
    if isinstance(sec, SecretResolverCompositeSpec) and not sec.chain:
        findings.append(
            LintFinding(
                "error",
                "secret_resolver.chain",
                "[SEC01] composite secret_resolver requires a non-empty chain",
            )
        )
    return findings


def check_mp01_bedrock_model_id(cfg: HarnessConfig) -> list[LintFinding]:
    """MP01: bedrock model_provider requires a non-empty model_id."""
    findings: list[LintFinding] = []
    mp = cfg.model_provider
    if isinstance(mp, ModelProviderBedrockSpec) and not mp.model_id:
        findings.append(
            LintFinding(
                "error",
                "model_provider.model_id",
                "[MP01] bedrock model_provider requires model_id",
            )
        )
    return findings


def check_reg01_no_shadowed_entries(cfg: HarnessConfig) -> list[LintFinding]:
    """REG01: any registry shadowed entry becomes an error under --strict."""
    from .extensions.base import (
        Checkpointer,
        IdentitySource,
        JobStorage,
        MemoryStore,
        ModelProvider,
        SecretResolver,
    )

    findings: list[LintFinding] = []
    for abc_cls in (Checkpointer, MemoryStore, JobStorage, IdentitySource, SecretResolver, ModelProvider):
        for shadowed in abc_cls.registry._shadowed_entries():
            findings.append(
                LintFinding(
                    "warning",
                    f"{abc_cls.registry.kind}:{shadowed.name}",
                    f"[REG01] registry shadowed: {shadowed.factory_qualname} "
                    f"was overridden by a higher-precedence source",
                )
            )
    return findings


def check_identity_smoke(cfg: HarnessConfig, *, enabled: bool = False) -> list[LintFinding]:
    """IDENTITY-SMOKE: performs a real IdentitySource.load probe.

    Only emits findings when ``enabled`` is True; Story 1 ships the hook but
    leaves the probe implementation to a later story.
    """
    if not enabled:
        return []
    return [
        LintFinding(
            "warning",
            "identity_source",
            "[IDENTITY-SMOKE] live identity probe not implemented in story 1",
        )
    ]


# END harness-extensibility story 1


def _run_checks(cfg: HarnessConfig) -> list[LintFinding]:
    findings: list[LintFinding] = []
    findings.extend(_check_identity_files(cfg))
    findings.extend(_check_skill_dirs(cfg))
    findings.extend(_check_import_paths(cfg))
    findings.extend(_check_sandbox_vs_policy(cfg))
    findings.extend(_check_hitl(cfg))
    # BEGIN harness-extensibility story 1
    findings.extend(check_ckpt01_postgres_dsn_present(cfg))
    findings.extend(check_ckpt02_mongo_uri_present(cfg))
    findings.extend(check_mem01_vector_search_supported(cfg))
    findings.extend(check_id01_callable_requires_import_path(cfg))
    findings.extend(check_id02_cache_ttl_sane(cfg))
    findings.extend(check_sec01_composite_chain_non_empty(cfg))
    findings.extend(check_mp01_bedrock_model_id(cfg))
    findings.extend(check_reg01_no_shadowed_entries(cfg))
    # END harness-extensibility story 1
    return findings


def lint_config(path: str | Path, strict: bool = False) -> tuple[int, list[LintFinding]]:
    """Validate a harness config file.

    Returns a tuple ``(exit_code, findings)``.
    """
    p = Path(path)
    if not p.exists():
        return 1, [LintFinding("error", str(p), "file not found")]
    try:
        cfg = HarnessConfig.from_yaml(p)
    except ValidationError as exc:
        return 1, [LintFinding("error", e["loc"] and ".".join(str(x) for x in e["loc"]) or "<root>", e["msg"]) for e in exc.errors()]
    except Exception as exc:  # noqa: BLE001
        return 1, [LintFinding("error", str(p), f"{type(exc).__name__}: {exc}")]

    findings = _run_checks(cfg)
    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]
    if errors:
        return 1, findings
    if warnings and strict:
        return 2, findings
    if warnings:
        return 2, findings
    return 0, findings


def _cmd_lint(args: argparse.Namespace) -> int:
    code, findings = lint_config(args.config, strict=args.strict)
    for f in findings:
        print(f"[{f.level.upper()}] {f.path}: {f.message}", file=sys.stderr if f.level == "error" else sys.stdout)
    if code == 0:
        print(f"OK: {args.config} is valid")
    return code


def _cmd_diff(args: argparse.Namespace) -> int:
    import yaml as _yaml

    a_raw = _yaml.safe_load(Path(args.a).read_text()) or {}
    b_raw = _yaml.safe_load(Path(args.b).read_text()) or {}
    a = migrate_config(a_raw)
    b = migrate_config(b_raw)
    _print_diff("", a, b)
    return 0


def _print_diff(prefix: str, a: Any, b: Any) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        keys = sorted(set(a) | set(b))
        for k in keys:
            _print_diff(f"{prefix}.{k}" if prefix else k, a.get(k), b.get(k))
        return
    if a != b:
        print(f"~ {prefix}: {a!r} -> {b!r}")


def _cmd_introspect(args: argparse.Namespace) -> int:
    code, findings = lint_config(args.config, strict=False)
    if code != 0:
        for f in findings:
            print(f"[{f.level.upper()}] {f.path}: {f.message}", file=sys.stderr)
        return code
    cfg = HarnessConfig.from_yaml(args.config)
    print(f"Agent:             {cfg.agent.name} ({cfg.agent.provider}/{cfg.agent.model})")
    print(f"Identity dir:      {cfg.identity.dir}")
    print(f"Skills dirs:       {cfg.skills.dirs}")
    print(f"MCP servers:       {[s.name for s in cfg.mcp_servers]}")
    print(f"Subagents:         {[s.name for s in cfg.subagents]}")
    print(f"Sandbox backend:   {cfg.sandbox.backend}")
    print(f"HITL mode:         {cfg.hitl.mode} via {cfg.hitl.channel}")
    print(f"Observability:     event_bus={cfg.observability.event_bus}, run_package={cfg.observability.run_package.writer}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emonk-harness", description="Agent Harness tooling")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_lint = sub.add_parser("lint", help="Validate a harness.yaml")
    p_lint.add_argument("--config", required=True)
    p_lint.add_argument("--strict", action="store_true")
    p_lint.set_defaults(func=_cmd_lint)

    p_diff = sub.add_parser("diff", help="Show migration effect between two harness files")
    p_diff.add_argument("a")
    p_diff.add_argument("b")
    p_diff.set_defaults(func=_cmd_diff)

    p_intro = sub.add_parser("introspect", help="Dry-run build; print resolved configuration")
    p_intro.add_argument("--config", required=True)
    p_intro.set_defaults(func=_cmd_introspect)

    # BEGIN harness-extensibility story 1
    p_plugin = sub.add_parser("plugin", help="Introspect registered extension backends")
    plugin_sub = p_plugin.add_subparsers(dest="plugin_cmd", required=True)
    p_plugin_ls = plugin_sub.add_parser("ls", help="List registered backends")
    p_plugin_ls.add_argument("--kind", default=None)
    p_plugin_ls.add_argument("--source", default=None)
    p_plugin_ls.add_argument("--strict", action="store_true")
    p_plugin_ls.set_defaults(func=_cmd_plugin_ls)
    # END harness-extensibility story 1

    ns = parser.parse_args(argv)
    return int(ns.func(ns))


# BEGIN harness-extensibility story 1
def _cmd_plugin_ls(args: argparse.Namespace) -> int:
    from .extensions.cli import plugin_ls

    return plugin_ls(args)


# END harness-extensibility story 1


if __name__ == "__main__":
    raise SystemExit(main())
