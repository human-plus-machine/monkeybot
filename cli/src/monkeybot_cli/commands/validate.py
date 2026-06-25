"""monkeybot validate — check monkeybot.yaml and related paths."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from monkeybot.core.config import (
    SUPPORTED_YAML_MODEL_PROVIDERS,
    validate_monkeybot_yaml_doc,
    validate_provider_env,
)
from monkeybot.core.config.runtime_env import ENV_MAP
from monkeybot.core.config.settings import ConfigError, normalize_model_provider
from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict

from monkeybot_cli.config_resolve import load_agent_dotenv, resolve_config
from monkeybot_cli.output import CommandReport, check


def _resolve_path(base: Path, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def _flatten_to_env(doc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for (section, key), env_name in ENV_MAP.items():
        sec = doc.get(section)
        if not isinstance(sec, dict) or key not in sec:
            continue
        raw = sec[key]
        if raw is None:
            continue
        if isinstance(raw, bool):
            out[env_name] = "true" if raw else "false"
        else:
            out[env_name] = str(raw)
    gcp = doc.get("gcp") if isinstance(doc.get("gcp"), dict) else {}
    if isinstance(gcp, dict) and gcp.get("project_id"):
        out["GCP_PROJECT_ID"] = str(gcp["project_id"])
    return out


def _collect_mcp_env_refs(obj: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(obj, str):
        for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", obj):
            refs.add(m.group(1))
    elif isinstance(obj, dict):
        for v in obj.values():
            refs.update(_collect_mcp_env_refs(v))
    elif isinstance(obj, list):
        for v in obj:
            refs.update(_collect_mcp_env_refs(v))
    return refs


def run_validate(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    report = CommandReport(command="validate", ok=True, config_path=None)

    if config_path is None:
        check(
            report,
            id="config.file.exists",
            category="config",
            severity="error",
            passed=False,
            message="monkeybot.yaml not found",
            remediation="Run `monkeybot new` or set MONKEYBOT_CONFIG",
        )
        return report.emit(as_json=args.json)

    report.config_path = str(config_path.resolve())
    base = config_path.parent.parent if config_path.parent.name == "monkeybot_config" else Path.cwd()

    try:
        raw = config_path.read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
        check(report, id="config.yaml.parses", category="config", severity="error", passed=True)
    except yaml.YAMLError as exc:
        check(
            report,
            id="config.yaml.parses",
            category="config",
            severity="error",
            passed=False,
            message=str(exc),
        )
        return report.emit(as_json=args.json)

    if not isinstance(doc, dict):
        check(
            report,
            id="config.root.is_mapping",
            category="config",
            severity="error",
            passed=False,
            message=f"Expected mapping at root, got {type(doc).__name__}",
        )
        return report.emit(as_json=args.json)
    check(report, id="config.root.is_mapping", category="config", severity="error", passed=True)

    _, merged = load_monkeybot_yaml_dict(config_path)
    includes = doc.get("includes")
    if includes:
        missing = []
        if isinstance(includes, list):
            for item in includes:
                if isinstance(item, str):
                    inc = (config_path.parent / item.strip()).resolve()
                    if not inc.is_file():
                        missing.append(str(inc))
        check(
            report,
            id="config.includes.resolve",
            category="config",
            severity="warning",
            passed=not missing,
            message=f"Missing includes: {', '.join(missing)}" if missing else "All includes resolved",
        )
    else:
        check(report, id="config.includes.resolve", category="config", severity="warning", passed=True, skip=True)

    model = merged.get("model") if isinstance(merged.get("model"), dict) else {}
    provider = str(model.get("provider", "")).strip().lower() if isinstance(model, dict) else ""
    model_name = str(model.get("name", "")).strip() if isinstance(model, dict) else ""
    check(
        report,
        id="model.provider.supported",
        category="config",
        severity="error",
        passed=not provider or provider in SUPPORTED_YAML_MODEL_PROVIDERS,
        message=f"model.provider '{provider}' is not supported" if provider and provider not in SUPPORTED_YAML_MODEL_PROVIDERS else "",
        field="model.provider",
        value=provider,
        expected=sorted(SUPPORTED_YAML_MODEL_PROVIDERS),
        remediation="Set model.provider to a supported value.",
    )
    check(
        report,
        id="model.name.present",
        category="config",
        severity="error",
        passed=bool(model_name),
        message="model.name is required",
        field="model.name",
    )

    memory_uri = ""
    paths = merged.get("paths") if isinstance(merged.get("paths"), dict) else {}
    if isinstance(paths, dict):
        memory_uri = str(paths.get("memory_storage_uri", ""))
    memory_backend = "gcs" if memory_uri.startswith("gcs://") else "local"
    check(
        report,
        id="memory.backend.supported",
        category="config",
        severity="error",
        passed=memory_backend in {"local", "gcs", "drive"},
        message=f"Unsupported memory backend from uri: {memory_uri}",
    )

    flat = _flatten_to_env(merged)
    flat.update({k: v for k, v in os.environ.items() if k.startswith(("GCP_", "GOOGLE_", "ANTHROPIC_VERTEX"))})
    norm_provider = normalize_model_provider(provider or "gemini")
    needs_gcp = memory_backend == "gcs" or norm_provider == "vertex_anthropic"
    gcp_present = any(
        os.environ.get(k, "").strip() or flat.get(k, "").strip()
        for k in ("GCP_PROJECT_ID", "VERTEX_AI_PROJECT_ID", "ANTHROPIC_VERTEX_PROJECT_ID", "GOOGLE_CLOUD_PROJECT")
    )
    if needs_gcp:
        check(
            report,
            id="gcp.project.required",
            category="config",
            severity="error",
            passed=gcp_present,
            message="GCP project id required for gcs memory or vertex-claude provider",
            remediation="Set gcp.project_id in monkeybot.yaml or GCP_PROJECT_ID in .env",
        )
    else:
        check(report, id="gcp.project.required", category="config", severity="error", passed=True, skip=True)

    try:
        validate_monkeybot_yaml_doc(merged, env=dict(os.environ))
        validate_provider_env({**flat, "MODEL_PROVIDER": provider or flat.get("MODEL_PROVIDER", "gemini")})
    except ConfigError as exc:
        check(
            report,
            id="model.provider.supported",
            category="config",
            severity="error",
            passed=False,
            message=str(exc).split("\n")[0],
        )

    if isinstance(paths, dict):
        agent_md = str(paths.get("agent_md", ""))
        if agent_md:
            ap = _resolve_path(base, agent_md)
            check(
                report,
                id="paths.agent_md.exists",
                category="paths",
                severity="error",
                passed=ap.is_file(),
                message=f"Missing AGENT.md at {ap}",
                field="paths.agent_md",
            )
        skills = str(paths.get("skills_path", ""))
        if skills:
            sp = _resolve_path(base, skills)
            check(
                report,
                id="paths.skills_path.exists",
                category="paths",
                severity="warning",
                passed=sp.is_dir(),
                message=f"Skills path missing: {sp}",
            )
        mcp_cfg = str(paths.get("mcp_config", ""))
        mcp_path: Path | None = None
        if mcp_cfg:
            mcp_path = _resolve_path(base, mcp_cfg)
            check(
                report,
                id="paths.mcp_config.exists",
                category="paths",
                severity="error",
                passed=mcp_path.is_file(),
                message=f"Missing MCP config: {mcp_path}",
            )
        allow = str(paths.get("command_allowlist_config", ""))
        if allow:
            ap = _resolve_path(base, allow)
            check(
                report,
                id="paths.command_allowlist.exists",
                category="paths",
                severity="warning",
                passed=ap.is_file(),
                message=f"Missing command allowlist: {ap}",
            )
        db_url = str(paths.get("db_url", ""))
        if db_url.startswith("sqlite:///"):
            db_file = Path(db_url.removeprefix("sqlite:///"))
            if not db_file.is_absolute():
                db_file = (base / db_file).resolve()
            writable = True
            try:
                db_file.parent.mkdir(parents=True, exist_ok=True)
                if db_file.exists():
                    conn = sqlite3.connect(db_file)
                    conn.close()
                else:
                    conn = sqlite3.connect(db_file)
                    conn.close()
            except OSError as exc:
                writable = False
                msg = str(exc)
            else:
                msg = ""
            check(
                report,
                id="paths.db_url.writable",
                category="paths",
                severity="error",
                passed=writable,
                message=msg or f"SQLite path ok: {db_file}",
            )

        if mcp_path and mcp_path.is_file():
            try:
                mcp_doc = json.loads(mcp_path.read_text(encoding="utf-8"))
                check(report, id="mcp.json.parses", category="mcp", severity="error", passed=True)
                servers = mcp_doc.get("mcpServers") if isinstance(mcp_doc, dict) else None
                shape_ok = isinstance(servers, dict)
                check(
                    report,
                    id="mcp.servers.shape_valid",
                    category="mcp",
                    severity="error",
                    passed=shape_ok,
                    message="mcp.json must have mcpServers object",
                )
                if shape_ok and isinstance(servers, dict):
                    refs = _collect_mcp_env_refs(servers)
                    missing_refs = [r for r in refs if not os.environ.get(r, "").strip()]
                    check(
                        report,
                        id="mcp.env_refs.resolvable",
                        category="mcp",
                        severity="warning",
                        passed=not missing_refs,
                        message=f"Unresolved MCP env refs: {', '.join(missing_refs)}" if missing_refs else "All MCP env refs set",
                    )
                    if args.check_mcp:
                        for name, srv in servers.items():
                            if not isinstance(srv, dict) or srv.get("enabled") is False:
                                continue
                            url = srv.get("url")
                            if isinstance(url, str) and url.startswith("http"):
                                import httpx

                                try:
                                    httpx.get(url, timeout=3.0)
                                    reachable = True
                                    rmsg = f"{name} reachable"
                                except Exception as exc:
                                    reachable = False
                                    rmsg = f"{name}: {exc}"
                                check(
                                    report,
                                    id="mcp.server.reachable",
                                    category="mcp",
                                    severity="warning",
                                    passed=reachable,
                                    message=rmsg,
                                )
                    else:
                        check(
                            report,
                            id="mcp.server.reachable",
                            category="mcp",
                            severity="warning",
                            passed=True,
                            skip=True,
                        )
            except json.JSONDecodeError as exc:
                check(
                    report,
                    id="mcp.json.parses",
                    category="mcp",
                    severity="error",
                    passed=False,
                    message=str(exc),
                )

    return report.emit(as_json=args.json)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("validate", help="Validate monkeybot.yaml and related files")
    p.add_argument("--json", action="store_true", help="Emit JSON report")
    p.add_argument("--config", help="Path to monkeybot.yaml")
    p.add_argument("--cwd", help="Working directory for path resolution")
    p.add_argument("--check-mcp", action="store_true", help="Probe MCP HTTP servers (network)")
    p.set_defaults(func=run_validate)
