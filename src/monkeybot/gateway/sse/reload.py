"""Config hot-reload: turn-boundary lock, admin handlers, apply orchestration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from monkeybot.core.config.runtime_env import ENV_TIERS, ConfigTier
from monkeybot.core.config.snapshot import (
    ConfigDiff,
    RuntimeConfig,
    apply_reload_env_patch,
    get_config_store,
    load_into_store,
)
from monkeybot.core.context.tool_output_policy import invalidate_config_caches
from monkeybot.core.logging_utils import kv
from monkeybot.core.mcp.mcp_client import MCPCatalogApplyResult
from monkeybot.core.runtime.events import ConfigReloaded, event_to_json
from monkeybot.gateway.sse.models import APIError
from monkeybot.gateway.sse.session_bus import SessionRegistry

logger = logging.getLogger(__name__)

_reload_lock = asyncio.Lock()


def get_reload_lock() -> asyncio.Lock:
    """Lock shared by ``start_turn`` pin and ``POST /admin/config/reload``."""
    return _reload_lock


class ReloadRequest(BaseModel):
    """POST /admin/config/reload body."""

    env: dict[str, str] | None = None


class MCPReloadResponse(BaseModel):
    reconnected: list[str] = Field(default_factory=list)
    kept: list[str] = Field(default_factory=list)


class ConfigReloadResponse(BaseModel):
    revision: int
    digest: str
    changed: list[str] = Field(default_factory=list)
    applied: list[str] = Field(default_factory=list)
    hot: list[str] = Field(default_factory=list)
    restart_required: list[str] = Field(default_factory=list)
    mcp: MCPReloadResponse = Field(default_factory=MCPReloadResponse)
    error: str | None = None


class AdminConfigResponse(BaseModel):
    revision: int
    digest: str
    source_path: str | None = None
    loaded_at: float = 0.0
    env: dict[str, str] = Field(default_factory=dict)
    subagents: list[str] = Field(default_factory=list)
    content: dict[str, str | None] = Field(default_factory=dict)


def _keys_for_tier(changed: frozenset[str], tier: ConfigTier) -> list[str]:
    return sorted(k for k in changed if ENV_TIERS.get(k) == tier)


def _response_from_diff(
    cfg: RuntimeConfig,
    diff: ConfigDiff,
    *,
    applied: list[str],
    mcp: MCPCatalogApplyResult,
    error: str | None = None,
) -> ConfigReloadResponse:
    changed = frozenset(diff.changed_env_keys)
    return ConfigReloadResponse(
        revision=cfg.revision,
        digest=cfg.digest,
        changed=sorted(changed),
        applied=list(applied),
        hot=_keys_for_tier(changed, ConfigTier.HOT),
        restart_required=_keys_for_tier(changed, ConfigTier.RESTART),
        mcp=MCPReloadResponse(
            reconnected=list(mcp.reconnected),
            kept=list(mcp.kept),
        ),
        error=error,
    )


def _redacted_config(cfg: RuntimeConfig) -> AdminConfigResponse:
    return AdminConfigResponse(
        revision=cfg.revision,
        digest=cfg.digest,
        source_path=str(cfg.source_path) if cfg.source_path is not None else None,
        loaded_at=cfg.loaded_at,
        env=dict(cfg.env_values),
        subagents=sorted(cfg.subagents),
        content={
            "agent_md": cfg.paths.agent_md_digest,
            "skills": cfg.paths.skills_digest,
            "mcp_config": cfg.paths.mcp_config_digest,
            "command_allowlist": cfg.paths.command_allowlist_digest,
            "permission_config": cfg.paths.permission_config_digest,
        },
    )


def _gateway_runtime(fastapi_app: Any | None) -> Any | None:
    if fastapi_app is None:
        return None
    return getattr(getattr(fastapi_app, "state", None), "gateway_runtime", None)


async def _publish_reloaded(registry: SessionRegistry, report: ConfigReloadResponse) -> None:
    event = ConfigReloaded(
        revision=report.revision,
        digest=report.digest,
        hot=list(report.hot),
        applied=list(report.applied),
        restart_required=list(report.restart_required),
    )
    payload = event_to_json(event)
    for bus in registry.iter_buses():
        try:
            await bus.publish_data(payload)
        except Exception:
            logger.warning(
                "ConfigReloaded publish failed %s",
                kv(revision=report.revision, digest=report.digest),
                exc_info=True,
            )


def _log_reload(report: ConfigReloadResponse, *, noop: bool) -> None:
    logger.info(
        "config reload noop %s" if noop else "config reload applied %s",
        kv(
            revision=report.revision,
            digest=report.digest,
            changed=",".join(report.changed),
            restart_required=",".join(report.restart_required),
        ),
    )


async def run_config_reload(
    *,
    registry: SessionRegistry,
    fastapi_app: Any | None,
    env: Mapping[str, str] | None = None,
) -> ConfigReloadResponse:
    """Take the turn-boundary lock, rebuild the snapshot, apply live slices."""
    async with _reload_lock:
        if env:
            apply_reload_env_patch(env)
        store = get_config_store()
        if store.current_or_none() is None:
            load_into_store()
        cfg, diff = store.reload()
        if diff.noop:
            report = ConfigReloadResponse(revision=cfg.revision, digest=cfg.digest)
            _log_reload(report, noop=True)
            return report

        needs_apply = bool(diff.tiers & {ConfigTier.REBUILD, ConfigTier.RECONNECT_MCP})
        runtime = _gateway_runtime(fastapi_app)
        if needs_apply and runtime is None:
            logger.error(
                "config reload runtime not bound %s",
                kv(revision=cfg.revision, digest=cfg.digest),
            )
            raise APIError(
                503,
                "GATEWAY_RUNTIME_NOT_BOUND",
                "Gateway runtime is not bound",
                uuid.uuid4().hex,
            )

        invalidate_config_caches()
        applied: list[str] = []
        mcp_result = MCPCatalogApplyResult()
        error: str | None = None
        if runtime is not None and needs_apply:
            slice_result = await runtime.apply(
                cfg, diff, fastapi_app=fastapi_app, registry=registry
            )
            applied = list(slice_result.applied)
            mcp_result = slice_result.mcp
            error = slice_result.error
        report = _response_from_diff(
            cfg, diff, applied=applied, mcp=mcp_result, error=error
        )
        await _publish_reloaded(registry, report)
        _log_reload(report, noop=False)
        return report


def build_admin_router() -> APIRouter:
    """127.0.0.1 admin config surface (no session auth; same as ``/health``)."""
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/config", response_model=AdminConfigResponse)
    async def get_config() -> AdminConfigResponse:
        cfg = get_config_store().current_or_none()
        if cfg is None:
            raise APIError(
                503,
                "CONFIG_NOT_LOADED",
                "RuntimeConfig has not been loaded",
                uuid.uuid4().hex,
            )
        return _redacted_config(cfg)

    @router.post("/config/reload", response_model=ConfigReloadResponse)
    async def post_reload(
        request: Request, body: ReloadRequest | None = None
    ) -> ConfigReloadResponse:
        payload = body or ReloadRequest()
        registry = request.app.state.registry
        return await run_config_reload(
            registry=registry,
            fastapi_app=request.app,
            env=payload.env,
        )

    return router
