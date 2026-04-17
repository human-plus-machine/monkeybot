"""Agent Harness — the seven-pillar operating system around LangGraph Deep Agents.

Primary public API:

    from emonk.core.harness import HarnessConfig, build_universal_agent
    from emonk.core.harness.events import EventKind

See docs/agent-harness.md for the full guide.
"""

from .assembler import build_universal_agent

# isort: off
# BEGIN harness-extensibility story 2
# ``DynamoDBCheckpointerStub`` removed — see src/core/harness/checkpointer.py
# and docs/extending-the-harness.md (Story 9) for the replacement worked example.
from .checkpointer import (
    CheckpointerBackend,
    CheckpointRef,
    FirestoreCheckpointer,
    InMemoryCheckpointer,
)

# END harness-extensibility story 2
from .compiled_agent import CompiledAgent
from .control import IntrospectionReport, SessionRegistry, SessionState
from .errors import (
    ApprovalDenied,
    BudgetExceeded,
    HarnessConfigError,
    HarnessError,
    RecursionBudgetExceeded,
    RedactionError,
    RuleViolation,
    SandboxDenied,
)
from .event_bus import EventBus, EventHandler, LoggingEventHandler
from .events import EventKind, HarnessEvent, Principal, VersionTriple

# BEGIN harness-extensibility story 1
from .extensions import (
    BackendCapabilityMismatch,
    BackendConfigError,
    BackendNotFound,
    Checkpointer,
    CheckpointerError,
    CheckpointerSpec,
    CheckpointMissing,
    IdentityNotFound,
    IdentitySource,
    IdentitySourceSpec,
    Item,
    JobStorage,
    JobStorageSpec,
    LoadedIdentity,
    MemoryPatch,
    MemoryStore,
    MemoryStoreCapabilities,
    MemoryStoreError,
    MemoryStoreSpec,
    ModelCapabilities,
    ModelProvider,
    ModelProviderError,
    ModelProviderSpec,
    Registry,
    RegistryEntry,
    RegistrySource,
    SecretNotFound,
    SecretResolver,
    SecretResolverError,
    SecretResolverSpec,
)

# END harness-extensibility story 1
# BEGIN harness-extensibility story 2
# The legacy ``InMemoryCheckpointer`` / ``FirestoreCheckpointer`` names above
# stay the canonical re-exports to keep pre-Story-2 consumers working; new
# ABC-conformant backends are importable from
# ``emonk.core.harness.extensions.checkpointers`` directly. Only the new
# Postgres/Mongo builtins are added to this public surface.
from .extensions.checkpointers import MongoCheckpointer, PostgresCheckpointer

# END harness-extensibility story 2
# BEGIN harness-extensibility story 3
from .extensions.memory_stores import (
    FirestoreMemoryStore,
    GCSMemoryStore,
    InMemoryMemoryStore,
    MongoMemoryStore,
    PostgresMemoryStore,
    S3MemoryStore,
)

# END harness-extensibility story 3
# BEGIN harness-extensibility story 4
from .extensions.job_storage import (
    FirestoreJobStorage,
    JSONFileJobStorage,
    MongoJobStorage,
    PostgresJobStorage,
)

# END harness-extensibility story 4
# BEGIN harness-extensibility story 5
from .extensions.identity_sources import (
    CallableIdentitySource,
    GCSIdentitySource,
    LocalFSIdentitySource,
    MongoIdentitySource,
    PostgresIdentitySource,
    S3IdentitySource,
)

# END harness-extensibility story 5
# BEGIN harness-extensibility story 6
from .extensions.secret_resolvers import (
    AWSSecretsManagerResolver,
    CompositeSecretResolver,
    EnvSecretResolver,
    GCPSecretManagerResolver,
)

# END harness-extensibility story 6
# BEGIN harness-extensibility story 7
from .extensions.model_providers import (
    AnthropicProvider,
    BedrockProvider,
    OllamaProvider,
    OpenAIProvider,
    VertexProvider,
)

# END harness-extensibility story 7
# isort: on
from .hitl.protocol import ApprovalChannel, ApprovalDecision, ApprovalRequest
from .mcp import load_mcp_tools
from .middleware.identity_resolution import IdentityResolutionMW
from .principal import ANONYMOUS, make_service_principal, make_user_principal
from .runpackage import (
    ApprovalRecord,
    RunPackage,
    RunPackageRef,
    TokenAccounting,
    ToolCallRecord,
)
from .runpackage_writers import (
    DisabledRunPackageWriter,
    GCSRunPackageWriter,
    LocalRunPackageWriter,
    RunPackageWriter,
    S3RunPackageWriter,
)
from .sandbox import (
    ExecuteResult,
    FileInfo,
    LocalShellSandbox,
    ModalSandbox,
    Policy,
    SandboxBackend,
    SandboxCapabilities,
    WriteResult,
)
from .specs import (
    AgentSpec,
    AutonomySpec,
    CommandTierSpec,
    ContextPolicySpec,
    CostSpec,
    GatewaySpec,
    HarnessConfig,
    HITLSpec,
    IdentitySpec,
    MCPServerSpec,
    ObservabilitySpec,
    PolicySpec,
    RunPackageSpec,
    SandboxSpec,
    SchedulerSpec,
    SecuritySpec,
    SkillsSpec,
    SubagentSpec,
    ToolSpec,
)
from .subagent_results import SubagentResult
from .versioning import HARNESS_SCHEMA_VERSION, migrate_config

