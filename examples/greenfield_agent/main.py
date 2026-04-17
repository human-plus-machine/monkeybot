"""Greenfield example: a minimal enterprise agent using the Agent Harness.

This is the file an enterprise consumer would deploy to AgentCore (or Cloud Run).
It shows:

 * `HarnessConfig` built in Python (the spec mode the consumer chose)
 * A custom tool wired via `ToolSpec` → `import_path`
 * Phoenix + DeepEval subscription (illustrative; real imports are commented)
 * RunPackage sink on local disk for the demo

Run:

    uv run python -m examples.greenfield_agent.main

Then:

    curl -X POST http://localhost:8080/agentcore/invocations \\
      -H 'content-type: application/json' \\
      -d '{"inputText":"hello","sessionId":"demo-1","sessionState":{"sessionAttributes":{"user_id":"alice"}}}'
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from langchain_core.tools import tool

from src.core.harness import (
    AgentSpec,
    ContextPolicySpec,
    EventKind,
    HarnessConfig,
    IdentitySpec,
    MCPServerSpec,
    ObservabilitySpec,
    PolicySpec,
    RunPackageSpec,
    SandboxSpec,
    SecuritySpec,
    SubagentSpec,
    ToolSpec,
    build_universal_agent,
)
from src.core.harness.principal import make_user_principal
from src.gateway.agentcore_routes import router as agentcore_router
from src.gateway.harness_routes import router as harness_router


@tool
def company_kb_lookup(query: str) -> str:
    """Search the company knowledge base for `query`."""
    return f"[kb stub] results for: {query}"


def _bootstrap_identity(base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "SOUL.md").write_text(
        "I am the enterprise agent. I help employees automate work safely.\n"
    )
    (base / "IDENTITY.md").write_text("role: enterprise-agent\nvoice: concise, professional\n")
    (base / "USER.md").write_text("Prefer Markdown. Avoid jargon. Cite sources.\n")
    (base / "RULES.md").write_text(
        "- [R-1] DENY_TOOL: git push\n"
        "- [R-2] DENY_SANDBOX_WRITE: /etc/**\n"
        "- [R-3] DENY_PATTERN: (?i)\\bdrop\\s+table\\b\n"
    )


def build_config() -> HarnessConfig:
    base = Path("./greenfield_data")
    _bootstrap_identity(base / "memory")

    return HarnessConfig(
        agent=AgentSpec(name="greenfield-demo", model="gemini-2.5-flash"),
        identity=IdentitySpec(dir=str(base / "memory"), enforce_rules=True),
        context=ContextPolicySpec(token_budget=160_000),
        sandbox=SandboxSpec(
            backend="local_shell",
            policy=PolicySpec(fs_allow=[str(base.resolve()) + "/**"]),
        ),
        tools=[
            ToolSpec(
                name="company_kb_lookup",
                import_path="examples.greenfield_agent.main:company_kb_lookup",
                tier="preapproved",
                side_effects="read",
            )
        ],
        mcp_servers=[
            # Uncomment to wire a filesystem MCP server
            # MCPServerSpec(
            #     name="filesystem",
            #     command="npx",
            #     args=["@modelcontextprotocol/server-filesystem", "./greenfield_data"],
            # ),
        ],
        subagents=[
            SubagentSpec(name="writer", description="Drafts long-form docs", recursion_depth_limit=2),
        ],
        security=SecuritySpec(principal_required=True),
        observability=ObservabilitySpec(
            run_package=RunPackageSpec(writer="local", sink_uri=str(base / "runs"))
        ),
    )


# --- Optional: Phoenix + DeepEval handlers (consumer-owned) --------------------

class PhoenixSpanHandler:
    name = "phoenix"

    async def handle(self, event):
        # Replace with real phoenix.otel.trace integration.
        if event.kind in (EventKind.LLM_CALL, EventKind.LLM_RESULT, EventKind.TOOL_CALL):
            print(f"[phoenix] {event.kind.value} run={event.run_id}")


class DeepEvalHandler:
    name = "deepeval"

    async def handle(self, event):
        # On task completion, pull the run package and run DeepEval.
        # Keep this best-effort; the harness isolates exceptions.
        if event.kind == EventKind.TASK_COMPLETE:
            print(f"[deepeval] scoring run={event.run_id}")


# -------------------------------------------------------------------------------

def build_app() -> FastAPI:
    compiled = build_universal_agent(build_config())
    compiled.event_bus.subscribe(PhoenixSpanHandler())
    compiled.event_bus.subscribe(DeepEvalHandler())

    app = FastAPI(title="greenfield-demo")
    app.include_router(agentcore_router)
    app.include_router(harness_router)
    app.state.compiled_agent = compiled
    app.state.session_registry = compiled.session_registry
    app.state.run_package_writer = compiled.run_package_writer
    app.state.approval_channel = compiled.approval_channel
    return app


async def _demo_invoke(app: FastAPI) -> None:
    compiled = app.state.compiled_agent
    result = await compiled.ainvoke(
        [{"role": "user", "content": "hello"}],
        principal=make_user_principal(user_id="alice", email="alice@demo.example"),
        session_id="demo-boot",
    )
    print(f"demo run_id={result['run_id']} outcome={result['outcome']}")


def main() -> None:
    app = build_app()
    asyncio.run(_demo_invoke(app))
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
