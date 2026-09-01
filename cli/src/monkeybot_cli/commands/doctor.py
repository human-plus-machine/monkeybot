"""monkeybot doctor — environment readiness checks."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import tomllib
from pathlib import Path

import httpx
from monkeybot.core.layout import AgentLayout, bootstrap_agent_layout
from monkeybot.core.memory.config import memory_enabled_from_config
from monkeybot.core.tools.sandbox_executor import SandboxConfig

from monkeybot_cli.compat import COMPATIBLE_CORE_RANGE
from monkeybot_cli.config_resolve import (
    load_agent_dotenv,
    load_config_doc,
    resolve_agent_root,
    resolve_config,
)
from monkeybot_cli.gateway_health import port_free as _port_free
from monkeybot_cli.output import CommandReport, check
from monkeybot_cli.providers import credentials_present, extra_module, spec_for_provider
from monkeybot_cli.runtime_python import (
    CORE_PROBE,
    MANAGED_RUNTIME_SOURCE,
    MEMORY_PROBE,
    _probe,
    resolve_runtime_python,
    run_probe,
)


def _runtime_python_version(runtime) -> tuple[int, int, int]:
    """Ask the runtime interpreter for its version (major, minor, micro)."""
    ok, text = _probe(
        runtime,
        "import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2])",
    )
    if not ok:
        return (0, 0, 0)
    parts = text.split()
    if len(parts) < 3:
        return (0, 0, 0)
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return (0, 0, 0)


def _agent_defines_project_extra(agent_root: Path, extra: str) -> bool:
    """True when the agent ``pyproject.toml`` declares ``extra`` as a project optional."""
    path = agent_root / "pyproject.toml"
    if not path.is_file():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project")
    if not isinstance(project, dict):
        return False
    optional = project.get("optional-dependencies")
    return isinstance(optional, dict) and extra in optional


def _extra_remediation(extra: str, agent_root: Path, runtime) -> str:
    """Remediation text pointing at the agent project, not the CLI env."""
    if runtime.source in {"cli", MANAGED_RUNTIME_SOURCE}:
        refresh = (
            ", then re-run the agent to refresh the managed runtime"
            if runtime.source == MANAGED_RUNTIME_SOURCE
            else ""
        )
        return (
            "Config-only tree: install in the CLI env — "
            f"uv tool install --with 'monkeybot[{extra}]' monkeybot-cli"
            f"{refresh}"
        )
    if _agent_defines_project_extra(agent_root, extra):
        return f"Install in the agent project: cd {agent_root} && uv sync --extra {extra}"
    return (
        f"Add monkeybot[{extra}] to {agent_root}/pyproject.toml dependencies, "
        f"then run: cd {agent_root} && uv sync"
    )


def _storage_kind(uri: str) -> str:
    """Render storage kind without leaking URL credentials or filesystem details."""
    if "://" not in uri:
        return "unknown"
    scheme, remainder = uri.split("://", 1)
    if scheme == "sqlite":
        return "sqlite"
    host = remainder.split("@")[-1].split("/")[0]
    return f"{scheme}://{host}" if host else scheme


def _add_layout_checks(report: CommandReport, layout: AgentLayout) -> None:
    check(
        report,
        id="layout.resolved",
        category="layout",
        severity="error",
        passed=True,
        message="Canonical agent layout resolved",
        value={
            "agent_root": str(layout.agent_root),
            "workspace": str(layout.workspace_root),
            "skills": str(layout.skills_path),
            "data": str(layout.data_root),
            "db": _storage_kind(layout.db_url),
            "memory": _storage_kind(layout.memory_storage_uri),
        },
    )

    legacy_skills = layout.workspace_root / "skills"
    populated_skills = layout.skills_path.exists() and any(layout.skills_path.iterdir())
    legacy_exists = legacy_skills.exists() or legacy_skills.is_symlink()
    collision = legacy_exists and populated_skills
    source = str(legacy_skills)
    destination = str(layout.skills_path)
    check(
        report,
        id="layout.legacy_nested_skills",
        category="layout",
        severity="warning",
        passed=not legacy_exists,
        message=(
            "Legacy workspace/skills detected"
            + (
                "; destination skills/ is populated (resolve collision manually)"
                if collision
                else ""
            )
            if legacy_exists
            else "No legacy nested skills directory"
        ),
        remediation=(
            "Collision detected: do not move automatically; reconcile the two skill trees first."
            if collision
            else (
                f"Preview only (not executed): mv {shlex.quote(source)} {shlex.quote(destination)}"
            )
            if legacy_exists
            else None
        ),
        value=(
            {
                "source": source,
                "destination": destination,
                "collision": collision,
                "action": None if collision else "mv",
            }
            if legacy_exists
            else None
        ),
    )

    browser_enabled = False
    if layout.mcp_config_path.is_file():
        try:
            servers = json.loads(layout.mcp_config_path.read_text(encoding="utf-8")).get(
                "mcpServers", {}
            )
            browser = servers.get("browser", {}) if isinstance(servers, dict) else {}
            browser_enabled = bool(browser.get("enabled")) if isinstance(browser, dict) else False
        except (OSError, json.JSONDecodeError):
            pass
    check(
        report,
        id="browser.bundled",
        category="layout",
        severity="warning",
        passed=True,
        message="browser: enabled" if browser_enabled else "browser: disabled (bundled)",
    )

    sandbox = SandboxConfig.from_env()
    sandbox_mode = "remote (compute-only)" if not sandbox.shared_filesystem else "shared-filesystem"
    check(
        report,
        id="sandbox.status",
        category="layout",
        severity="warning",
        passed=True,
        message=f"sandbox: {'enabled' if sandbox.enabled else 'disabled'} ({sandbox_mode})",
        value={"image": sandbox.image, "mode": sandbox_mode},
    )


_OLLAMA_LOCAL_DOCS = "docs/ollama-local.md"
_OLLAMA_NUM_CTX_LARGE = 32_768
_OLLAMA_REASONING_TAGS = ("qwen3", "gemma4", "gemma-4", "deepseek-r1", "qwq", "magistral")


def _is_local_ollama_provider(provider: str) -> bool:
    """True for ollama-local and legacy ``ollama`` (not ollama-cloud)."""
    key = provider.strip().lower().replace("_", "-")
    return key in {"ollama-local", "ollama"}


def _looks_like_reasoning_model(model_name: str) -> bool:
    name = model_name.lower()
    return any(tag in name for tag in _OLLAMA_REASONING_TAGS)


def _parse_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _strict_positive_int(raw: object) -> tuple[int | None, bool]:
    """Return ``(value, invalid)``. Unset is ``(None, False)``; garbage or ``<1`` is invalid."""
    if raw is None or raw == "":
        return None, False
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, True
    if raw < 1:
        return None, True
    return raw, False


def _add_ollama_local_checks(
    report: CommandReport,
    *,
    provider: str,
    model_name: str,
    thinking_budget: object,
    num_ctx: object,
) -> None:
    """Warn on local-Ollama prefix-cache traps. Always emit the four check ids."""
    local = _is_local_ollama_provider(provider)
    mlx = "-mlx" in model_name.lower()
    check(
        report,
        id="ollama.local.mlx_runner",
        category="provider",
        severity="warning",
        passed=not mlx,
        skip=not local,
        message=(
            f"model.name {model_name!r} looks like an MLX pack; prefix KV cache "
            "often does not reuse between agent steps"
            if mlx
            else "model.name is not an MLX pack"
        ),
        field="model.name",
        value=model_name or None,
        remediation=(
            f"Prefer a GGUF tag for tool-calling loops. See {_OLLAMA_LOCAL_DOCS}." if mlx else None
        ),
        docs=_OLLAMA_LOCAL_DOCS,
    )
    budget = _parse_int(thinking_budget)
    if budget is None:
        budget = -1
    reasoning = _looks_like_reasoning_model(model_name)
    thinking_on_default = budget == -1
    check(
        report,
        id="ollama.local.thinking_default",
        category="provider",
        severity="warning",
        passed=not thinking_on_default,
        skip=not local or not reasoning,
        message=(
            "thinking_budget is -1 (server default); reasoning models generate "
            "thinking tokens before the first visible reply"
            if thinking_on_default
            else f"thinking_budget is {budget}"
        ),
        field="model.thinking_budget",
        value=budget,
        remediation=(
            f"Set thinking_budget: 0 to send reasoning_effort: none. See {_OLLAMA_LOCAL_DOCS}."
            if thinking_on_default
            else None
        ),
        docs=_OLLAMA_LOCAL_DOCS,
    )
    ctx, ctx_invalid = _strict_positive_int(num_ctx)
    check(
        report,
        id="ollama.local.num_ctx_invalid",
        category="provider",
        severity="error",
        passed=not ctx_invalid,
        skip=not local or (ctx is None and not ctx_invalid),
        message=(
            f"num_ctx must be a positive integer, got {num_ctx!r}"
            if ctx_invalid
            else (f"num_ctx is {ctx}" if ctx is not None else "num_ctx is unset")
        ),
        field="model.num_ctx",
        value=num_ctx,
        remediation=(
            f"Set a positive integer num_ctx (e.g. 8192), or omit it. See {_OLLAMA_LOCAL_DOCS}."
            if ctx_invalid
            else None
        ),
        docs=_OLLAMA_LOCAL_DOCS,
    )
    large = ctx is not None and ctx > _OLLAMA_NUM_CTX_LARGE
    check(
        report,
        id="ollama.local.num_ctx_large",
        category="provider",
        severity="warning",
        passed=not large,
        skip=not local or ctx is None or ctx_invalid,
        message=(
            f"num_ctx {ctx} is large for a local runner; prefill will be slow"
            if large
            else (f"num_ctx is {ctx}" if ctx is not None else "num_ctx is unset")
        ),
        field="model.num_ctx",
        value=ctx,
        remediation=(
            f"Pin a modest num_ctx (e.g. 8192). Do not copy context_window. See {_OLLAMA_LOCAL_DOCS}."
            if large
            else None
        ),
        docs=_OLLAMA_LOCAL_DOCS,
    )


def run_doctor(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    report = CommandReport(command="doctor", ok=True, config_path=None)
    if config_path:
        report.config_path = str(config_path.resolve())

    agent_root = resolve_agent_root(cwd=cwd, config_path=config_path)
    layout = bootstrap_agent_layout(cwd=agent_root, config_path=config_path)
    _add_layout_checks(report, layout)
    if not args.json:
        print("layout:")
        print(f"  agent_root: {layout.agent_root}")
        print(f"  workspace: {layout.workspace_root}")
        print(f"  skills: {layout.skills_path}")
        print(f"  data: {layout.data_root}")

    memory_on = memory_enabled_from_config(str(config_path) if config_path else None)
    runtime = resolve_runtime_python(agent_root, memory_enabled=memory_on)

    py_version = _runtime_python_version(runtime)
    py_ok = py_version >= (3, 11)
    check(
        report,
        id="env.python.version",
        category="env",
        severity="error",
        passed=py_ok,
        message=f"Python {py_version[0]}.{py_version[1]} ({runtime.source})",
        value=f"{py_version[0]}.{py_version[1]}.{py_version[2]}",
        remediation=None if py_ok else "Install Python 3.11+ in the agent project environment",
    )

    harness_ok = run_probe(runtime, MEMORY_PROBE if memory_on else CORE_PROBE)
    check(
        report,
        id="env.harness.compatible",
        category="env",
        severity="error",
        passed=harness_ok,
        message=(
            f"MonkeyBot {COMPATIBLE_CORE_RANGE}"
            + (" with MemPalace" if memory_on else "")
            + (" ready" if harness_ok else " missing in gateway interpreter")
        ),
        remediation=None
        if harness_ok
        else (
            f"Pin monkeybot{'[memory]' if memory_on else ''}{COMPATIBLE_CORE_RANGE} "
            f"in {agent_root}/pyproject.toml, then "
            f"cd {agent_root} && uv sync"
            if (agent_root / "pyproject.toml").is_file()
            else (
                f"Install monkeybot{'[memory]' if memory_on else ''}"
                f"{COMPATIBLE_CORE_RANGE} in this environment"
                + (
                    ", run the agent once to provision a managed MemPalace runtime, "
                    "or set memory.enabled: false"
                    if memory_on and runtime.source in {"cli", MANAGED_RUNTIME_SOURCE}
                    else (", or set memory.enabled: false" if memory_on else "")
                )
            )
        ),
    )

    _, doc = load_config_doc(str(config_path) if config_path else None)
    model = doc.get("model") if isinstance(doc.get("model"), dict) else {}
    provider = str(model.get("provider", "gemini")) if isinstance(model, dict) else "gemini"
    spec = spec_for_provider(provider)

    if spec and spec.extra:
        installed = run_probe(
            runtime,
            f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({extra_module(spec.extra)!r}) else 1)",
        )
        check(
            report,
            id="provider.extra.installed",
            category="provider",
            severity="error",
            passed=installed,
            message=f"Extra '{spec.extra}' {'installed' if installed else 'missing'} in {runtime.source} env",
            field="model.provider",
            value=provider,
            remediation=_extra_remediation(spec.extra, agent_root, runtime),
        )
    else:
        check(
            report,
            id="provider.extra.installed",
            category="provider",
            severity="error",
            passed=True,
            skip=True,
        )

    if spec:
        creds = credentials_present(spec)
        check(
            report,
            id="provider.credentials.present",
            category="provider",
            severity="error",
            passed=creds,
            message="Provider credentials detected" if creds else "No provider credentials found",
            remediation="Set API keys or ADC in .env (see .env.example)",
        )
        if spec.gcp_adc:
            adc_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            adc_ok = bool(adc_path) and Path(adc_path).is_file()
            check(
                report,
                id="gcp.adc.valid",
                category="provider",
                severity="warning",
                passed=adc_ok or creds,
                message="GOOGLE_APPLICATION_CREDENTIALS set and file exists"
                if adc_ok
                else "ADC file not set (may use gcloud auth)",
                skip=not spec.gcp_adc,
            )
    else:
        check(
            report,
            id="provider.credentials.present",
            category="provider",
            severity="error",
            passed=False,
            skip=True,
        )

    thinking_raw = model.get("thinking_budget") if isinstance(model, dict) else None
    if thinking_raw is None:
        thinking_raw = "-1"
    num_ctx_raw = model.get("num_ctx") if isinstance(model, dict) else None
    model_name = str(model.get("name", "")).strip() if isinstance(model, dict) else ""
    _add_ollama_local_checks(
        report,
        provider=provider,
        model_name=model_name,
        thinking_budget=thinking_raw,
        num_ctx=num_ctx_raw,
    )

    runtime_cfg = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
    port = int(runtime_cfg.get("port", 8080)) if isinstance(runtime_cfg, dict) else 8080
    free = _port_free(port)
    check(
        report,
        id="runtime.port.free",
        category="runtime",
        severity="warning",
        passed=free,
        message=f"Port {port} {'available' if free else 'in use'}",
        value=port,
    )

    web = doc.get("web_search") if isinstance(doc.get("web_search"), dict) else {}
    backend = str(web.get("backend", "duckduckgo")) if isinstance(web, dict) else "duckduckgo"
    if backend == "none":
        check(
            report,
            id="web_search.backend.ready",
            category="tools",
            severity="warning",
            passed=True,
            skip=True,
        )
    elif backend == "duckduckgo":
        if run_probe(runtime, "import ddgs"):
            check(
                report,
                id="web_search.backend.ready",
                category="tools",
                severity="warning",
                passed=True,
                message="duckduckgo available",
                value=backend,
            )
        else:
            check(
                report,
                id="web_search.backend.ready",
                category="tools",
                severity="warning",
                passed=False,
                message="ddgs not installed",
                remediation=_extra_remediation("web-search", agent_root, runtime),
                value=backend,
            )
    else:
        key_var = "TAVILY_API_KEY" if backend == "tavily" else "FIRECRAWL_API_KEY"
        has_key = bool(os.environ.get(key_var, "").strip())
        check(
            report,
            id="web_search.backend.ready",
            category="tools",
            severity="warning",
            passed=has_key,
            message=f"{key_var} {'set' if has_key else 'missing'}",
            value=backend,
        )

    if args.check_mcp and config_path:
        paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
        mcp_rel = str(paths.get("mcp_config", "")) if isinstance(paths, dict) else ""
        if mcp_rel:
            base = (
                config_path.parent.parent
                if config_path.parent.name == "monkeybot_config"
                else Path.cwd()
            )
            mcp_path = Path(mcp_rel) if Path(mcp_rel).is_absolute() else (base / mcp_rel)
            if mcp_path.is_file():
                mcp_doc = json.loads(mcp_path.read_text(encoding="utf-8"))
                servers = mcp_doc.get("mcpServers", {})
                if isinstance(servers, dict):
                    for name, srv in servers.items():
                        if not isinstance(srv, dict):
                            continue
                        url = srv.get("url")
                        if isinstance(url, str) and url.startswith("http"):
                            try:
                                httpx.get(url, timeout=3.0)
                                ok = True
                                msg = f"{name} ok"
                            except Exception as exc:
                                ok = False
                                msg = str(exc)
                            check(
                                report,
                                id="mcp.server.reachable",
                                category="mcp",
                                severity="warning",
                                passed=ok,
                                message=msg,
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

    return report.emit(as_json=args.json)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("doctor", help="Check environment readiness for running the agent")
    p.add_argument("--json", action="store_true", help="Emit JSON report")
    p.add_argument("--config", help="Path to monkeybot.yaml")
    p.add_argument("--cwd", help="Working directory")
    p.add_argument("--check-mcp", action="store_true", help="Probe MCP HTTP servers")
    p.set_defaults(func=run_doctor)
