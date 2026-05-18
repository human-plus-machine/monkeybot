"""Optional cold-start wiring for harness-as-library (Pattern B / C).

Callers who prefer explicit construction can ignore this module and wire
:class:`~monkeybot.core.persistence.backends.StorageBackend`,
:class:`~monkeybot.core.memory.subsystem.MemorySubsystem`, :class:`~monkeybot.core.mcp.mcp_client.MCPClient`,
and a :class:`~monkeybot.core.llm.provider.Provider` themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from monkeybot.core.config.settings import get_provider_config
from monkeybot.core.context import build_context
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import Provider
from monkeybot.core.llm.usage import usage_from_totals
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import StorageBackend, create_storage_backend
from monkeybot.core.runtime.events import AssistantDelta, TurnComplete
from monkeybot.core.runtime.loop import run as run_loop
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.workspace import create_workspace_storage


@dataclass
class HarnessDeps:
    """Process-scoped dependencies for ``build_context`` + ``run_loop``."""

    storage: StorageBackend
    memory: MemorySubsystem | None
    mcp: MCPClient
    provider: Provider
    model: str

    async def close(self) -> None:
        """Close persisted storage and tear down MCP sessions.

        :class:`~monkeybot.core.workspace.protocol.WorkspaceStorage` (behind ``memory``)
        does not define ``aclose``/``close`` today; cloud SDK clients are process-scoped.
        Call explicit workspace teardown here if the protocol gains lifecycle hooks.
        """
        await self.storage.close()
        await self.mcp.disconnect_all()


async def create_harness_deps(
    db_url: str,
    memory_storage_uri: str | None = None,
    *,
    mcp_config_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    open_mcp: bool = True,
    _provider_override: Provider | None = None,
) -> HarnessDeps:
    """Open storage, optional memory, MCP, and resolve the LLM provider.

    Args:
        db_url: Passed to :func:`~monkeybot.core.persistence.backends.create_storage_backend`.
        memory_storage_uri: When set, passed to :func:`~monkeybot.core.workspace.create_workspace_storage`
            and wrapped in :class:`~monkeybot.core.memory.subsystem.MemorySubsystem`.
        mcp_config_path: When ``open_mcp`` is True and this path is a file,
            :meth:`~monkeybot.core.mcp.mcp_client.MCPClient.load_from_config` is called.
        provider: ``MODEL_PROVIDER`` override for :func:`~monkeybot.core.config.settings.get_provider_config`.
        model: Model id override for :func:`~monkeybot.core.config.settings.get_provider_config`.
        open_mcp: When False, MCP is not loaded from disk (typical for Lambda).
        _provider_override: Test-only injection of a fake provider; skips ``get_provider_config``.
    """
    backend = create_storage_backend(db_url)
    await backend.open()
    try:
        if _provider_override is not None:
            prov = _provider_override
            model_str = str(model or "test-model")
        else:
            pc = get_provider_config(provider=provider, model_name=model)
            prov = pc.provider
            model_str = pc.model

        memory: MemorySubsystem | None = None
        uri = (memory_storage_uri or "").strip()
        if uri:
            ws = create_workspace_storage(uri)
            memory = MemorySubsystem(
                storage=ws,
                provider=prov,
                model=model_str,
                memory_uri=uri,
            )

        mcp = MCPClient()
        if open_mcp and mcp_config_path is not None:
            await mcp.load_from_config(mcp_config_path)

        return HarnessDeps(
            storage=backend,
            memory=memory,
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
    hook_manager: HookManager | None = None,
) -> str:
    """Run one user message through :func:`~monkeybot.core.context.build_context` + :func:`~monkeybot.core.runtime.loop.run`.

    When ``hook_manager`` is ``None`` (typical for Lambda / short-lived handlers), the loop
    does not fire :class:`~monkeybot.core.hooks.HookManager` hooks, so :class:`~monkeybot.core.memory.hook.MemoryHook`
    callbacks (automatic ``POST_TOOL`` / ``POST_TURN`` capture, organizer scheduling) never run.
    ``deps.memory`` still powers the memory index, ``search_memory``, etc. To enable hook-driven
    memory in FaaS, construct a ``HookManager``, ``memory.register_hooks(mgr)``, pass ``mgr``,
    and use positive hook timeouts where needed so work finishes before the handler returns.

    Returns:
        Concatenated assistant text from :class:`~monkeybot.core.runtime.events.AssistantDelta` events.
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
    )

    executor = CoreToolExecutor(
        workspace_root=workspace_root,
        memory=deps.memory,
        skills_path=skills_path,
        mcp=deps.mcp,
        run_command_allowed_commands=(),
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
            await deps.memory.flush()

    return "".join(parts)


__all__ = ["HarnessDeps", "create_harness_deps", "run_pattern_bc_turn"]
