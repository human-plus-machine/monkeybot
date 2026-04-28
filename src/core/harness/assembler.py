"""build_universal_agent — the assembler that wires HarnessConfig into a CompiledAgent.

Frozen middleware order (from Phase 1B §4):
    [Principal, Rules, Redaction(in), ContextPolicy, SubagentRecursion, Observability(pre),
     CommandTier, HITL, ToolOutputOffload, Redaction(out), Recovery, Observability(post)]
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

# BEGIN harness-extensibility story 2
# DynamoDBCheckpointerStub removed: Story 9 ships the worked DynamoDB example
# via the new ABC. The assembler now raises HarnessConfigError when the legacy
# ``scheduler.storage == "dynamodb"`` path is requested.
from .checkpointer import CheckpointerBackend, FirestoreCheckpointer, InMemoryCheckpointer

# END harness-extensibility story 2
from .compiled_agent import CompiledAgent
from .control import SessionRegistry
from .errors import HarnessConfigError
from .event_bus import EventBus
from .events import VersionTriple
from .hitl.google_chat import GoogleChatApprovalChannel
from .hitl.protocol import ApprovalChannel
from .hitl.webhook import WebhookApprovalChannel
from .identity import IdentityLoader, LoadedIdentity
from .middleware.command_tier import CommandTierMW
from .middleware.context_policy import ContextPolicyMW
from .middleware.hitl import HITLApprovalMW

# BEGIN harness-extensibility story 5
from .middleware.identity_resolution import IdentityResolutionMW

# END harness-extensibility story 5
from .middleware.observability import ObservabilityMW
from .middleware.principal_propagation import PrincipalPropagationMW
from .middleware.recovery import RecoveryMW
from .middleware.redaction import RedactionMW
from .middleware.rules import RulesEnforcementMW
from .middleware.subagent_recursion import SubagentRecursionMW
from .middleware.tool_output_offload import ToolOutputOffloadMW
from .redaction import Redactor
from .runpackage_writers import (
    DisabledRunPackageWriter,
    GCSRunPackageWriter,
    LocalRunPackageWriter,
    RunPackageWriter,
    S3RunPackageWriter,
)
from .sandbox.local_shell import LocalShellSandbox
from .sandbox.modal_backend import ModalSandbox
from .sandbox.policy import Policy
from .sandbox.protocol import SandboxBackend
from .specs import HarnessConfig
from .versioning import HARNESS_SCHEMA_VERSION

log = logging.getLogger("emonk.harness.assembler")


def _build_sandbox(
    cfg: HarnessConfig,
    # BEGIN harness-extensibility story 6
    *,
    resolver: Any | None = None,
    # END harness-extensibility story 6
) -> SandboxBackend:
    if cfg.sandbox.backend == "local_shell":
        # BEGIN harness-extensibility story 6
        return LocalShellSandbox(resolver=resolver)
        # END harness-extensibility story 6
    if cfg.sandbox.backend == "modal":
        # BEGIN harness-extensibility story 6
        return ModalSandbox(resolver=resolver)
        # END harness-extensibility story 6
    if cfg.sandbox.backend == "custom":
        path = cfg.sandbox.custom_import_path
        if not path or ":" not in path:
            raise HarnessConfigError(f"invalid custom_import_path: {path!r}")
        module_name, attr = path.split(":", 1)
        mod = importlib.import_module(module_name)
        factory = getattr(mod, attr)
        return factory()
    raise HarnessConfigError(f"unknown sandbox backend: {cfg.sandbox.backend}")


def _capability_gate(cfg: HarnessConfig, sandbox: SandboxBackend) -> None:
    caps = sandbox.capabilities()
    if cfg.sandbox.policy.net_deny and not caps.network_egress_control and cfg.sandbox.backend == "local_shell":
        log.warning(
            "local_shell does not enforce network egress; net_deny is best-effort. "
            "Use modal or a custom sandbox for SB-3 compliance."
        )


def _build_approval_channel(cfg: HarnessConfig) -> ApprovalChannel:
    if cfg.hitl.mode == "disabled":
        class _NoopChannel:
            name = "disabled"

            async def request(self, req):
                from .hitl.protocol import ApprovalDecision

                return ApprovalDecision(approval_id=req.approval_id, decision="approved")

        return _NoopChannel()  # type: ignore[return-value]
    if cfg.hitl.channel == "google_chat":
        return GoogleChatApprovalChannel(timeout_seconds=cfg.hitl.approval_timeout_seconds)
    if cfg.hitl.channel == "webhook":
        assert cfg.hitl.webhook_url  # validated by HITLSpec
        return WebhookApprovalChannel(url=cfg.hitl.webhook_url, timeout_seconds=cfg.hitl.approval_timeout_seconds)
    raise HarnessConfigError(f"unknown HITL channel: {cfg.hitl.channel}")


def _build_run_package_writer(cfg: HarnessConfig) -> RunPackageWriter:
    spec = cfg.observability.run_package
    if cfg.observability.event_bus == "disabled" or spec.writer == "disabled":
        return DisabledRunPackageWriter()
    if spec.writer == "local":
        return LocalRunPackageWriter(sink_uri=spec.sink_uri)
    if spec.writer == "gcs":
        return GCSRunPackageWriter(sink_uri=spec.sink_uri)
    if spec.writer == "s3":
        return S3RunPackageWriter(sink_uri=spec.sink_uri)
    raise HarnessConfigError(f"unknown run_package writer: {spec.writer}")


def _default_checkpointer(cfg: HarnessConfig) -> CheckpointerBackend:
    if cfg.scheduler.storage == "firestore":
        return FirestoreCheckpointer()
    # BEGIN harness-extensibility story 2
    if cfg.scheduler.storage == "dynamodb":
        raise HarnessConfigError(
            "DynamoDBCheckpointerStub was removed in harness-extensibility story 2. "
            "Use a PostgresCheckpointer via HarnessConfig.checkpointer, or ship a custom "
            "DynamoDB backend by subclassing emonk.core.harness.extensions.Checkpointer "
            "(see docs/extending-the-harness.md)."
        )
    # END harness-extensibility story 2
    return InMemoryCheckpointer()


def _build_model(cfg: HarnessConfig) -> BaseChatModel | str:
    """Return either an instantiated BaseChatModel or a string that deep_agents
    will resolve on its own (this preserves compatibility with existing tests)."""
    return cfg.agent.model


def _materialize_tools(cfg: HarnessConfig) -> list[BaseTool]:
    tools: list[BaseTool] = []
    for tspec in cfg.tools:
        mod_name, attr = tspec.import_path.split(":", 1)
        mod = importlib.import_module(mod_name)
        tool = getattr(mod, attr)
        if callable(tool) and not isinstance(tool, BaseTool):
            tool = tool()
        tools.append(tool)
    return tools


def _load_identity(cfg: HarnessConfig) -> LoadedIdentity:
    try:
        return IdentityLoader(cfg.identity).load()
    except Exception as exc:
        if cfg.identity.enforce_rules:
            raise
        log.warning("identity load failed (enforce_rules=False): %s", exc)
        return LoadedIdentity()


# BEGIN harness-extensibility story 6
def _build_secret_resolver(cfg: HarnessConfig, event_bus: EventBus) -> Any | None:
    """Resolve ``cfg.secret_resolver`` into a :class:`SecretResolver` instance.

    Returns ``None`` when no resolver is configured. When configured, the
    user-selected backend is wrapped in :class:`TracingResolver` so every
    successful resolution emits a ``secret.resolved`` audit event
    automatically.
    """
    spec = getattr(cfg, "secret_resolver", None)
    if spec is None:
        return None
    from .extensions import SecretResolver
    from .extensions import secret_resolvers as _secret_resolvers  # noqa: F401
    from .extensions.secret_resolvers._resolve_tracer import TracingResolver

    payload = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
    inner = SecretResolver.registry.resolve(payload)
    return TracingResolver(inner, event_bus=event_bus)
# END harness-extensibility story 6


# BEGIN harness-extensibility story 7
def _synthesize_model_provider_spec(cfg: HarnessConfig) -> Any:
    """Return ``cfg.model_provider`` or synthesize one from legacy ``agent.provider``.

    Implements the 1B §4.3 backward-compat shim: when ``HarnessConfig.model_provider``
    is ``None``, the assembler synthesizes a :class:`ModelProviderSpec` from the
    legacy ``AgentSpec.provider`` literal so existing configs keep working without
    a rewrite. ``agent.model`` is carried over into Bedrock's ``model_id`` since
    Bedrock requires an explicit model identifier.
    """
    from .extensions.specs.model_provider import (
        ModelProviderAnthropicSpec,
        ModelProviderBedrockSpec,
        ModelProviderOpenAISpec,
        ModelProviderVertexSpec,
    )

    spec = getattr(cfg, "model_provider", None)
    if spec is not None:
        return spec
    provider = cfg.agent.provider
    if provider == "openai":
        return ModelProviderOpenAISpec()
    if provider == "anthropic":
        return ModelProviderAnthropicSpec()
    if provider == "bedrock":
        return ModelProviderBedrockSpec(model_id=cfg.agent.model)
    return ModelProviderVertexSpec()


def _build_model_provider(cfg: HarnessConfig) -> Any | None:
    """Resolve a :class:`ModelProvider` instance from ``cfg``.

    Returns ``None`` when the registry cannot resolve the synthesized spec
    (e.g. an optional LLM SDK extra is not installed). The caller is expected
    to fall through to the legacy inline model-string path in that case.
    """
    from .extensions import model_providers as _mp_builtins  # noqa: F401 - register builtins
    from .extensions.base import ModelProvider

    spec = _synthesize_model_provider_spec(cfg)
    payload = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
    try:
        return ModelProvider.registry.resolve(payload)
    except Exception as exc:  # noqa: BLE001 - logged + fall through
        log.warning(
            "ModelProvider resolution failed for backend=%s: %s; "
            "falling back to legacy model-string path",
            payload.get("backend"),
            exc,
        )
        return None
# END harness-extensibility story 7


# BEGIN harness-extensibility phase 6
def _build_memory_store(cfg: HarnessConfig) -> Any | None:
    """Resolve ``cfg.memory_store`` into a :class:`MemoryStore` instance.

    Returns ``None`` when no spec is configured. Mirrors the resolution
    pattern used by every other extension surface (dump spec → registry
    resolve). Importing the ``memory_stores`` package is sufficient to
    register the builtin backends.
    """
    spec = getattr(cfg, "memory_store", None)
    if spec is None:
        return None
    from .extensions import MemoryStore
    from .extensions import memory_stores as _memory_stores  # noqa: F401

    payload = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
    # `require_vector_search` is harness-level validation metadata (1b §4.2),
    # not a backend constructor kwarg. Strip it so builtin factories don't
    # choke on an otherwise-unused keyword.
    payload.pop("require_vector_search", None)
    try:
        return MemoryStore.registry.resolve(payload)
    except Exception as exc:  # noqa: BLE001 - logged + fall through
        log.warning("MemoryStore resolution failed: %s", exc)
        return None


def _build_langgraph_store(memory_store: Any | None) -> Any | None:
    """Adapt a harness MemoryStore for DeepAgents/LangGraph."""
    if memory_store is None:
        return None
    return memory_store.as_langgraph_store()


def _build_job_storage(cfg: HarnessConfig) -> Any | None:
    """Resolve ``cfg.job_storage`` into a :class:`JobStorage` instance."""
    spec = getattr(cfg, "job_storage", None)
    if spec is None:
        return None
    from .extensions import JobStorage
    from .extensions import job_storage as _job_storage  # noqa: F401

    payload = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
    try:
        return JobStorage.registry.resolve(payload)
    except Exception as exc:  # noqa: BLE001 - logged + fall through
        log.warning("JobStorage resolution failed: %s", exc)
        return None


def _build_extensions_checkpointer(cfg: HarnessConfig) -> Any | None:
    """Resolve ``cfg.checkpointer`` into an ABC-based :class:`Checkpointer`.

    Returns ``None`` when no spec is configured. Does NOT replace the legacy
    ``_default_checkpointer`` path — the ABC-based instance is surfaced on
    :attr:`CompiledAgent.checkpointer_ext` while the legacy Protocol-based
    instance continues to drive ``session_registry`` for zero-change bots.
    """
    spec = getattr(cfg, "checkpointer", None)
    if spec is None:
        return None
    from .extensions import Checkpointer
    from .extensions import checkpointers as _checkpointers  # noqa: F401

    payload = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
    try:
        return Checkpointer.registry.resolve(payload)
    except Exception as exc:  # noqa: BLE001 - logged + fall through
        log.warning("Checkpointer (ABC) resolution failed: %s", exc)
        return None
# END harness-extensibility phase 6


# BEGIN harness-extensibility story 5
def _build_identity_source(cfg: HarnessConfig) -> Any | None:
    """Resolve ``cfg.identity_source`` into an :class:`IdentitySource` instance.

    Returns ``None`` when no source is configured — the assembler then
    omits :class:`IdentityResolutionMW` from the pipeline entirely.

    **Phase 6 decision (2026-04-17): the assembler does NOT auto-synthesize
    an :class:`LocalFSIdentitySource` from ``cfg.identity`` when
    ``cfg.identity_source`` is omitted.** The zero-change regression gate
    (``tests/e2e/test_zero_change_bot_configs.py``) asserts that existing
    marketing-bot / coding-bot configs compile with the same middleware
    order they had pre-Story-5. Auto-synthesis would inject a new middleware
    into that pipeline and break the invariant. Consumers wanting the new
    per-invocation identity plumbing opt in by adding an explicit
    ``identity_source:`` stanza to their ``harness.yaml`` — see
    ``docs/harness/identity-source.md``.

    ``allow_cross_principal`` is spec-only metadata (consumed by future
    policy middleware) and is filtered out before the factory is invoked
    so backends don't have to accept an otherwise-unused kwarg.
    """
    spec = getattr(cfg, "identity_source", None)
    if spec is None:
        return None
    from .extensions import IdentitySource
    from .extensions import identity_sources as _identity_sources  # noqa: F401

    payload = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)
    payload.pop("allow_cross_principal", None)
    return IdentitySource.registry.resolve(payload)
# END harness-extensibility story 5


def _build_middleware_stack(
    cfg: HarnessConfig,
    *,
    identity: LoadedIdentity,
    event_bus: EventBus,
    approval_channel: ApprovalChannel,
    versions: VersionTriple,
    identity_source: Any | None = None,
) -> list[Any]:
    redactor = Redactor(cfg.security.redaction_patterns)
    stack: list[Any] = [
        PrincipalPropagationMW(principal_required=cfg.security.principal_required),
        RulesEnforcementMW(identity=identity, event_bus=event_bus),
        RedactionMW(redactor=redactor, direction="in"),
        ContextPolicyMW(
            spec=cfg.context,
            event_bus=event_bus,
            summarizer=None,
            model_name=cfg.agent.model,
        ),
        SubagentRecursionMW(
            depth_limit=max([s.recursion_depth_limit for s in cfg.subagents], default=3)
        ),
        ObservabilityMW(event_bus=event_bus, versions=versions),
        CommandTierMW(cfg.sandbox.command_tiers),
        HITLApprovalMW(
            channel=approval_channel,
            event_bus=event_bus,
            timeout_seconds=cfg.hitl.approval_timeout_seconds,
        ),
        ToolOutputOffloadMW(
            threshold_tokens=cfg.context.tool_output_offload_threshold,
            event_bus=event_bus,
            model_name=cfg.agent.model,
        ),
        RedactionMW(redactor=redactor, direction="out"),
        RecoveryMW(spec=cfg.autonomy),
        ObservabilityMW(event_bus=event_bus, versions=versions),
    ]
    # BEGIN harness-extensibility story 5
    if identity_source is not None:
        stack.insert(
            1,
            IdentityResolutionMW(
                identity_source,
                cache_size=1024,
                default_ttl_seconds=max(
                    1, int(getattr(cfg.identity_source, "cache_ttl_seconds", 300) or 300)
                ),
                # Phase 6 integration: IDENTITY_* events flow through the bus
                event_bus=event_bus,
                versions=versions,
            ),
        )
    # END harness-extensibility story 5
    return stack


def build_universal_agent(
    harness: HarnessConfig,
    *,
    model: BaseChatModel | str | None = None,
    extra_tools: Sequence[BaseTool] | None = None,
    extra_middleware: Sequence[Any] | None = None,
    event_handlers: Sequence[Any] | None = None,
    approval_channel: ApprovalChannel | None = None,
    sandbox: SandboxBackend | None = None,
    checkpointer: CheckpointerBackend | None = None,
    run_package_writer: RunPackageWriter | None = None,
) -> CompiledAgent:
    """Assemble the Agent Harness for a given HarnessConfig.

    All sub-components accept an override so consumers can inject their own
    instances in tests. By default everything is built from ``harness``.
    """
    identity = _load_identity(harness)

    approval_channel = approval_channel or _build_approval_channel(harness)
    writer = run_package_writer or _build_run_package_writer(harness)
    ckpt = checkpointer or _default_checkpointer(harness)

    event_bus = EventBus(
        include_default_logger=harness.observability.event_bus != "disabled",
    )
    for h in event_handlers or []:
        event_bus.subscribe(h)

    # BEGIN harness-extensibility story 6
    secret_resolver = _build_secret_resolver(harness, event_bus)
    # END harness-extensibility story 6

    # BEGIN harness-extensibility story 6
    sandbox = sandbox or _build_sandbox(harness, resolver=secret_resolver)
    # END harness-extensibility story 6
    _capability_gate(harness, sandbox)

    versions = VersionTriple(
        harness=HARNESS_SCHEMA_VERSION,
        deep_agents=_deep_agents_version(),
        model=harness.agent.model,
    )

    # BEGIN harness-extensibility story 5
    identity_source = _build_identity_source(harness)
    # END harness-extensibility story 5

    # BEGIN harness-extensibility phase 6
    memory_store = _build_memory_store(harness)
    langgraph_store = _build_langgraph_store(memory_store)
    job_storage = _build_job_storage(harness)
    checkpointer_ext = _build_extensions_checkpointer(harness)
    # END harness-extensibility phase 6

    # BEGIN harness-extensibility story 7
    model_provider = _build_model_provider(harness)
    if (
        model is None
        and getattr(harness, "model_provider", None) is not None
        and model_provider is not None
    ):
        try:
            model = model_provider.build(harness.agent)
        except Exception as exc:  # noqa: BLE001 - logged + fall through
            log.warning(
                "ModelProvider.build failed (%s); falling back to legacy model-string path",
                exc,
            )
    # END harness-extensibility story 7
    middleware = _build_middleware_stack(
        harness,
        identity=identity,
        event_bus=event_bus,
        approval_channel=approval_channel,
        versions=versions,
        # BEGIN harness-extensibility story 5
        identity_source=identity_source,
        # END harness-extensibility story 5
    )
    if extra_middleware:
        middleware.extend(extra_middleware)

    session_registry = SessionRegistry(checkpointer=ckpt, event_bus=event_bus)

    agent = _build_deep_agent(
        harness=harness,
        identity=identity,
        sandbox=sandbox,
        extra_tools=extra_tools,
        model=model,
        store=langgraph_store,
    )

    return CompiledAgent(
        agent=agent,
        event_bus=event_bus,
        session_registry=session_registry,
        run_package_writer=writer,
        harness=harness,
        sandbox=sandbox,
        approval_channel=approval_channel,
        middleware=middleware,
        versions=versions,
        # BEGIN harness-extensibility story 5
        identity_source=identity_source,
        # END harness-extensibility story 5
        # BEGIN harness-extensibility story 6
        secret_resolver=secret_resolver,
        # END harness-extensibility story 6
        # BEGIN harness-extensibility story 7
        model_provider=model_provider,
        # END harness-extensibility story 7
        # BEGIN harness-extensibility phase 6
        memory_store=memory_store,
        job_storage=job_storage,
        checkpointer_ext=checkpointer_ext,
        # END harness-extensibility phase 6
    )


def _deep_agents_version() -> str:
    try:
        from importlib.metadata import version

        return version("deepagents")
    except Exception:
        return "unknown"


def _build_deep_agent(
    *,
    harness: HarnessConfig,
    identity: LoadedIdentity,
    sandbox: SandboxBackend,
    extra_tools: Sequence[BaseTool] | None,
    model: BaseChatModel | str | None,
    store: Any | None = None,
) -> Any:
    """Wire the harness into ``build_deep_agent`` from the existing monkey-bot core.

    Falls back to a minimal pass-through agent if deepagents is unavailable —
    this keeps unit tests and the linter working in constrained environments.
    """
    system_prompt = identity.system_prompt_block()
    tools = list(_materialize_tools(harness))
    if extra_tools:
        tools.extend(extra_tools)
    subagents = _build_subagent_specs(harness)

    try:
        from ..deepagent import build_deep_agent
        from .sandbox.deepagents_backend import HarnessDeepAgentsSandbox

        backend = HarnessDeepAgentsSandbox(
            sandbox,
            Policy.from_spec(
                harness.sandbox.policy,
                timeout_seconds=harness.sandbox.timeout_seconds,
            ),
        )
        return build_deep_agent(
            model or _build_model(harness),
            tools=tools,
            system_prompt=system_prompt,
            skills=list(harness.skills.dirs),
            backend=backend,
            store=store,
            subagents=subagents,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("build_deep_agent unavailable, falling back to stub agent: %s", exc)
        return _StubAgent(system_prompt=system_prompt, tools=tools)


def _build_subagent_specs(harness: HarnessConfig) -> list[dict[str, Any]] | None:
    """Convert harness subagent specs into DeepAgents subagent dictionaries."""
    specs: list[dict[str, Any]] = []
    for subagent in harness.subagents:
        spec: dict[str, Any] = {
            "name": subagent.name,
            "description": subagent.description,
            "system_prompt": (
                _load_prompt_file(subagent.prompt_file)
                if subagent.prompt_file
                else f"You are the {subagent.name} specialist."
            ),
            "skills": list(subagent.skills),
        }
        if subagent.model:
            spec["model"] = subagent.model
        specs.append(spec)
    return specs or None


def _load_prompt_file(path: str) -> str:
    """Load a subagent system prompt, matching the legacy bot.yaml path."""
    prompt_path = Path(path)
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    log.warning("Prompt file not found: %s", path)
    return ""


class _StubAgent:
    """Fallback agent used when deepagents isn't installable (dev/test only)."""

    def __init__(self, system_prompt: str, tools: list[BaseTool]) -> None:
        self.system_prompt = system_prompt
        self.tools = tools

    async def ainvoke(self, state: dict) -> dict:
        messages = state.get("messages", [])
        return {"messages": list(messages) + [{"role": "assistant", "content": "[stub agent]"}]}
