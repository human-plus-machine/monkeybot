"""MonkeyBot CLI entry point."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import click
import ulid

from monkeybot.core.council import run_council
from monkeybot.core.durable_runs import DurableRunStore
from monkeybot.core.history import ConversationHistory
from monkeybot.core.loop import AgentLoop
from monkeybot.core.runs import cleanup_old_runs
from monkeybot.core.scheduler import JobConfig, Scheduler
from monkeybot.core.subagent_registry import SubagentRegistry
from monkeybot.core.usage import get_usage_summary, record_usage
from monkeybot.gateway.cli import CLIGateway

_council_timers: dict[str, asyncio.Task[None]] = {}
_background_tasks: set[asyncio.Task[None]] = set()


@click.group()
def main() -> None:
    """MonkeyBot v2 — lightweight agent framework."""


@main.command()
@click.option(
    "--bot-dir",
    required=True,
    type=click.Path(exists=True),
    help="Bot directory containing AGENT.md",
)
@click.option("--session-id", default=None, help="Session ID (auto-generated if omitted)")
@click.option(
    "--model",
    default=None,
    help="Override model (default from config.yaml or gemini-2.0-flash)",
)
def run(bot_dir: str, session_id: str | None, model: str | None) -> None:
    """Start an interactive agent session."""
    _setup_logging()
    asyncio.run(_run_async(bot_dir, session_id, model))


def _setup_logging() -> None:
    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            log: dict[str, object] = {
                "timestamp": self.formatTime(record),
                "level": record.levelname,
                "service": "monkeybot",
                "message": record.getMessage(),
            }
            for field in (
                "session_id",
                "run_id",
                "input_tokens",
                "output_tokens",
                "duration_ms",
                "tool",
                "tool_args",
                "result",
                "iteration",
            ):
                if hasattr(record, field):
                    log[field] = getattr(record, field)
            return json.dumps(log)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(
        handlers=[handler],
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    )


async def _run_async(bot_dir: str, session_id: str | None, model: str | None) -> None:
    bot_path = Path(bot_dir)
    agent_md_path = str(bot_path / "AGENT.md")
    config_path = bot_path / "config.yaml"

    bot_config: dict[str, object] = {}
    if config_path.exists():
        import yaml  # type: ignore[import-untyped]

        bot_config = yaml.safe_load(config_path.read_text()) or {}

    model_cfg = bot_config.get("model")
    default_model = (
        model_cfg.get("default", "gemini-2.0-flash")
        if isinstance(model_cfg, dict)
        else "gemini-2.0-flash"
    )

    config: dict[str, object] = {
        "agent_md_path": agent_md_path,
        "memory_path": os.getenv("MEMORY_PATH", "./data/memory"),
        "skills_path": os.getenv("SKILLS_PATH", "./.agents/skills"),
        "bot_dir": str(bot_path),
        "model": model or default_model,
        "council": bot_config.get("council", {}),
    }

    db_url = os.getenv("DB_URL", "sqlite:///data/monkeybot.db")
    db_path = db_url.removeprefix("sqlite:///")
    history = ConversationHistory(db_url=db_url)
    await history.init()

    provider = _load_provider()
    inspectors = _load_inspectors(bot_config)

    registry = _build_registry(bot_config, config)
    durable_store = DurableRunStore(db_path=db_path + ".runs")
    await durable_store.init()
    cleanup_old_runs(tempfile.gettempdir())

    memory_path = str(config["memory_path"])

    async def _turn_cb(sid: str, ev: object) -> None:
        import asyncio as _asyncio  # noqa: PLC0415

        await _asyncio.gather(
            record_usage(db_path, sid, ev),  # type: ignore[arg-type]
            _on_turn_complete(
                sid,
                ev,
                config=config,
                history=history,
                provider=provider,
                memory_path=memory_path,
            ),
        )

    agent_loop = AgentLoop(
        provider=provider,  # type: ignore[arg-type]
        history=history,
        inspectors=inspectors,  # type: ignore[arg-type]
        config=config,
        on_turn_complete=_turn_cb,
        registry=registry,
        durable_store=durable_store,
    )
    gateway = CLIGateway(loop=agent_loop, session_id=session_id or str(ulid.new()))
    await gateway.run_interactive()


def _load_provider() -> object:
    provider_name = os.getenv("MODEL_PROVIDER", "gemini")
    if provider_name == "gemini":
        from monkeybot.providers.gemini import GeminiProvider  # noqa: PLC0415

        return GeminiProvider()
    if provider_name == "claude":
        from monkeybot.providers.claude import ClaudeProvider  # noqa: PLC0415

        return ClaudeProvider()
    if provider_name == "vertex-claude":
        from monkeybot.providers.vertex_claude import VertexClaudeProvider  # noqa: PLC0415

        return VertexClaudeProvider()
    raise ValueError(f"Unknown MODEL_PROVIDER: {provider_name}. Supported: gemini, claude, vertex-claude")


def _build_registry(
    bot_config: dict[str, object], config: dict[str, object]
) -> SubagentRegistry | None:
    """Build SubagentRegistry from config if a subagents block is present."""
    subagents_cfg = bot_config.get("subagents")
    if not isinstance(subagents_cfg, dict):
        return None
    registry_block = subagents_cfg.get("registry", {})
    if not isinstance(registry_block, dict) or not registry_block:
        return None
    try:
        return SubagentRegistry(
            registry_block,
            bot_skills_path=str(config.get("skills_path", "./.agents/skills")),
            bot_model=str(config.get("model", "gemini-2.0-flash")),
        )
    except ValueError:
        logging.getLogger(__name__).exception("Failed to build SubagentRegistry — skipping")
        return None


def _load_inspectors(bot_config: dict[str, object]) -> list[object]:
    from monkeybot.core.safety import load_inspectors  # noqa: PLC0415

    return load_inspectors(bot_config)  # type: ignore[return-value]


async def _on_turn_complete(
    session_id: str,
    tc: object,
    *,
    config: dict[str, object],
    history: ConversationHistory,
    provider: object,
    memory_path: str,
) -> None:
    """Debounced council trigger. Called after every agent turn."""
    council_cfg = config.get("council", {})
    if not isinstance(council_cfg, dict) or not council_cfg.get("enabled"):
        return

    existing = _council_timers.pop(session_id, None)
    if existing and not existing.done():
        existing.cancel()

    idle_seconds = float(council_cfg.get("idle_seconds", 300))  # type: ignore[arg-type]
    bot_model = str(config.get("model", "gemini-2.0-flash"))
    council_model = str(council_cfg.get("model", bot_model))

    async def _fire() -> None:
        await asyncio.sleep(idle_seconds)
        _council_timers.pop(session_id, None)
        msgs = await history.load(session_id)
        text = "\n".join(f"{m.role}: {m.content}" for m in msgs)
        await run_council(text, memory_path, provider, council_model, session_id)  # type: ignore[arg-type]

    task = asyncio.create_task(_fire())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    _council_timers[session_id] = task


async def _flush_council_on_shutdown(
    *,
    history: ConversationHistory,
    provider: object,
    memory_path: str,
    council_model: str,
) -> None:
    """Cancel all pending idle timers; run council immediately for each session.

    Called from _serve_async try/finally before process exit.
    """
    for session_id, task in list(_council_timers.items()):
        task.cancel()
        try:
            msgs = await history.load(session_id)
            text = "\n".join(f"{m.role}: {m.content}" for m in msgs)
            await run_council(text, memory_path, provider, council_model, session_id)  # type: ignore[arg-type]
        except Exception:
            logging.getLogger(__name__).exception(
                "flush_council failed session_id=%s", session_id
            )
    _council_timers.clear()


@main.command()
@click.option(
    "--bot-dir",
    required=True,
    type=click.Path(exists=True),
    help="Bot directory containing AGENT.md and optionally webhook.py",
)
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to listen on")
def serve(bot_dir: str, host: str, port: int) -> None:
    """Start the webhook gateway server."""
    _setup_logging()
    asyncio.run(_serve_async(bot_dir, host, port))


async def _serve_async(bot_dir: str, host: str, port: int) -> None:
    import uvicorn  # noqa: PLC0415

    from monkeybot.gateway.webhook import (  # noqa: PLC0415
        WebhookGateway,
        load_bot_webhook,
    )

    bot_path = Path(bot_dir)
    config_path = bot_path / "config.yaml"
    bot_config: dict[str, object] = {}
    if config_path.exists():
        import yaml  # noqa: PLC0415

        bot_config = yaml.safe_load(config_path.read_text()) or {}

    model_cfg = bot_config.get("model")
    default_model = (
        model_cfg.get("default", "gemini-2.0-flash")
        if isinstance(model_cfg, dict)
        else "gemini-2.0-flash"
    )

    config: dict[str, object] = {
        "agent_md_path": str(bot_path / "AGENT.md"),
        "memory_path": os.getenv("MEMORY_PATH", "./data/memory"),
        "skills_path": os.getenv("SKILLS_PATH", "./.agents/skills"),
        "bot_dir": str(bot_path),
        "model": default_model,
        "council": bot_config.get("council", {}),
    }

    db_url = os.getenv("DB_URL", "sqlite:///data/monkeybot.db")
    db_path = db_url.removeprefix("sqlite:///")
    history = ConversationHistory(db_url=db_url)
    await history.init()

    provider = _load_provider()
    inspectors = _load_inspectors(bot_config)

    registry = _build_registry(bot_config, config)
    durable_store = DurableRunStore(db_path=db_path + ".runs")
    await durable_store.init()
    cleanup_old_runs(tempfile.gettempdir())

    memory_path = str(config["memory_path"])
    council_cfg = bot_config.get("council", {})
    bot_model_str = str(config["model"])
    council_model = (
        str(council_cfg.get("model", bot_model_str))  # type: ignore[union-attr]
        if isinstance(council_cfg, dict)
        else bot_model_str
    )

    async def _turn_cb(sid: str, ev: object) -> None:
        import asyncio as _asyncio  # noqa: PLC0415

        await _asyncio.gather(
            record_usage(db_path, sid, ev),  # type: ignore[arg-type]
            _on_turn_complete(
                sid,
                ev,
                config=config,
                history=history,
                provider=provider,
                memory_path=memory_path,
            ),
        )

    agent_loop = AgentLoop(
        provider=provider,  # type: ignore[arg-type]
        history=history,
        inspectors=inspectors,  # type: ignore[arg-type]
        config=config,
        on_turn_complete=_turn_cb,
        registry=registry,
        durable_store=durable_store,
    )

    # Build Scheduler if jobs defined in config
    scheduler: Scheduler | None = None
    scheduler_cfg = bot_config.get("scheduler")
    if isinstance(scheduler_cfg, dict) and scheduler_cfg.get("jobs"):
        raw_jobs = scheduler_cfg["jobs"]
        poll_interval = int(scheduler_cfg.get("poll_interval", 30))
        jobs = [
            JobConfig(
                name=name,
                cron=str(cfg.get("cron", "0 * * * *")),
                callable=str(cfg.get("callable", "")),
                enabled=bool(cfg.get("enabled", True)),
            )
            for name, cfg in raw_jobs.items()
            if isinstance(cfg, dict) and cfg.get("callable")
        ]
        scheduler = Scheduler(db_path=db_path, jobs=jobs, poll_interval=poll_interval)

    extract, fmt, session_fn = load_bot_webhook(bot_dir)
    gateway = WebhookGateway(
        loop=agent_loop,
        session_id_fn=session_fn,
        extract_message=extract,
        format_response=fmt,
    )

    if not os.getenv("WEBHOOK_SECRET"):
        logging.getLogger(__name__).warning(
            "WEBHOOK_SECRET not set — webhook endpoint is unauthenticated"
        )

    app = gateway.build_app()
    server_config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(server_config)

    if scheduler is not None:
        await scheduler.start()
    try:
        await server.serve()
    finally:
        if scheduler is not None:
            await scheduler.stop()
        await _flush_council_on_shutdown(
            history=history,
            provider=provider,
            memory_path=memory_path,
            council_model=council_model,
        )


@main.command()
@click.option("--since", default=24.0, type=float, help="Look back N hours (default: 24)")
def usage(since: float) -> None:
    """Show token usage and cost summary."""
    db_url = os.getenv("DB_URL", "sqlite:///data/monkeybot.db")
    db_path = db_url.removeprefix("sqlite:///")
    summary = asyncio.run(get_usage_summary(db_path, since))
    if summary.turns == 0:
        click.echo("No usage data found.")
        return
    click.echo(f"Usage summary (last {since:.0f}h)")
    click.echo("─" * 36)
    click.echo(f"{'Turns':<20}: {summary.turns:>10,}")
    click.echo(f"{'Input tokens':<20}: {summary.input_tokens:>10,}")
    click.echo(f"{'Output tokens':<20}: {summary.output_tokens:>10,}")
    click.echo(f"{'Cached tokens':<20}: {summary.cached_tokens:>10,}")
    click.echo(f"{'Total cost (USD)':<20}: ${summary.total_cost_usd:>10.4f}")
    click.echo(f"{'Avg latency (ms)':<20}: {summary.avg_latency_ms:>10.0f}")


if __name__ == "__main__":
    main()
