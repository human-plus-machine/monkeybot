"""Process-level bootstrap before the FastAPI app is constructed."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from monkeybot.core.layout import AgentLayout, bootstrap_agent_layout
from monkeybot.core.logging_utils import kv
from monkeybot.core.tools.sandbox_executor import SandboxConfig

logger = logging.getLogger(__name__)
# Process-local: each gateway worker emits one startup layout event.
_startup_layout_logged = False


def ensure_gateway_runtime_env() -> AgentLayout:
    """Initialize the canonical agent-root layout before gateway imports use env."""
    return bootstrap_agent_layout()


def _storage_kind(uri: str) -> str:
    """Return a redacted storage backend label (scheme and optional host only)."""
    if "://" not in uri:
        return "unknown"
    scheme, remainder = uri.split("://", 1)
    if scheme in {"sqlite", "local"}:
        return scheme
    host = remainder.split("@")[-1].split("/")[0]
    return f"{scheme}://{host}" if host else scheme


def _browser_mode(mcp_config_path: Path) -> str:
    try:
        doc = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        servers = doc.get("mcpServers", {}) if isinstance(doc, dict) else {}
        browser = servers.get("browser", {}) if isinstance(servers, dict) else {}
        return "enabled" if isinstance(browser, dict) and browser.get("enabled") else "disabled"
    except (OSError, json.JSONDecodeError):
        return "unconfigured"


def log_gateway_startup(layout: AgentLayout, *, force: bool = False) -> None:
    """Emit one redacted layout event after the gateway logger is configured."""
    global _startup_layout_logged
    if _startup_layout_logged and not force:
        return
    sandbox = SandboxConfig.from_env()
    workspace_mode = "ephemeral" if str(layout.workspace_root).startswith("/tmp/") else "local"
    fingerprint = hashlib.sha256(str(layout.agent_root).encode("utf-8")).hexdigest()[:12]
    logger.info(
        "gateway_startup_layout %s",
        kv(
            config_source="configured" if layout.config_path else "defaults",
            agent_root_fingerprint=fingerprint,
            workspace_mode=workspace_mode,
            db_backend=_storage_kind(layout.db_url),
            memory_backend=_storage_kind(layout.memory_storage_uri),
            sandbox_mode="shared-filesystem" if sandbox.shared_filesystem else "remote-compute-only",
            sandbox_enabled=sandbox.enabled,
            sandbox_image=sandbox.image,
            browser_mode=_browser_mode(layout.mcp_config_path),
        ),
    )
    _startup_layout_logged = True
