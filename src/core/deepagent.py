"""Deep agent factory for monkey-bot.

Provides build_deep_agent() — the primary public API for constructing
agents with monkey-bot's opinionated defaults on top of LangChain Deep Agents.
"""

import logging
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any
from datetime import UTC
from pathlib import Path

import yaml
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore

from .filesystem_sync import GCSFilesystemSync
from .prompt import compose_system_prompt
from .store import create_search_memory_tool
from .task_tool_patches import apply_task_tool_patches

logger = logging.getLogger(__name__)

# Try to import deep agents
try:
    from deepagents import create_deep_agent
    from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol

    _DEEPAGENTS_AVAILABLE = True
except ImportError:
    _DEEPAGENTS_AVAILABLE = False
    BackendProtocol = object
    SandboxBackendProtocol = object


def build_deep_agent(
    model: str | BaseChatModel,
    *,
    tools: Sequence[BaseTool | Callable] | None = None,
    system_prompt: str = "",
    skills: list[str] | None = None,
    backend: object | None = None,
    store: BaseStore | None = None,
    scheduler: object | None = None,
    subagents: list[dict] | None = None,
    checkpointer: object | None = None,
    summarization_trigger: tuple[str, float] = ("fraction", 0.85),
    summarization_keep: tuple[str, float] = ("fraction", 0.10),
    soul_file: "str | None" = None,
    tools_file: "str | None" = None,
    heartbeat: "object | None" = None,
    voice: "object | None" = None,
    identity_file: "str | None" = None,
    extra_middleware: Sequence[Any] | None = None,
    subagent_middleware: Sequence[Any] | None = None,
    workspace_settings: Any | None = None,
    subagent_invocation_ctx: Callable[[str], AbstractContextManager[Any]] | None = None,
):
    """Build a deep agent with monkey-bot's opinionated defaults.

    This is the primary public API for constructing agents with monkey-bot's
    opinionated defaults on top of LangChain Deep Agents. It handles:
    - 3-layer system prompt composition (internal + base + user)
    - Skills manifest generation from SKILL.md files
    - Auto-adding scheduler and memory tools
    - Middleware configuration (summarization, subagents)
    - Backend setup

    Args:
        model: LLM model name (e.g., "gemini-2.5-flash") or BaseChatModel instance
        tools: Optional list of LangChain tools or callables
        system_prompt: User's custom system prompt (Layer 3)
        skills: List of skill directory paths to load (e.g., ["./skills/", "/shared/skills/"])
        backend: Backend protocol implementation (BackendProtocol or SandboxBackendProtocol)
        store: LangGraph Store for long-term memory (enables search_memory tool)
        scheduler: Scheduler instance (enables schedule_task tool)
        subagents: List of subagent configs for SubAgentMiddleware
        checkpointer: LangGraph checkpointer for conversation persistence (defaults to InMemorySaver)
        summarization_trigger: When to trigger summarization (type, value)
        summarization_keep: How much context to keep after summarization (type, value)
        extra_middleware: Additional LangChain ``AgentMiddleware`` instances appended to the
            orchestrator stack (e.g. tool limits, output guards).
        subagent_middleware: Same middleware instances merged into each YAML/dict subagent's
            ``middleware`` list (skipped for compiled subagents with ``runnable``).
        workspace_settings: Optional app settings object; stored on the returned agent as
            ``agent.workspace_settings`` for FastAPI or other hosts (e.g. workspace file API).
        subagent_invocation_ctx: Optional context manager factory ``(subagent_name) -> ctx``;
            passed to the patched ``task`` tool (e.g. for logging which subagent is active).

    Returns:
        Compiled deep agent (LangGraph graph)

    Raises:
        ImportError: If deepagents package is not installed
        ValueError: If required dependencies are missing for enabled features

    Example:
        >>> from langchain_google_vertexai import ChatVertexAI
        >>> from langgraph.store.memory import InMemoryStore
        >>>
        >>> model = ChatVertexAI(model_name="gemini-2.5-flash")
        >>> store = InMemoryStore()
        >>>
        >>> agent = build_deep_agent(
        ...     model=model,
        ...     tools=[my_custom_tool],
        ...     system_prompt="You are a helpful marketing assistant.",
        ...     skills=["./skills/"],
        ...     store=store,
        ...     scheduler=my_scheduler,
        ... )
        >>>
        >>> # Invoke the agent
        >>> result = await agent.ainvoke(
        ...     {"messages": [{"role": "user", "content": "Hello"}]},
        ...     config={"configurable": {"thread_id": "thread-123"}}
        ... )
    """
    if not _DEEPAGENTS_AVAILABLE:
        raise ImportError(
            "deepagents package required: pip install deepagents\n"
            "See: https://github.com/langchain-ai/deepagents"
        )

    apply_task_tool_patches(subagent_invocation_ctx=subagent_invocation_ctx)

    # Step 1: Collect all tools
    all_tools = list(tools) if tools else []

    # Auto-add schedule_task if scheduler provided
    if scheduler is not None:
        schedule_tool = _create_schedule_task_tool(scheduler)
        all_tools.append(schedule_tool)
        logger.info("Auto-added schedule_task tool")

    # Auto-add search_memory if store provided
    if store is not None:
        memory_tool = create_search_memory_tool(store)
        all_tools.append(memory_tool)
        logger.info("Auto-added search_memory tool")

    # Step 2: Generate skills manifest
    skills_manifest = ""
    if skills:
        skills_manifest = _generate_skills_manifest(skills)
        logger.info(f"Generated skills manifest from {len(skills)} directories")

    # Step 3: Resolve GCS filesystem sync from env vars (set by load_bot_config from bot.yaml)
    # memory.backend: gcs in bot.yaml → MEMORY_BACKEND=gcs + GCS_MEMORY_BUCKET set
    fs_sync: GCSFilesystemSync | None = None
    if os.getenv("MEMORY_BACKEND", "local") == "gcs":
        memory_bucket = os.getenv("GCS_MEMORY_BUCKET")
        if memory_bucket:
            fs_sync = GCSFilesystemSync(
                bucket_name=memory_bucket,
                local_dir=os.getenv("MEMORY_DIR", "./data/memory"),
                project_id=os.getenv("GCP_PROJECT_ID"),
            )
            logger.info(f"GCS filesystem sync: configured (bucket={memory_bucket})")
        else:
            logger.warning(
                "MEMORY_BACKEND=gcs but GCS_MEMORY_BUCKET is not set — "
                "filesystem sync disabled"
            )

    # Step 3b: Load Layer 0 identity and context files
    _soul_path = Path(soul_file) if soul_file else Path.cwd() / "SOUL.md"
    _tools_path = Path(tools_file) if tools_file else Path.cwd() / "TOOLS.md"
    _memory_dir = os.getenv("MEMORY_DIR", "./data/memory")
    _user_path = Path(_memory_dir) / "USER.md"

    soul_content = _load_text_file(_soul_path, "SOUL")
    user_content = _load_text_file(_user_path, "USER")
    tools_content = _load_text_file(_tools_path, "TOOLS")

    # Resolve IDENTITY.md
    _identity_path = (
        Path(identity_file) if identity_file
        else Path(os.getenv("IDENTITY_FILE", "")) if os.getenv("IDENTITY_FILE")
        else Path.cwd() / "IDENTITY.md"
    )
    identity_content = _load_text_file(_identity_path, "IDENTITY")

    # Load INDEX.md from memory dir (always load — agent needs memory map on every call)
    _index_path = Path(_memory_dir) / "INDEX.md"
    index_content = _load_text_file(_index_path, "INDEX")

    # Resolve all key paths to absolute
    _resolved_memory_dir = str(Path(_memory_dir).resolve())
    _resolved_skills_dir = str(Path(os.getenv("SKILLS_DIR", "./skills")).resolve())
    resolved_paths: dict[str, str] = {
        "MEMORY_DIR": _resolved_memory_dir,
        "SKILLS_DIR": _resolved_skills_dir,
        "INDEX_MD": str(_index_path.resolve()),
        "USER_MD": str((_index_path.parent / "USER.md").resolve()),
    }
    if _soul_path.exists():
        resolved_paths["SOUL_FILE"] = str(_soul_path.resolve())
    if _identity_path.exists():
        resolved_paths["IDENTITY_FILE"] = str(_identity_path.resolve())

    # Step 3c: Token budget logging and warning
    soul_tokens = _estimate_tokens(soul_content)
    user_tokens = _estimate_tokens(user_content)
    tools_tokens = _estimate_tokens(tools_content)
    identity_tokens = _estimate_tokens(identity_content)
    index_tokens = _estimate_tokens(index_content)
    total_new_tokens = soul_tokens + user_tokens + tools_tokens + identity_tokens + index_tokens

    if soul_tokens > 500:
        logger.warning(
            "SOUL.md exceeds token budget",
            extra={"soul_tokens": soul_tokens, "budget": 500}
        )
    if identity_tokens > 800:
        logger.warning(
            "IDENTITY.md exceeds token budget",
            extra={"identity_tokens": identity_tokens, "budget": 800}
        )
    if index_tokens > 1000:
        logger.warning(
            "INDEX.md exceeds token budget",
            extra={"index_tokens": index_tokens, "budget": 1000}
        )

    logger.info(
        "Layer 0 token usage",
        extra={
            "soul_tokens": soul_tokens,
            "user_tokens": user_tokens,
            "tools_tokens": tools_tokens,
            "identity_tokens": identity_tokens,
            "index_tokens": index_tokens,
            "total_new_tokens": total_new_tokens,
        }
    )

    # Step 4: Compose 3-layer system prompt
    full_system_prompt = compose_system_prompt(
        skills_manifest=skills_manifest,
        skills_dirs=skills,
        user_system_prompt=system_prompt,
        has_scheduler=scheduler is not None,
        has_memory=store is not None,
        has_backend=backend is not None,
        has_filesystem_memory=fs_sync is not None,
        soul_content=soul_content,
        user_content=user_content,
        tools_content=tools_content,
        identity_content=identity_content,
        index_content=index_content,
        resolved_paths=resolved_paths,
        memory_dir=_resolved_memory_dir,
    )

    logger.info(
        "Composed system prompt",
        extra={
            "component": "deepagent",
            "has_scheduler": scheduler is not None,
            "has_memory": store is not None,
            "has_backend": backend is not None,
            "has_filesystem_memory": fs_sync is not None,
            "num_skills": len(skills) if skills else 0,
        }
    )

    # Step 5: Configure middleware
    middleware = []
    if extra_middleware:
        middleware.extend(list(extra_middleware))

    # Note: SummarizationMiddleware is added by default by create_deep_agent,
    # so we don't need to add it manually. The summarization_trigger and
    # summarization_keep parameters are not currently configurable via
    # create_deep_agent API, so we accept them but don't use them for now.

    # SubAgentMiddleware and SkillsMiddleware are now handled natively by
    # create_deep_agent() via the `subagents` and `skills` parameters below.
    # This ensures each subagent gets its own SkillsMiddleware, FilesystemMiddleware,
    # and SummarizationMiddleware wired correctly by the deepagents package.

    if checkpointer is None:
        checkpoint_backend = os.getenv("CHECKPOINT_BACKEND", "memory")
        if checkpoint_backend == "firestore":
            from .firestore_checkpointer import FirestoreCheckpointSaver  # noqa: PLC0415
            project_id = os.getenv("GCP_PROJECT_ID") or os.getenv("VERTEX_AI_PROJECT_ID")
            if not project_id:
                raise ValueError(
                    "CHECKPOINT_BACKEND=firestore requires GCP_PROJECT_ID or VERTEX_AI_PROJECT_ID"
                )
            checkpointer = FirestoreCheckpointSaver(project_id=project_id)
            logger.info("Using FirestoreCheckpointSaver: project=%s", project_id)
        else:
            checkpointer = InMemorySaver()
            logger.info("Using InMemorySaver for conversation persistence (in-memory only)")

    # Step 5b: Register HeartbeatHandler if heartbeat config provided
    _heartbeat_handler_cls = None
    try:
        from .scheduler.handlers import HeartbeatHandler as _HBCls
        _heartbeat_handler_cls = _HBCls
    except ImportError:
        pass

    if heartbeat is not None:
        if scheduler is None:
            logger.warning(
                "heartbeat config provided but scheduler is None — skipping HeartbeatHandler registration"
            )
        elif _heartbeat_handler_cls is None:
            logger.warning("HeartbeatHandler not available (scheduler.handlers not importable)")
        else:
            hb_handler = _heartbeat_handler_cls(agent=None, config=heartbeat)
            scheduler.register_handler("heartbeat", hb_handler)
            logger.info("Registered HeartbeatHandler with scheduler")

    # Step 5c: Prepare VoiceHandler if voice config provided
    _voice_handler_cls = None
    try:
        from ..voice.handler import VoiceHandler as _VHCls
        _voice_handler_cls = _VHCls
    except ImportError:
        pass

    voice_handler = None
    if voice is not None:
        if _voice_handler_cls is None:
            logger.warning("VoiceHandler not available (voice module not importable)")
        else:
            voice_handler = _voice_handler_cls(config=voice)
            logger.info("Created VoiceHandler instance")

    # Step 5d: Merge subagent middleware into each dict subagent (not compiled runnables)
    merged_subagents = subagents
    if subagent_middleware and subagents:
        smw = list(subagent_middleware)
        merged: list = []
        for spec in subagents:
            if isinstance(spec, dict) and "runnable" not in spec:
                existing = list(spec.get("middleware", []))
                merged.append({**spec, "middleware": existing + smw})
            else:
                merged.append(spec)
        merged_subagents = merged

    # Step 6: Call create_deep_agent with all params
    agent = create_deep_agent(
        model=model,
        tools=all_tools,
        system_prompt=full_system_prompt,
        middleware=middleware,
        backend=backend,
        store=store,
        checkpointer=checkpointer,
        skills=skills,          # SkillsMiddleware wired per-agent by deepagents
        subagents=merged_subagents,  # SubAgentMiddleware + per-subagent stacks by deepagents
    )

    # Attach fs_sync to agent so callers can run startup sync via FastAPI lifespan
    if fs_sync is not None:
        agent.fs_sync = fs_sync
        logger.info(
            "GCS filesystem sync: attached to agent "
            "(wire agent.fs_sync.sync_from_gcs() to FastAPI lifespan startup, "
            "agent.fs_sync.close() to lifespan shutdown)"
        )
    else:
        agent.fs_sync = None

    # Attach voice_handler to agent
    agent.voice_handler = voice_handler

    if workspace_settings is not None:
        agent.workspace_settings = workspace_settings

    logger.info(
        "Deep agent created",
        extra={
            "component": "deepagent",
            "num_tools": len(all_tools),
            "num_middleware": len(middleware),
            "has_filesystem_sync": fs_sync is not None,
        }
    )

    return agent


