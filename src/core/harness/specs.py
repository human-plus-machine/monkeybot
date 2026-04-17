"""Pydantic schemas for HarnessConfig (v1). See docs/agent-harness.md.

Every sub-spec has sensible defaults so minimal configs are trivial; every field is
validated at build time by the harness linter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .versioning import HARNESS_SCHEMA_VERSION, migrate_config


_BASE_CONFIG = ConfigDict(extra="forbid")


class AgentSpec(BaseModel):
    model_config = _BASE_CONFIG
    name: str
    model: str = "gemini-2.5-flash"
    provider: Literal["google_vertexai", "openai", "anthropic", "bedrock", "vertex_anthropic"] = "google_vertexai"
    temperature: float = 0.7
    max_output_tokens: int = 8192
    thinking_budget: int = -1
    extra_model_kwargs: dict[str, Any] = Field(default_factory=dict)


class IdentitySpec(BaseModel):
    model_config = _BASE_CONFIG
    dir: str = "./data/memory"
    soul_file: str = "SOUL.md"
    rules_file: str = "RULES.md"
    identity_file: str = "IDENTITY.md"
    user_file: str = "USER.md"
    index_file: str = "INDEX.md"
    memory_file: str = "MEMORY.md"
    heartbeat_file: str = "HEARTBEAT.md"
    enforce_rules: bool = True
    allow_identity_introspection: bool = True


class ContextPolicySpec(BaseModel):
    model_config = _BASE_CONFIG
    token_budget: int = 175_000
    summarize_at: float = 0.75
    hard_reset_at: float = 0.92
    tool_output_offload_threshold: int = 20_000
    progressive_skill_disclosure: bool = True
    emit_utilization_events: bool = True

    @model_validator(mode="after")
    def _thresholds_order(self) -> "ContextPolicySpec":
        if not 0.0 < self.summarize_at < self.hard_reset_at <= 1.0:
            raise ValueError(
                "ContextPolicySpec requires 0 < summarize_at < hard_reset_at <= 1.0 "
                f"(got summarize_at={self.summarize_at}, hard_reset_at={self.hard_reset_at})"
            )
        return self


class PolicySpec(BaseModel):
    """Declarative sandbox policy. Versioned, hot-reloadable (SB-6)."""

    model_config = _BASE_CONFIG
    version: int = 1
    fs_allow: list[str] = Field(default_factory=lambda: ["./workspace/**"])
    fs_deny: list[str] = Field(default_factory=lambda: ["**/.env", "**/*.pem", "**/.git/**"])
    net_allow: list[str] = Field(default_factory=list)
    net_deny: list[str] = Field(default_factory=lambda: ["*"])
    env_allow: list[str] = Field(default_factory=list)
    secret_handles: dict[str, str] = Field(default_factory=dict)


class CommandTierSpec(BaseModel):
    model_config = _BASE_CONFIG
    preapproved: list[str] = Field(default_factory=list)
    requires_approval: list[str] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=lambda: ["sudo", "rm -rf /", "curl | bash"])


class SandboxSpec(BaseModel):
    model_config = _BASE_CONFIG
    backend: Literal["local_shell", "modal", "custom"] = "local_shell"
    custom_import_path: str | None = None
    policy: PolicySpec = Field(default_factory=PolicySpec)
    command_tiers: CommandTierSpec = Field(default_factory=CommandTierSpec)
    reload_policy_on_sighup: bool = True
    timeout_seconds: int = 300

    @model_validator(mode="after")
    def _custom_needs_import(self) -> "SandboxSpec":
        if self.backend == "custom" and not self.custom_import_path:
            raise ValueError("SandboxSpec.backend='custom' requires custom_import_path")
        return self


class SkillsSpec(BaseModel):
    model_config = _BASE_CONFIG
    dirs: list[str] = Field(default_factory=lambda: ["./skills"])
    progressive: bool = True
    semantic_discovery: bool = False
    embedder_import_path: str | None = None

    @model_validator(mode="after")
    def _embedder_required(self) -> "SkillsSpec":
        if self.semantic_discovery and not self.embedder_import_path:
            raise ValueError("SkillsSpec.semantic_discovery=True requires embedder_import_path")
        return self


class MCPServerSpec(BaseModel):
    model_config = _BASE_CONFIG
    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    transport: Literal["stdio", "sse", "http"] = "stdio"

    @model_validator(mode="after")
    def _transport_requires(self) -> "MCPServerSpec":
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"MCP server {self.name!r} stdio transport requires 'command'")
        if self.transport in ("sse", "http") and not self.url:
            raise ValueError(f"MCP server {self.name!r} {self.transport} transport requires 'url'")
        return self


class ToolSpec(BaseModel):
    model_config = _BASE_CONFIG
    name: str
    import_path: str
    tier: Literal["preapproved", "requires_approval", "denied"] = "preapproved"
    side_effects: Literal["none", "read", "write", "external"] = "none"
    idempotent: bool = True
    approx_latency_ms: int = 100
    failure_modes: list[str] = Field(default_factory=list)


class SubagentSpec(BaseModel):
    model_config = _BASE_CONFIG
    name: str
    description: str
    skills: list[str] = Field(default_factory=list)
    prompt_file: str | None = None
    model: str | None = None
    recursion_depth_limit: int = 3
    sandbox_policy: PolicySpec | None = None


class HITLSpec(BaseModel):
    model_config = _BASE_CONFIG
    mode: Literal["sync_interrupt", "async_webhook", "disabled"] = "sync_interrupt"
    channel: Literal["google_chat", "webhook", "disabled"] = "google_chat"
    webhook_url: str | None = None
    approval_timeout_seconds: int = 600

    @model_validator(mode="after")
    def _webhook_needs_url(self) -> "HITLSpec":
        if self.channel == "webhook" and not self.webhook_url:
            raise ValueError("HITLSpec.channel='webhook' requires webhook_url")
        return self


class AutonomySpec(BaseModel):
    model_config = _BASE_CONFIG
    retry_max: int = 3
    retry_backoff_base_seconds: float = 1.0
    escalate_after_consecutive_failures: int = 5
    heartbeat_cadence: str | None = None
    checkpoint_before_destructive: bool = True
    progress_signal_seconds: int = 60


class RunPackageSpec(BaseModel):
    model_config = _BASE_CONFIG
    writer: Literal["local", "gcs", "s3", "disabled"] = "local"
    sink_uri: str = "./data/run_packages"
    include_full_traces: bool = True
    include_token_trace: bool = True


class ObservabilitySpec(BaseModel):
    model_config = _BASE_CONFIG
    event_bus: Literal["default", "disabled"] = "default"
    run_package: RunPackageSpec = Field(default_factory=RunPackageSpec)
    emit_llm_call_events: bool = True
    emit_tool_call_events: bool = True
    emit_context_events: bool = True


class SecuritySpec(BaseModel):
    model_config = _BASE_CONFIG
    pii_redaction: bool = True
    secret_redaction: bool = True
    redaction_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?i)(api[_-]?key|secret|token|password|bearer)[\"'\s:=]+[A-Za-z0-9_\-\.]{8,}",
            r"-----BEGIN [A-Z ]+ PRIVATE KEY-----",
        ]
    )
    audit_log_sink: str | None = None
    principal_required: bool = True


class CostSpec(BaseModel):
    model_config = _BASE_CONFIG
    per_task_usd_budget: float | None = None
    pricing_table: dict[str, dict[str, float]] = Field(default_factory=dict)
    hard_kill_at_budget: bool = False


class SchedulerSpec(BaseModel):
    model_config = _BASE_CONFIG
    storage: Literal["json", "firestore", "dynamodb", "disabled"] = "json"
    cadence: str = "* * * * *"
    timezone: str = "UTC"
    memory_dir: str = "./data/memory"


class GatewaySpec(BaseModel):
    model_config = _BASE_CONFIG
    port: int = 8080
    log_level: str = "INFO"
    allowed_users: list[str] = Field(default_factory=list)
    enable_agentcore_route: bool = False
    enable_cloudrun_route: bool = True
    enable_control_plane: bool = True


# BEGIN harness-extensibility story 1
from .extensions.specs import (
    CheckpointerSpec,
    IdentitySourceSpec,
    JobStorageSpec,
    MemoryStoreSpec,
    ModelProviderSpec,
    SecretResolverSpec,
)

# END harness-extensibility story 1


class HarnessConfig(BaseModel):
    """Top-level Agent Harness configuration (schema v1)."""

    model_config = _BASE_CONFIG

    version: Literal["1"] = HARNESS_SCHEMA_VERSION
    agent: AgentSpec
    identity: IdentitySpec = Field(default_factory=IdentitySpec)
    context: ContextPolicySpec = Field(default_factory=ContextPolicySpec)
    sandbox: SandboxSpec = Field(default_factory=SandboxSpec)
    skills: SkillsSpec = Field(default_factory=SkillsSpec)
    tools: list[ToolSpec] = Field(default_factory=list)
    mcp_servers: list[MCPServerSpec] = Field(default_factory=list)
    subagents: list[SubagentSpec] = Field(default_factory=list)
    hitl: HITLSpec = Field(default_factory=HITLSpec)
    autonomy: AutonomySpec = Field(default_factory=AutonomySpec)
    observability: ObservabilitySpec = Field(default_factory=ObservabilitySpec)
    security: SecuritySpec = Field(default_factory=SecuritySpec)
    cost: CostSpec = Field(default_factory=CostSpec)
    scheduler: SchedulerSpec = Field(default_factory=SchedulerSpec)
    gateway: GatewaySpec = Field(default_factory=GatewaySpec)
    extensions: dict[str, Any] = Field(default_factory=dict)

    # BEGIN harness-extensibility story 1
    checkpointer: CheckpointerSpec | None = None
    memory_store: MemoryStoreSpec | None = None
    job_storage: JobStorageSpec | None = None
    identity_source: IdentitySourceSpec | None = None
    secret_resolver: SecretResolverSpec = Field(default_factory=SecretResolverSpec.default)
    model_provider: ModelProviderSpec | None = None
    # END harness-extensibility story 1

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HarnessConfig":
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a YAML mapping at top level")
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HarnessConfig":
        migrated = migrate_config(dict(data))
        return cls.model_validate(migrated)

    def to_yaml(self, path: str | Path) -> None:
        Path(path).write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False))
