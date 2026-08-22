"""Optional cold-start wiring for harness-as-library (Pattern B / C).

Callers who prefer explicit construction can ignore this module and wire
:class:`~monkeybot.core.persistence.backends.StorageBackend`,
:class:`~monkeybot.core.memory.subsystem.MemorySubsystem`, :class:`~monkeybot.core.mcp.mcp_client.MCPClient`,
and a :class:`~monkeybot.core.llm.provider.Provider` themselves.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from monkeybot.core.config.settings import auto_schema_enabled_from_config, get_provider_config
from monkeybot.core.context import build_context
from monkeybot.core.hooks import HookManager
from monkeybot.core.knowledge import KnowledgeSubsystem, resolve_knowledge_settings
from monkeybot.core.knowledge.config import knowledge_enabled_from_config
from monkeybot.core.llm.provider import Provider
from monkeybot.core.llm.usage import usage_from_totals
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.memory.subsystem import MemoryConfigurationError, MemorySubsystem
from monkeybot.core.persistence.backends import StorageBackend, create_storage_backend
from monkeybot.core.runtime.events import AssistantDelta, Error, TurnComplete
from monkeybot.core.runtime.loop import run as run_loop
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.inspector import load_command_tier_policy

logger = logging.getLogger(__name__)


class PatternBcTurnError(RuntimeError):
    """Raised when :func:`run_pattern_bc_turn` receives an :class:`~monkeybot.core.runtime.events.Error` event."""

    def __init__(self, message: str, *, request_id: str = "") -> None:
        super().__init__(message)
        self.request_id = request_id


@dataclass
class HarnessDeps:
    """Process-scoped dependencies for ``build_context`` + ``run_loop``."""

    storage: StorageBackend
    memory: MemorySubsystem | None
    mcp: MCPClient
    provider: Provider
    model: str
    knowledge: KnowledgeSubsystem | None = None

    async def close(self) -> None:
        """Close persisted storage and tear down MCP sessions.

        :class:`~monkeybot.core.workspace.protocol.WorkspaceStorage` (behind ``memory``)
        does not define ``aclose``/``close`` today; cloud SDK clients are process-scoped.
        Call explicit workspace teardown here if the protocol gains lifecycle hooks.
        """
        if self.knowledge is not None:
            await self.knowledge.close()
        if self.memory is not None:
            await self.memory.close()
        await self.storage.close()
        await self.mcp.disconnect_all()


def _resolve_run_command_allowlists() -> tuple[list[str] | None, list[str] | None]:
    """Load allowlists from ``COMMAND_ALLOWLIST_CONFIG`` when present; else executor defaults."""
    path_str = os.environ.get("COMMAND_ALLOWLIST_CONFIG", "").strip()
    if not path_str:
        return None, None
    path = Path(path_str)
    if not path.is_file():
        logger.info("command allowlist missing (%s); using CoreToolExecutor defaults", path)
        return None, None
    try:
        policy = load_command_tier_policy(path)
        return list(policy.allowed_commands), list(policy.allowed_path_prefixes)
    except Exception as exc:
        logger.warning("command allowlist load failed (%s): %s", path, exc)
        return None, None


def _env_context_window_tokens() -> int:
    cap_raw = os.environ.get("MODEL_CONTEXT_WINDOW", "200000").strip()
    try:
        return max(1, int(cap_raw))
    except ValueError:
        return 200_000


async def create_harness_deps(
    db_url: str,
    memory_storage_uri: str | None = None,
    *,
    mcp_config_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    open_mcp: bool = True,
    provider_override: Provider | None = None,
    _provider_override: Provider | None = None,
    workspace_root: Path | None = None,
    agent_scope: str | None = None,
) -> HarnessDeps:
    """Open storage, optional memory, MCP, and resolve the LLM provider.

    Args:
        db_url: Passed to :func:`~monkeybot.core.persistence.backends.create_storage_backend`.
        memory_storage_uri: When set, opens a per-agent MemPalace via
            :class:`~monkeybot.core.memory.subsystem.MemorySubsystem`.
        mcp_config_path: When ``open_mcp`` is True and this path is a file,
            :meth:`~monkeybot.core.mcp.mcp_client.MCPClient.load_from_config` is called.
        provider: ``MODEL_PROVIDER`` override for :func:`~monkeybot.core.config.settings.get_provider_config`.
        model: Model id override for :func:`~monkeybot.core.config.settings.get_provider_config`.
        open_mcp: When False, MCP is not loaded from disk (typical for Lambda).
        provider_override: Inject a custom :class:`~monkeybot.core.llm.provider.Provider` (skips config lookup).
        _provider_override: Deprecated alias for ``provider_override`` (tests).
        workspace_root: When set with knowledge enabled, constructs :class:`KnowledgeSubsystem`.
        agent_scope: Namespaces conversation history in ``db_url``, same as the gateway's
            resolved agent root (see :func:`~monkeybot.core.persistence.backends.create_storage_backend`).
            **Required** whenever more than one ``create_harness_deps`` caller (e.g. one Lambda
            handler serving several tenants, or several agent-platform deployments) can ever
            point at the same ``db_url`` — otherwise every embedded agent shares one global,
            unscoped conversation-history namespace and can read/resume each other's
            same-named sessions via ``list_threads``/``load``, the exact leak this scoping
            exists to close. Defaults to ``None``, which resolves ``MONKEYBOT_AGENT_ID`` from
            the process environment (empty string if unset) — the same env var
            ``AgentLayout.export_environment()`` sets for the gateway and its subagent workers,
            so an embedder that already exports it for that reason gets it here for free. Pass
            ``''`` explicitly only to deliberately opt out of scoping (single-tenant embedding
            with no shared ``db_url``); passing a concrete value always wins over the
            environment.
    """
    override = provider_override if provider_override is not None else _provider_override
    resolved_agent_scope = (
        os.environ.get("MONKEYBOT_AGENT_ID", "").strip() if agent_scope is None else agent_scope
    )
    backend = create_storage_backend(db_url, agent_scope=resolved_agent_scope)
    await backend.open(run_schema=auto_schema_enabled_from_config())
    try:
        if override is not None:
            prov = override
            model_str = str(model or "test-model")
        else:
            pc = get_provider_config(provider=provider, model_name=model)
            prov = pc.provider
            model_str = pc.model

        memory: MemorySubsystem | None = None
        uri = (memory_storage_uri or "").strip()
        if uri:
            from monkeybot.core.layout import AgentLayout
            from monkeybot.core.memory.config import memory_enabled_from_config

            if not memory_enabled_from_config():
                logger.info("memory disabled (memory.enabled=false)")
            else:
                try:
                    layout = AgentLayout.from_environment()
                    memory = MemorySubsystem(
                        memory_uri=uri,
                        db_url=db_url,
                        agent_id=layout.agent_root.name,
                        agent_name=layout.agent_root.name,
                        storage=backend,
                    )
                    await memory.ensure_ready()
                except MemoryConfigurationError:
                    raise
                except Exception as exc:
                    logger.warning("memory setup failed; continuing without: %r", exc)
                    memory = None

        knowledge: KnowledgeSubsystem | None = None
        if knowledge_enabled_from_config() and workspace_root is not None:
            try:
                settings = resolve_knowledge_settings(workspace_root=workspace_root)
                knowledge = await KnowledgeSubsystem.create(
                    workspace_root=workspace_root,
                    settings=settings,
                    knowledge_root=Path(settings.knowledge_root),
                    index_path=Path(settings.index_path),
                    read_only=os.environ.get("MONKEYBOT_KNOWLEDGE_READ_ONLY", "").strip().lower()
                    in ("1", "true", "yes"),
                )
                await knowledge.ensure_ready()
            except Exception as exc:
                logger.warning("knowledge layer setup failed; continuing without: %r", exc)
                knowledge = None

        mcp = MCPClient()
        if open_mcp and mcp_config_path is not None:
            strict = os.environ.get("MCP_STRICT_LOAD", "").strip().lower() in ("1", "true", "yes")
            await mcp.load_from_config(mcp_config_path, raise_on_error=strict)

        return HarnessDeps(
            storage=backend,
            memory=memory,
            knowledge=knowledge,
            mcp=mcp,
            provider=prov,
            model=model_str,
        )
    except BaseException:
        await backend.close()
        raise


async def run_pattern_bc_turn(
    deps: HarnessDeps,
    message: str,
    *,
    session_id: str,
    request_id: str,
    agent_md_path: Path,
    skills_path: Path,
    workspace_root: Path,
    artifacts_path: Path | None = None,
    hook_manager: HookManager | None = None,
) -> str:
    """Run one user message through :func:`~monkeybot.core.context.build_context` + :func:`~monkeybot.core.runtime.loop.run`.

    When ``hook_manager`` is ``None`` (typical for Lambda / short-lived handlers), the loop
    does not fire :class:`~monkeybot.core.hooks.HookManager` hooks, so :class:`~monkeybot.core.memory.hook.MemoryHook`
    callbacks (automatic recall / writer drain) never run.
    ``deps.memory`` still powers MemPalace wake-up. To enable hook-driven
    memory in FaaS, construct a ``HookManager``, ``memory.register_hooks(mgr)``, pass ``mgr``,
    and use positive hook timeouts where needed so work finishes before the handler returns.

    Returns:
        Concatenated assistant text from :class:`~monkeybot.core.runtime.events.AssistantDelta` events.

    Raises:
        PatternBcTurnError: When the loop emits an :class:`~monkeybot.core.runtime.events.Error` event.
    """
    ctx = await build_context(
        session_id,
        request_id,
        agent_md_path=agent_md_path,
        memory=deps.memory,
        skills_path=skills_path,
        mcp_client=deps.mcp,
        model=deps.model,
        include_task_tool=False,
        sse_bus=None,
        workspace_root=workspace_root,
        context_window_tokens=_env_context_window_tokens(),
    )

    run_cmds, run_paths = _resolve_run_command_allowlists()
    executor = CoreToolExecutor(
        workspace_root=workspace_root,
        memory=deps.memory,
        knowledge=deps.knowledge,
        skills_path=skills_path,
        artifacts_path=artifacts_path,
        mcp=deps.mcp,
        run_command_allowed_commands=run_cmds,
        run_command_allowed_path_prefixes=run_paths,
    )

    parts: list[str] = []
    try:
        async for evt in run_loop(
            message,
            ctx,
            provider=deps.provider,
            history=deps.storage.history(),
            inspectors=[],
            tool_executor=executor,
            hook_manager=hook_manager,
        ):
            if isinstance(evt, Error):
                logger.error(
                    "run_pattern_bc_turn failed request_id=%s: %s",
                    evt.request_id or request_id,
                    evt.error,
                )
                raise PatternBcTurnError(evt.error, request_id=evt.request_id or request_id)
            if isinstance(evt, AssistantDelta) and evt.delta:
                parts.append(evt.delta)
            if isinstance(evt, TurnComplete):
                await deps.storage.usage().record(
                    session_id,
                    deps.model,
                    usage_from_totals(evt.usage),
                    run_id=request_id,
                )
    finally:
        await executor.aclose()
        if deps.memory is not None:
            await deps.memory.drain_writer()

    return "".join(parts)


__all__ = [
    "HarnessDeps",
    "PatternBcTurnError",
    "create_harness_deps",
    "run_pattern_bc_turn",
]
