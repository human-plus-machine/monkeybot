"""monkeybot doctor — environment readiness checks."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

import httpx

from monkeybot_cli.config_resolve import load_agent_dotenv, load_config_doc, resolve_config
from monkeybot_cli.output import CommandReport, check
from monkeybot_cli.providers import credentials_present, extra_installed, spec_for_provider


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def run_doctor(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    report = CommandReport(command="doctor", ok=True, config_path=None)
    if config_path:
        report.config_path = str(config_path.resolve())

    py_ok = sys.version_info >= (3, 11)
    check(
        report,
        id="env.python.version",
        category="env",
        severity="error",
        passed=py_ok,
        message=f"Python {sys.version_info.major}.{sys.version_info.minor}",
        value=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )

    _, doc = load_config_doc(str(config_path) if config_path else None)
    model = doc.get("model") if isinstance(doc.get("model"), dict) else {}
    provider = str(model.get("provider", "gemini")) if isinstance(model, dict) else "gemini"
    spec = spec_for_provider(provider)

    if spec and spec.extra:
        installed = extra_installed(spec.extra)
        check(
            report,
            id="provider.extra.installed",
            category="provider",
            severity="error",
            passed=installed,
            message=f"Extra '{spec.extra}' {'installed' if installed else 'missing'}",
            field="model.provider",
            value=provider,
            remediation=f"Install: uv sync --extra {spec.extra}",
        )
    else:
        check(report, id="provider.extra.installed", category="provider", severity="error", passed=True, skip=True)

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
                message="GOOGLE_APPLICATION_CREDENTIALS set and file exists" if adc_ok else "ADC file not set (may use gcloud auth)",
                skip=not spec.gcp_adc,
            )
    else:
        check(report, id="provider.credentials.present", category="provider", severity="error", passed=False, skip=True)

    runtime = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
    port = int(runtime.get("port", 8080)) if isinstance(runtime, dict) else 8080
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
        check(report, id="web_search.backend.ready", category="tools", severity="warning", passed=True, skip=True)
    elif backend == "duckduckgo":
        try:
            import ddgs  # noqa: F401

            check(report, id="web_search.backend.ready", category="tools", severity="warning", passed=True, message="duckduckgo available", value=backend)
        except ImportError:
            check(
                report,
                id="web_search.backend.ready",
                category="tools",
                severity="warning",
                passed=False,
                message="ddgs not installed",
                remediation="uv sync --extra web-search on the harness package",
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
            base = config_path.parent.parent if config_path.parent.name == "monkeybot_config" else Path.cwd()
            mcp_path = Path(mcp_rel) if Path(mcp_rel).is_absolute() else (base / mcp_rel)
            if mcp_path.is_file():
                mcp_doc = json.loads(mcp_path.read_text(encoding="utf-8"))
                servers = mcp_doc.get("mcpServers", {})
                if isinstance(servers, dict):
                    for name, srv in servers.items():
                        if not isinstance(srv, dict) or srv.get("enabled") is False:
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
                            check(report, id="mcp.server.reachable", category="mcp", severity="warning", passed=ok, message=msg)
    else:
        check(report, id="mcp.server.reachable", category="mcp", severity="warning", passed=True, skip=True)

    return report.emit(as_json=args.json)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("doctor", help="Check environment readiness for running the agent")
    p.add_argument("--json", action="store_true", help="Emit JSON report")
    p.add_argument("--config", help="Path to monkeybot.yaml")
    p.add_argument("--cwd", help="Working directory")
    p.add_argument("--check-mcp", action="store_true", help="Probe MCP HTTP servers")
    p.set_defaults(func=run_doctor)
