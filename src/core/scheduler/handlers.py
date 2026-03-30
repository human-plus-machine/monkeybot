"""Scheduler job handlers for monkey-bot.

Provides HeartbeatHandler — a scheduler handler that periodically invokes
the agent to check workspace health and notify on urgent findings.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from ..config import HeartbeatConfig

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatResult:
    """Result from a single heartbeat check."""

    urgent: bool
    summary: str
    raw_response: str
    checked_at: str  # ISO8601
    identity_loaded: bool = False
    index_loaded: bool = False
    council_result: object | None = None


class HeartbeatHandler:
    """Registered with CronScheduler for job_type='heartbeat'.

    Invokes the agent with workspace health check instructions,
    parses urgency from the response, and sends a Google Chat
    notification if the result is urgent and within active hours.
    """

    def __init__(
        self,
        agent: object,
        config: HeartbeatConfig,
        bot_root: str | Path = ".",
        google_chat_webhook_url: str | None = None,
    ) -> None:
        """Initialize HeartbeatHandler.

        Args:
            agent: Compiled LangGraph agent with ainvoke() method
            config: HeartbeatConfig with schedule and active hours settings
            bot_root: Root directory to look for HEARTBEAT.md
            google_chat_webhook_url: Optional webhook URL override.
                Falls back to GOOGLE_CHAT_WEBHOOK env var.
        """
        self.agent = agent
        self.config = config
        self.bot_root = Path(bot_root)
        self.webhook_url = google_chat_webhook_url or os.getenv("GOOGLE_CHAT_WEBHOOK")

    async def handle(self, job: dict) -> None:
        """Called by CronScheduler for job_type='heartbeat'. Never raises."""
        try:
            heartbeat_context = await self._load_heartbeat_context()
            identity_context = await self._load_identity_context()
            index_context = await self._load_index_context()

            result = await self._invoke_agent(
                heartbeat_context,
                identity_context=identity_context,
                index_context=index_context,
            )

            # Always send completion report (urgency-independent)
            await self._send_completion_report(result)

            # Legacy urgent notification
            if result.urgent and self._is_within_active_hours():
                await self._notify(result)

            # Council runs after report; exits immediately when disabled
            await self._run_council(result)

        except Exception as e:
            logger.error(
                "HeartbeatHandler.handle() failed: %s",
                str(e),
                extra={"error": str(e)},
            )

    def _is_within_active_hours(self) -> bool:
        """Return True if current time is within config active hours.

        Returns:
            True if current local time is between active_hours_start and
            active_hours_end
        """
        try:
            tz = ZoneInfo(self.config.active_hours_timezone)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Unknown timezone: %s, falling back to UTC",
                self.config.active_hours_timezone,
            )
            tz = ZoneInfo("UTC")

        now_local = datetime.now(tz)

        def parse_hhmm(s: str) -> tuple[int, int]:
            h, m = s.split(":")
            return int(h), int(m)

        start_h, start_m = parse_hhmm(self.config.active_hours_start)
        end_h, end_m = parse_hhmm(self.config.active_hours_end)

        now_minutes = now_local.hour * 60 + now_local.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        return start_minutes <= now_minutes < end_minutes

    async def _load_heartbeat_context(self) -> str:
        """Load HEARTBEAT.md content from bot_root.

        Returns:
            Content of HEARTBEAT.md, or empty string if not found
        """
        hb_path = (
            Path(self.config.heartbeat_md_path)
            if self.config.heartbeat_md_path
            else self.bot_root / "HEARTBEAT.md"
        )
        try:
            if hb_path.exists():
                return hb_path.read_text(encoding="utf-8")
            logger.warning(
                "HEARTBEAT.md not found at %s, using empty context", str(hb_path)
            )
            return ""
        except Exception as e:
            logger.warning("Failed to load HEARTBEAT.md: %s", str(e))
            return ""

    async def _load_identity_context(self) -> str:
        """Load IDENTITY.md. Returns '' on missing or error."""
        path = (
            Path(getattr(self.config, "identity_md_path", None))
            if getattr(self.config, "identity_md_path", None)
            else self.bot_root / "IDENTITY.md"
        )
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
            logger.debug("IDENTITY.md not found at %s", str(path))
            return ""
        except Exception as e:
            logger.warning("Failed to load IDENTITY.md: %s", str(e))
            return ""

    async def _load_index_context(self) -> str:
        """Load INDEX.md from memory dir. Returns '' on missing or error."""
        memory_dir = getattr(self.config, "council_memory_dir", None) or os.getenv(
            "MEMORY_DIR", "./data/memory"
        )
        path = (
            Path(getattr(self.config, "council_index_path", None))
            if getattr(self.config, "council_index_path", None)
            else Path(memory_dir) / "INDEX.md"
        )
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
            logger.debug(
                "INDEX.md not found at %s, agent starts with empty memory index",
                str(path),
            )
            return ""
        except Exception as e:
            logger.warning("Failed to load INDEX.md: %s", str(e))
            return ""

    async def _invoke_agent(
        self,
        heartbeat_context: str,
        identity_context: str = "",
        index_context: str = "",
    ) -> HeartbeatResult:
        """Invoke the agent with heartbeat prompt and parse the response.

        Args:
            heartbeat_context: Content from HEARTBEAT.md
            identity_context: Content from IDENTITY.md
            index_context: Content from INDEX.md

        Returns:
            HeartbeatResult with urgency flag and summary
        """
        now_iso = datetime.now(UTC).isoformat()
        tz_label = self.config.active_hours_timezone

        identity_block = (
            f"[IDENTITY]\n{identity_context}\n\n" if identity_context else ""
        )
        index_block = (
            f"[MEMORY INDEX]\n{index_context}\n\n" if index_context else ""
        )

        prompt = (
            f"[HEARTBEAT CHECK]\n"
            f"Current time: {now_iso} ({tz_label})\n\n"
            f"{identity_block}"
            f"{index_block}"
            f"Your heartbeat instructions:\n{heartbeat_context}\n\n"
            f"Review your workspace, memory, and scheduled jobs.\n"
            f"Respond in this exact format:\n"
            f"URGENT: yes|no\n"
            f"SUMMARY: <one paragraph>"
        )

        result = await self.agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config={"configurable": {"thread_id": "heartbeat"}},
        )

        messages = result.get("messages", [])
        raw = messages[-1].content if messages else ""

        urgent = "urgent: yes" in raw[:500].lower()

        summary = ""
        for line in raw.splitlines():
            if line.upper().startswith("SUMMARY:"):
                summary = line[8:].strip()
                break

        checked_at = datetime.now(UTC).isoformat()
        return HeartbeatResult(
            urgent=urgent,
            summary=summary,
            raw_response=raw,
            checked_at=checked_at,
            identity_loaded=bool(identity_context),
            index_loaded=bool(index_context),
        )

    async def _notify(self, result: HeartbeatResult) -> None:
        """POST heartbeat result to Google Chat webhook.

        Args:
            result: HeartbeatResult to notify about
        """
        if not self.webhook_url:
            logger.warning("No webhook URL configured, skipping notification")
            return

        try:
            import httpx

            payload = {"text": f"🔔 *Heartbeat Alert*\n{result.summary}"}
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.webhook_url, json=payload, timeout=10.0
                )
            if resp.status_code >= 300:
                logger.warning(
                    "Webhook returned non-200: status=%d",
                    resp.status_code,
                    extra={"status_code": resp.status_code},
                )
        except Exception as e:
            logger.warning("Failed to send webhook notification: %s", str(e))

    async def _send_completion_report(self, result: HeartbeatResult) -> None:
        """Send completion report to webhook regardless of urgency.

        Always called after _invoke_agent(). Controlled by config.notify_on_complete
        and config.notify_report_format ('standup' | 'detailed' | 'minimal').
        """
        if not self.webhook_url:
            return
        if not getattr(self.config, "notify_on_complete", True):
            return

        fmt = getattr(self.config, "notify_report_format", "standup")
        if fmt == "detailed":
            body = result.raw_response
        elif fmt == "minimal":
            body = result.summary
        else:  # standup (default)
            status = "🔴 URGENT" if result.urgent else "✅ All clear"
            body = (
                f"{status}\n"
                f"• Summary: {result.summary}\n"
                f"• Identity loaded: {result.identity_loaded}\n"
                f"• Memory index loaded: {result.index_loaded}\n"
                f"• Checked at: {result.checked_at}"
            )

        try:
            import httpx

            payload = {"text": f"🤖 *Heartbeat Report*\n{body}"}
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.webhook_url, json=payload, timeout=10.0
                )
            if resp.status_code >= 300:
                logger.warning(
                    "Completion report webhook returned %d", resp.status_code
                )
        except Exception as e:
            logger.warning("Failed to send completion report: %s", str(e))

    async def _run_council(self, result: HeartbeatResult) -> None:
        """Run LLM Council in background. Catches all errors."""
        if not getattr(self.config, "council_enabled", False):
            return
        try:
            from ..council import LLMCouncil
            from langchain_google_genai import ChatGoogleGenerativeAI

            council_model_name = getattr(
                self.config, "council_model", "gemini-2.0-flash"
            )
            project = (
                os.getenv("GCP_PROJECT_ID")
                or os.getenv("VERTEX_AI_PROJECT_ID")
                or os.getenv("GOOGLE_CLOUD_PROJECT")
            )
            location = (
                os.getenv("VERTEX_AI_LOCATION")
                or os.getenv("GOOGLE_CLOUD_LOCATION")
                or "global"
            )
            council_model = ChatGoogleGenerativeAI(
                model=council_model_name,
                vertexai=True,
                project=project,
                location=location,
            )
            memory_dir = Path(
                getattr(self.config, "council_memory_dir", None)
                or os.getenv("MEMORY_DIR", "./data/memory")
            )
            custom_folders = getattr(self.config, "council_custom_folders", None)
            council = LLMCouncil(
                model=council_model,
                memory_dir=memory_dir,
                custom_folders=custom_folders,
            )
            council_result = await council.run()
            logger.info(
                "LLM Council completed",
                extra={
                    "files_processed": council_result.files_processed,
                    "files_written": council_result.files_written,
                },
            )
            result.council_result = council_result
        except Exception as e:
            logger.error("LLMCouncil run failed: %s", str(e))