def _generate_skills_manifest(skills_dirs: list[str]) -> str:
    """Generate skills manifest by reading SKILL.md frontmatter.

    Walks each skills directory, finds SKILL.md files, parses YAML frontmatter,
    and extracts name and description to build a formatted manifest.

    Args:
        skills_dirs: List of directory paths to scan for skills

    Returns:
        Formatted skills manifest string (one skill per line)

    Example:
        >>> manifest = _generate_skills_manifest(["./skills/"])
        >>> print(manifest)
        - file-ops: File operations (read, write, list)
        - search-web: Search the web for information
    """
    skills = []

    for skills_dir in skills_dirs:
        dir_path = Path(skills_dir)

        if not dir_path.exists():
            logger.warning(f"Skills directory not found: {skills_dir}")
            continue

        if not dir_path.is_dir():
            logger.warning(f"Skills path is not a directory: {skills_dir}")
            continue

        # Walk the directory
        for skill_path in dir_path.iterdir():
            if not skill_path.is_dir():
                continue

            skill_md = skill_path / "SKILL.md"
            if not skill_md.exists():
                logger.debug(f"Skipping {skill_path.name} - no SKILL.md found")
                continue

            # Parse SKILL.md frontmatter
            metadata = _parse_skill_frontmatter(skill_md)
            if not metadata:
                continue

            name = metadata.get("name")
            description = metadata.get("description", "")

            if not name:
                logger.warning(f"Skill {skill_path.name} missing 'name' in frontmatter")
                continue

            skills.append(f"- {name}: {description}")
            logger.debug(f"Loaded skill: {name}")

    if not skills:
        return "No skills available."

    return "\n".join(skills)