__all__ = [
    "ANONYMOUS",
    "AgentSpec",
    "ApprovalChannel",
    "ApprovalDecision",
    "ApprovalDenied",
    "ApprovalRecord",
    "ApprovalRequest",
    "AutonomySpec",
    "BackendCapabilityMismatch",
    "BackendConfigError",
    "BackendNotFound",
    "BudgetExceeded",
    # BEGIN harness-extensibility story 5
    "CallableIdentitySource",
    # END harness-extensibility story 5
    "CheckpointMissing",
    "CheckpointRef",
    "Checkpointer",
    "CheckpointerBackend",
    "CheckpointerError",
    "CheckpointerSpec",
    "CommandTierSpec",
    "CompiledAgent",
    "ContextPolicySpec",
    "CostSpec",
    "DisabledRunPackageWriter",
    "EventBus",
    "EventHandler",
    "EventKind",
    "ExecuteResult",
    "FileInfo",
    "FirestoreCheckpointer",
    # BEGIN harness-extensibility story 4
    "FirestoreJobStorage",
    # END harness-extensibility story 4
    # BEGIN harness-extensibility story 3
    "FirestoreMemoryStore",
    # END harness-extensibility story 3
    "GCSRunPackageWriter",
    # BEGIN harness-extensibility story 3
    "GCSMemoryStore",
    # END harness-extensibility story 3
    # BEGIN harness-extensibility story 5
    "GCSIdentitySource",
    # END harness-extensibility story 5
    "GatewaySpec",
    "HARNESS_SCHEMA_VERSION",
    "HITLSpec",
    "HarnessConfig",
    "HarnessConfigError",
    "HarnessError",
    "HarnessEvent",
    "IdentityNotFound",
    # BEGIN harness-extensibility story 5
    "IdentityResolutionMW",
    # END harness-extensibility story 5
    "IdentitySource",
    "IdentitySourceSpec",
    "IdentitySpec",
    "InMemoryCheckpointer",
    # BEGIN harness-extensibility story 3
    "InMemoryMemoryStore",
    # END harness-extensibility story 3
    "IntrospectionReport",
    "Item",
    "JobStorage",
    "JobStorageSpec",
    # BEGIN harness-extensibility story 4
    "JSONFileJobStorage",
    # END harness-extensibility story 4
    "LoadedIdentity",
    # BEGIN harness-extensibility story 5
    "LocalFSIdentitySource",
    # END harness-extensibility story 5
    "LocalRunPackageWriter",
    "LocalShellSandbox",
    "LoggingEventHandler",
    "MCPServerSpec",
    "MemoryPatch",
    "MemoryStore",
    "MemoryStoreCapabilities",
    "MemoryStoreError",
    "MemoryStoreSpec",
    "ModalSandbox",
    "ModelCapabilities",
    "ModelProvider",
    "ModelProviderError",
    "ModelProviderSpec",
    # BEGIN harness-extensibility story 2
    "MongoCheckpointer",
    # END harness-extensibility story 2
    # BEGIN harness-extensibility story 4
    "MongoJobStorage",
    # END harness-extensibility story 4
    # BEGIN harness-extensibility story 3
    "MongoMemoryStore",
    # END harness-extensibility story 3
    # BEGIN harness-extensibility story 5
    "MongoIdentitySource",
    # END harness-extensibility story 5
    "ObservabilitySpec",
    "Policy",
    "PolicySpec",
    # BEGIN harness-extensibility story 2
    "PostgresCheckpointer",
    # END harness-extensibility story 2
    # BEGIN harness-extensibility story 4
    "PostgresJobStorage",
    # END harness-extensibility story 4
    # BEGIN harness-extensibility story 3
    "PostgresMemoryStore",
    # END harness-extensibility story 3
    # BEGIN harness-extensibility story 5
    "PostgresIdentitySource",
    # END harness-extensibility story 5
    "Principal",
    "RecursionBudgetExceeded",
    "RedactionError",
    "Registry",
    "RegistryEntry",
    "RegistrySource",
    "RuleViolation",
    "RunPackage",
    "RunPackageRef",
    "RunPackageSpec",
    "RunPackageWriter",
    "S3RunPackageWriter",
    # BEGIN harness-extensibility story 3
    "S3MemoryStore",
    # END harness-extensibility story 3
    # BEGIN harness-extensibility story 5
    "S3IdentitySource",
    # END harness-extensibility story 5
    "SandboxBackend",
    "SandboxCapabilities",
    "SandboxDenied",
    "SandboxSpec",
    "SchedulerSpec",
    "SecretNotFound",
    "SecretResolver",
    "SecretResolverError",
    "SecretResolverSpec",
    # BEGIN harness-extensibility story 6
    "AWSSecretsManagerResolver",
    "CompositeSecretResolver",
    "EnvSecretResolver",
    "GCPSecretManagerResolver",
    # END harness-extensibility story 6
    # BEGIN harness-extensibility story 7
    "AnthropicProvider",
    "BedrockProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "VertexProvider",
    # END harness-extensibility story 7
    "SecuritySpec",
    "SessionRegistry",
    "SessionState",
    "SkillsSpec",
    "SubagentResult",
    "SubagentSpec",
    "TokenAccounting",
    "ToolCallRecord",
    "ToolSpec",
    "VersionTriple",
    "WriteResult",
    "build_universal_agent",
    "load_mcp_tools",
    "make_service_principal",
    "make_user_principal",
    "migrate_config",
]
