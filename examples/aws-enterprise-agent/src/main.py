"""Entry point for the AWS-enterprise reference deployment.

The code-spec (§8.2) writes the imports against the public distribution
names (``emonk.harness`` / ``emonk.gateway``). Inside this monorepo the
canonical module paths are ``src.core.harness`` and ``src.gateway`` — the
``pyproject.toml`` ``package-dir = {"emonk" = "src"}`` mapping renames them
to ``emonk.*`` at install time, but when running in-repo (``python -m
examples.aws-enterprise-agent.src.main``) we import from ``src.*``
directly. Production containers built from ``Dockerfile.aws-enterprise``
install the published ``emonk`` wheel so both paths resolve consistently.

Startup does three things:

    1. Load ``HARNESS_CONFIG`` (defaults to ``/app/harness.yaml``) with
       ``os.path.expandvars`` applied before YAML parsing so ``${VAR}``
       placeholders in ``harness.yaml`` are resolved from the process env.
    2. Build the :class:`CompiledAgent` via ``build_universal_agent``.
    3. Mount the FastAPI gateway (harness + agentcore routers), populate
       ``app.state`` slots so the control-plane endpoints are live, and
       run uvicorn on ``$PORT`` (defaults to ``8080``).

The AWS smoke endpoint (``/harness/aws/smoke``) is gated on
``HARNESS_ENABLE_AWS_SMOKE=1`` and an ``X-Admin-Token`` matching
``EMONK_ADMIN_TOKEN``.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI

from src.core.harness import HarnessConfig, build_universal_agent
from src.gateway.agentcore_routes import router as agentcore_router
from src.gateway.harness_aws_routes import router as harness_aws_router
from src.gateway.harness_identity_routes import router as harness_identity_router
from src.gateway.harness_routes import router as harness_router
from src.gateway.harness_secrets_routes import router as harness_secrets_router


def load_config(path: str | Path) -> HarnessConfig:
    """Return a :class:`HarnessConfig` loaded from ``path`` with env interpolation.

    The harness YAML contains ``${VAR}`` placeholders for runtime values
    (bucket names, DSNs, KMS key ids). We apply :func:`os.path.expandvars`
    to the raw text before handing it to :func:`yaml.safe_load` so the
    substitution happens before Pydantic validation runs.
    """
    raw = Path(path).read_text()
    expanded = os.path.expandvars(raw)
    data = yaml.safe_load(expanded)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level")
    return HarnessConfig.from_mapping(data)


def build_app() -> FastAPI:
    """Assemble the gateway app with all harness routers mounted.

    Reads ``HARNESS_CONFIG`` (path) from the environment and wires the
    resulting :class:`CompiledAgent` into ``app.state`` so the mounted
    routers can reach the session registry, run-package writer, approval
    channel, identity middleware, and secret resolver.
    """
    config_path = os.environ.get("HARNESS_CONFIG", "/app/harness.yaml")
    cfg = load_config(config_path)
    compiled = build_universal_agent(cfg)

    app = FastAPI(title=cfg.agent.name)
    app.include_router(harness_router)
    app.include_router(agentcore_router)
    app.include_router(harness_identity_router)
    app.include_router(harness_secrets_router)
    app.include_router(harness_aws_router)

    app.state.compiled_agent = compiled
    app.state.session_registry = compiled.session_registry
    app.state.run_package_writer = compiled.run_package_writer
    app.state.approval_channel = compiled.approval_channel
    app.state.secret_resolver = compiled.secret_resolver

    for mw in compiled.middleware:
        if type(mw).__name__ == "IdentityResolutionMW":
            app.state.identity_mw = mw
            break

    return app


def main() -> None:
    """Run the uvicorn server bound to ``0.0.0.0:$PORT`` (defaults to 8080)."""
    app = build_app()
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
