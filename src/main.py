"""
Main application entry point for Emonk.

Wires all components together with LangChain v1 create_agent:
- Gateway → build_agent() → LangGraph
- ChatGoogleGenerativeAI on Vertex (Gemini 2.5 Flash) → LangChain tools
- GCSStore for long-term memory

Run locally:
    python -m src.main

Run with uvicorn:
    uvicorn src.main:app --reload --port 8080
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env FIRST (before any imports that read env vars)
load_dotenv()

from google.cloud import aiplatform  # noqa: E402
from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

from deepagents.backends.local_shell import LocalShellBackend  # noqa: E402

from src.core.agent import build_agent  # noqa: E402
from src.core.deepagent import build_deep_agent  # noqa: E402
from src.core.config import load_bot_config, get_subagent_configs  # noqa: E402
from src.core.store import GCSStore, create_search_memory_tool  # noqa: E402
from src.core.scheduler import create_storage  # noqa: E402
from src.core.terminal import TerminalExecutor  # noqa: E402
from src.gateway import server  # noqa: E402
from src.skills.executor import SkillsEngine  # noqa: E402
from src.skills.loader import SkillLoader  # noqa: E402

logger = logging.getLogger(__name__)


def validate_env_vars() -> None:
    """Validate required environment variables.
    
    Raises:
        RuntimeError: If any required env var is missing
    """
    # Always required
    required_vars = ["ALLOWED_USERS"]
    
    # Check if using GCP-specific features
    memory_backend = os.getenv("MEMORY_BACKEND", "local")
    model_provider = os.getenv("MODEL_PROVIDER", "google_vertexai")
    secrets_provider = os.getenv("SECRETS_PROVIDER", "env")
    
    # GOOGLE_APPLICATION_CREDENTIALS only required in development when using GCP features
    # In production (Cloud Run), the service account is automatically available
    environment = os.getenv("ENVIRONMENT", "development")
    uses_gcp = memory_backend == "gcs" or model_provider == "google_vertexai" or secrets_provider == "gcp_secret_manager"
    
    if environment == "development" and uses_gcp:
        required_vars.append("GOOGLE_APPLICATION_CREDENTIALS")
    
    # VERTEX_AI_PROJECT_ID required if using Vertex AI
    if model_provider == "google_vertexai":
        required_vars.append("VERTEX_AI_PROJECT_ID")
    
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your values, or configure in bot.yaml"
        )
    
    # Validate GOOGLE_APPLICATION_CREDENTIALS file exists (if required and set)
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_path and not Path(creds_path).exists():
        raise RuntimeError(
            f"GOOGLE_APPLICATION_CREDENTIALS file not found: {creds_path}\n"
            f"Download from: https://console.cloud.google.com/iam-admin/serviceaccounts"
        )
    
    logger.info("✅ Environment variables validated")


def load_skills_as_tools(skills_dir: str, terminal_executor: TerminalExecutor) -> list:
    """Load skills as LangChain tools.
    
    Converts the old subprocess-based skills into LangChain @tool functions.
    For now, this wraps the SkillsEngine but future implementations should
    convert skills to native @tool decorated functions.
    
    Args:
        skills_dir: Directory containing skill definitions
        terminal_executor: TerminalExecutor for running skills
    
    Returns:
        List of LangChain tool objects
    """
    # Create skills engine
    skills_engine = SkillsEngine(terminal_executor, skills_dir=skills_dir)
    
    # Load skill metadata
    loader = SkillLoader(skills_dir)
    skill_metadata = loader.load_skills()
    
    # Convert each skill to a LangChain tool
    tools = []
    for skill_name, metadata in skill_metadata.items():
        # Create a closure to capture skill_name for each tool
        def make_tool(name: str, desc: str):
            def skill_tool(**kwargs) -> str:
                """Execute skill."""
                # Execute skill via the engine
                import asyncio
                result = asyncio.run(skills_engine.execute_skill(name, kwargs))
                if result.success:
                    return result.output
                else:
                    return f"Error: {result.error}"
            
            # Use StructuredTool.from_function to explicitly set name and description
            return StructuredTool.from_function(
                func=skill_tool,
                name=name,
                description=desc,
            )
        
        tools.append(make_tool(skill_name, metadata.get("description", "No description")))
    
    logger.info(f"✅ Loaded {len(tools)} skills as LangChain tools")
    return tools


def _load_prompt_file(path: str) -> str:
    """Load a system prompt from a file path, returning empty string on miss."""
    p = Path(path)
    if p.exists():
        return p.read_text()
    logger.warning("Prompt file not found: %s", path)
    return ""


def create_app():
    """Create FastAPI app with LangChain v1 agent.
    
    Returns:
        FastAPI app ready to run
        
    Raises:
        RuntimeError: If configuration is invalid
    """
    # Load bot configuration from bot.yaml (with defaults)
    load_bot_config()
    
    # Validate configuration
    validate_env_vars()
    
    # Initialize Vertex AI (must happen before creating the chat model)
    project_id = os.getenv("VERTEX_AI_PROJECT_ID")
    location = os.getenv("VERTEX_AI_LOCATION", "us-central1")
    thinking_budget = int(os.getenv("MODEL_THINKING_BUDGET", "-1"))

    logger.info(f"Initializing Vertex AI: project={project_id}, location={location}")
    aiplatform.init(project=project_id, location=location)

    # Create Vertex AI chat model (Gemini 2.5 Flash) via langchain-google-genai
    model = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0.7,
        max_output_tokens=8192,
        thinking_budget=thinking_budget,
        vertexai=True,
        project=project_id,
        location=location,
    )
    logger.info("✅ Chat model created (Gemini 2.5 Flash)")
    
    # Create Terminal Executor (for legacy subprocess-based skills / fallback path)
    terminal_executor = TerminalExecutor()
    logger.info("✅ Terminal Executor created")

    # Create LocalShellBackend — gives the deep agent native read_file / execute
    # tools via FilesystemMiddleware.  Inherits the full process environment so
    # skills can access all credentials already set in os.environ.
    backend = LocalShellBackend(root_dir=Path.cwd(), inherit_env=True)
    logger.info("✅ LocalShellBackend created (root_dir=%s)", Path.cwd())

    # Load skills as LangChain tools (kept for the build_agent() fallback path)
    skills_dir = os.getenv("SKILLS_DIR", "./skills")
    tools = load_skills_as_tools(skills_dir, terminal_executor)
    
    # Create memory store based on configured backend
    memory_backend = os.getenv("MEMORY_BACKEND", "local")
    
    store = None
    if memory_backend == "gcs":
        gcs_bucket = os.getenv("GCS_MEMORY_BUCKET")
        if not gcs_bucket:
            logger.warning("⚠️  MEMORY_BACKEND is 'gcs' but GCS_MEMORY_BUCKET not set, falling back to no memory store")
        else:
            store = GCSStore(bucket_name=gcs_bucket, project_id=project_id)
            logger.info(f"✅ GCS Store created (bucket={gcs_bucket})")
            
            # Add search_memory tool
            search_tool = create_search_memory_tool(store)
            tools.append(search_tool)
            logger.info("✅ search_memory tool added")
    else:
        logger.info(f"Memory backend set to '{memory_backend}' - no persistent memory store")
    
    # Create Scheduler Storage Backend
    memory_dir = os.getenv("MEMORY_DIR", "./data/memory")
    scheduler_storage_type = os.getenv("SCHEDULER_STORAGE", "json")  # json or firestore
    scheduler_storage = None
    scheduler = None
    
    if scheduler_storage_type == "firestore":
        scheduler_storage = create_storage("firestore", project_id=project_id)
        logger.info("✅ Scheduler storage: Firestore")
    else:
        scheduler_storage = create_storage("json", memory_dir=Path(memory_dir))
        logger.info("✅ Scheduler storage: JSON files")
    
    # Create scheduler instance if storage is available
    if scheduler_storage:
        from src.core.scheduler import CronScheduler
        scheduler = CronScheduler(
            agent_state=store,
            check_interval_seconds=10,
            storage=scheduler_storage,
        )
    
    # Try to build agent with build_deep_agent(), fall back to build_agent()
    try:
        # Check if skills directory exists
        skills_list = None
        if Path(skills_dir).exists():
            skills_list = [skills_dir]
            logger.info(f"✅ Skills directory found: {skills_dir}")

        # Build subagent specs from bot.yaml `subagents:` section.
        # Returns None (not an empty list) when no subagents are configured so
        # create_deep_agent() can distinguish "no subagents" from "zero subagents".
        subagent_configs = get_subagent_configs()
        subagent_specs = None
        if subagent_configs:
            subagent_specs = []
            for cfg in subagent_configs:
                spec: dict = {
                    "name": cfg.name,
                    "description": cfg.description,
                    "system_prompt": (
                        _load_prompt_file(cfg.prompt_file)
                        if cfg.prompt_file
                        else f"You are the {cfg.name} specialist."
                    ),
                    "skills": cfg.skills,
                }
                if cfg.model:
                    spec["model"] = cfg.model
                subagent_specs.append(spec)
            logger.info("✅ Built %d subagent spec(s)", len(subagent_specs))

        agent = build_deep_agent(
            model=model,
            tools=tools,
            system_prompt="",  # Default, can be customized per-deployment
            skills=skills_list,
            backend=backend,
            subagents=subagent_specs,
            store=store,
            scheduler=scheduler,
        )
        logger.info("✅ Agent built with build_deep_agent()")
    except Exception as e:
        logger.warning(f"⚠️  build_deep_agent() failed: {e}. Falling back to build_agent()")
        # Fall back to old build_agent()
        agent = build_agent(
            model=model,
            tools=tools,
            user_system_prompt="",  # Default, can be customized per-deployment
            middleware=None,  # Uses default middleware (summarization + session summary)
            checkpointer=None,  # Uses InMemorySaver by default
            store=store,
            scheduler_storage=scheduler_storage,
        )
        logger.info("✅ Agent built with build_agent() (fallback)")
    
    # Note: Scheduler will be started by the application startup event
    # (see src/gateway/server.py for @app.on_event("startup"))
    # We don't start it here to avoid event loop issues during testing
    
    # Inject agent into Gateway
    server.agent_core = agent
    logger.info("✅ Agent injected into Gateway")
    
    # Return FastAPI app
    return server.app


# Create app instance (for uvicorn to import)
# Only create if not in test environment
if os.getenv("PYTEST_CURRENT_TEST"):
    # In test environment - tests will call create_app() manually
    app = None
else:
    # In production/development - create app immediately
    app = create_app()


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "INFO").lower()
    
    print("=" * 60)
    print("🚀 Starting Monkey-Bot with LangChain v1")
    print("=" * 60)
    print(f"Port: {port}")
    print(f"Log level: {log_level}")
    print(f"Allowed users: {os.getenv('ALLOWED_USERS', 'NOT SET')}")
    print(f"Vertex AI Project: {os.getenv('VERTEX_AI_PROJECT_ID', 'NOT SET')}")
    print(f"GCS Enabled: {os.getenv('GCS_ENABLED', 'false')}")
    print(f"GCS Bucket: {os.getenv('GCS_MEMORY_BUCKET', 'NOT SET')}")
    print("=" * 60)
    print()
    
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=port,
        log_level=log_level,
        reload=False,  # Disable reload for production
    )