def _parse_skill_frontmatter(skill_md_path: Path) -> dict | None:
    """Parse YAML frontmatter from SKILL.md file.

    SKILL.md format:
        ---
        name: skill-name
        description: "Description"
        ---

        # Skill Documentation
        ...

    Args:
        skill_md_path: Path to SKILL.md file

    Returns:
        Parsed metadata dict or None if parsing fails
    """
    try:
        with open(skill_md_path) as f:
            content = f.read()

        # Extract YAML frontmatter between --- delimiters
        if not content.startswith("---"):
            logger.error(f"SKILL.md missing frontmatter: {skill_md_path}")
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            logger.error(f"SKILL.md malformed frontmatter: {skill_md_path}")
            return None

        frontmatter = parts[1].strip()
        metadata = yaml.safe_load(frontmatter)

        return metadata

    except yaml.YAMLError as e:
        logger.error(f"Failed to parse YAML in {skill_md_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing {skill_md_path}: {e}")
        return None


def _create_schedule_task_tool(scheduler) -> BaseTool:
    """Create schedule_task tool for the scheduler.

    This tool allows the agent to schedule background jobs using cron expressions.

    Args:
        scheduler: CronScheduler instance

    Returns:
        LangChain tool function
    """
    from datetime import datetime

    from langchain_core.tools import tool
    from pydantic import BaseModel, ConfigDict

    class JobPayload(BaseModel):
        """Flexible job payload — accepts any key/value pairs."""
        model_config = ConfigDict(extra='allow')

        @classmethod
        def model_json_schema(cls, **kwargs):
            schema = super().model_json_schema(**kwargs)
            schema.pop("additionalProperties", None)
            return schema

    @tool
    async def schedule_task(
        job_type: str,
        schedule_at_iso: str,
        payload: JobPayload,
    ) -> str:
        """Schedule a background task to run at a specific time.

        Use this to schedule jobs for future execution (e.g., posting content,
        sending reminders, running reports).

        Args:
            job_type: Type of job (e.g., "post_content", "send_reminder")
            schedule_at_iso: When to run the job (ISO 8601 datetime string)
            payload: Job-specific data (flexible key/value pairs)

        Returns:
            Success message with job ID

        Example:
            >>> # Schedule a post for tomorrow at 9am
            >>> await schedule_task(
            ...     job_type="post_content",
            ...     schedule_at_iso="2024-02-14T09:00:00Z",
            ...     payload={"platform": "x", "content": "Hello world"}
            ... )
        """
        try:
            schedule_at = datetime.fromisoformat(schedule_at_iso.replace("Z", "+00:00"))
            # Ensure timezone-aware datetime
            if schedule_at.tzinfo is None:
                schedule_at = schedule_at.replace(tzinfo=UTC)
                logger.warning(
                    f"Received timezone-naive datetime '{schedule_at_iso}', "
                    f"assuming UTC: {schedule_at.isoformat()}"
                )
        except ValueError as e:
            return f"Error: Invalid ISO 8601 datetime format: {e}"

        job_id = await scheduler.schedule_job(
            job_type=job_type,
            schedule_at=schedule_at,
            payload=payload.model_dump(),
        )

        return f"Task scheduled successfully. Job ID: {job_id}"

    return schedule_task


def _load_text_file(path: "str | Path", label: str) -> str:
    """Read file content; return '' and log DEBUG if missing. Never raises.

    Args:
        path: Path to the file to read
        label: Human-readable label for logging (e.g., 'SOUL', 'USER', 'TOOLS')

    Returns:
        File content as string, or empty string if file not found or unreadable
    """
    try:
        p = Path(path)
        if not p.exists():
            logger.debug("File not found: label=%s path=%s", label, str(path))
            return ""
        content = p.read_text(encoding="utf-8")
        logger.debug("Loaded file: label=%s path=%s chars=%d", label, str(path), len(content))
        return content
    except Exception as e:
        logger.debug("Failed to load file: label=%s path=%s error=%s", label, str(path), str(e))
        return ""


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: len(text) // 4."""
    return len(text) // 4
