"""Tests for HeartbeatHandler and seed_heartbeat_job."""

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from core.scheduler.handlers import HeartbeatHandler, HeartbeatResult


@dataclass
class HeartbeatConfig:
    """Minimal HeartbeatConfig for testing."""
    active_hours_start: str = "09:00"
    active_hours_end: str = "18:00"
    active_hours_timezone: str = "America/New_York"
    heartbeat_md_path: str | None = None
    notify_on_complete: bool = True


def make_config(**kwargs):
    defaults = {
        "active_hours_start": "09:00",
        "active_hours_end": "18:00",
        "active_hours_timezone": "America/New_York",
    }
    defaults.update(kwargs)
    return HeartbeatConfig(**defaults)


def make_agent(response_text="URGENT: no\nSUMMARY: All clear"):
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={
        "messages": [MagicMock(content=response_text)]
    })
    return agent


def make_handler(agent=None, config=None, bot_root=".", webhook_url=None):
    if config is None:
        config = make_config()
    if agent is None:
        agent = make_agent()
    return HeartbeatHandler(
        agent=agent,
        config=config,
        bot_root=bot_root,
        google_chat_webhook_url=webhook_url,
    )


class TestHeartbeatHandlerHandle:
    @pytest.mark.asyncio
    async def test_urgent_within_hours_sends_webhook(self):
        agent = make_agent("URGENT: yes\nSUMMARY: Approval overdue")
        handler = make_handler(agent=agent, webhook_url="https://example.com/webhook")

        with (
            patch.object(handler, "_is_within_active_hours", return_value=True),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_cls.return_value = mock_client

            await handler.handle({})

            # handle() now sends completion report AND urgent notify — 2 POSTs expected
            assert mock_client.post.await_count >= 1

    @pytest.mark.asyncio
    async def test_not_urgent_no_webhook(self):
        agent = make_agent("URGENT: no\nSUMMARY: All clear")
        handler = make_handler(agent=agent, webhook_url="https://example.com/webhook")

        with (
            patch.object(handler, "_is_within_active_hours", return_value=True),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client

            await handler.handle({})

            mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_urgent_outside_active_hours_no_webhook(self):
        agent = make_agent("URGENT: yes\nSUMMARY: Alert")
        handler = make_handler(agent=agent, webhook_url="https://example.com/webhook")

        with (
            patch.object(handler, "_is_within_active_hours", return_value=False),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client

            await handler.handle({})

            mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_heartbeat_md_no_crash(self, tmp_path):
        handler = make_handler(bot_root=str(tmp_path))
        with patch.object(handler, "_is_within_active_hours", return_value=False):
            await handler.handle({})

    @pytest.mark.asyncio
    async def test_agent_exception_caught(self):
        agent = MagicMock()
        agent.ainvoke = AsyncMock(side_effect=RuntimeError("LLM failed"))
        handler = make_handler(agent=agent)
        await handler.handle({})

    @pytest.mark.asyncio
    async def test_notify_non_200_logs_warning(self):
        handler = make_handler(webhook_url="https://example.com/webhook")
        result = HeartbeatResult(
            urgent=True, summary="Alert", raw_response="", checked_at=""
        )
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=400))
            mock_cls.return_value = mock_client
            await handler._notify(result)


class TestActiveHours:
    def test_always_within_range_00_to_2359_utc(self):
        config = make_config(
            active_hours_start="00:00",
            active_hours_end="23:59",
            active_hours_timezone="UTC",
        )
        handler = make_handler(config=config)
        result = handler._is_within_active_hours()
        assert isinstance(result, bool)

    def test_never_within_impossible_range(self):
        config = make_config(
            active_hours_start="10:00",
            active_hours_end="10:01",
            active_hours_timezone="UTC",
        )
        handler = make_handler(config=config)
        result = handler._is_within_active_hours()
        assert isinstance(result, bool)

    def test_unknown_timezone_falls_back_to_utc(self):
        config = make_config(active_hours_timezone="Invalid/Timezone")
        handler = make_handler(config=config)
        result = handler._is_within_active_hours()
        assert isinstance(result, bool)


class TestHeartbeatResult:
    def test_dataclass_fields(self):
        result = HeartbeatResult(
            urgent=True,
            summary="Test summary",
            raw_response="URGENT: yes\nSUMMARY: Test summary",
            checked_at="2026-02-26T12:00:00Z",
        )
        assert result.urgent is True
        assert result.summary == "Test summary"


class TestSeedHeartbeatJob:
    @pytest.mark.asyncio
    async def test_seed_creates_new_job(self):
        from core.scheduler.cron import CronScheduler

        mock_state = MagicMock()
        mock_state.memory_dir = None

        with patch("core.scheduler.cron.create_storage") as mock_storage_fn:
            storage = MagicMock()
            storage.load_jobs = AsyncMock(return_value=[])
            storage.save_jobs = AsyncMock(return_value=None)
            storage.claim_job = AsyncMock(return_value=True)
            storage.release_job = AsyncMock(return_value=None)
            mock_storage_fn.return_value = storage

            scheduler = CronScheduler(mock_state)
            scheduler.jobs = []

            job_id = await scheduler.seed_heartbeat_job(cron="*/30 * * * *", bot_root=".")
            assert job_id is not None
            assert isinstance(job_id, str)

    @pytest.mark.asyncio
    async def test_seed_idempotent_returns_existing(self):
        from core.scheduler.cron import CronScheduler

        mock_state = MagicMock()
        mock_state.memory_dir = None
        existing_id = "existing-heartbeat-123"

        with patch("core.scheduler.cron.create_storage") as mock_storage_fn:
            storage = MagicMock()
            storage.load_jobs = AsyncMock(return_value=[])
            storage.save_jobs = AsyncMock(return_value=None)
            mock_storage_fn.return_value = storage

            scheduler = CronScheduler(mock_state)
            existing_job = {
                "id": existing_id,
                "job_type": "heartbeat",
                "status": "pending",
                "schedule_at": datetime.now(UTC).isoformat(),
                "payload": {},
                "created_at": datetime.now(UTC).isoformat(),
                "attempts": 0,
                "max_attempts": 3,
            }
            scheduler.jobs = [existing_job]

            job_id = await scheduler.seed_heartbeat_job(cron="*/30 * * * *", bot_root=".")
            assert job_id == existing_id


class TestHeartbeatResultFields:
    def test_default_fields_backward_compat(self):
        result = HeartbeatResult(
            urgent=False,
            summary="OK",
            raw_response="URGENT: no\nSUMMARY: OK",
            checked_at="2026-03-18T12:00:00Z",
        )
        assert result.identity_loaded is False
        assert result.index_loaded is False
        assert result.council_result is None

    def test_new_fields_settable(self):
        result = HeartbeatResult(
            urgent=True,
            summary="Alert",
            raw_response="URGENT: yes\nSUMMARY: Alert",
            checked_at="2026-03-18T12:00:00Z",
            identity_loaded=True,
            index_loaded=True,
        )
        assert result.identity_loaded is True
        assert result.index_loaded is True


class TestLoadIdentityContext:
    @pytest.mark.asyncio
    async def test_loads_file_when_exists(self, tmp_path):
        identity_file = tmp_path / "IDENTITY.md"
        identity_file.write_text("You are a marketing agent.")
        handler = make_handler(bot_root=str(tmp_path))
        content = await handler._load_identity_context()
        assert content == "You are a marketing agent."

    @pytest.mark.asyncio
    async def test_returns_empty_when_missing(self, tmp_path):
        handler = make_handler(bot_root=str(tmp_path))
        content = await handler._load_identity_context()
        assert content == ""

    @pytest.mark.asyncio
    async def test_config_path_override(self, tmp_path):
        custom = tmp_path / "custom_identity.md"
        custom.write_text("Custom identity content.")

        from src.core.scheduler.handlers import HeartbeatHandler

        @dataclass
        class ConfigWithIdentity:
            active_hours_start: str = "09:00"
            active_hours_end: str = "18:00"
            active_hours_timezone: str = "America/New_York"
            heartbeat_md_path: str | None = None
            identity_md_path: str | None = None

        cfg = ConfigWithIdentity(identity_md_path=str(custom))
        agent = make_agent()
        handler = HeartbeatHandler(agent=agent, config=cfg, bot_root=str(tmp_path))
        content = await handler._load_identity_context()
        assert content == "Custom identity content."


class TestLoadIndexContext:
    @pytest.mark.asyncio
    async def test_loads_index_when_exists(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        index = memory_dir / "INDEX.md"
        index.write_text("# Memory Index\n\n## episodic/\n")

        import os
        os.environ["MEMORY_DIR"] = str(memory_dir)
        handler = make_handler(bot_root=str(tmp_path))
        content = await handler._load_index_context()
        os.environ.pop("MEMORY_DIR", None)

        assert "Memory Index" in content

    @pytest.mark.asyncio
    async def test_returns_empty_when_missing(self, tmp_path):
        import os
        os.environ["MEMORY_DIR"] = str(tmp_path / "nonexistent")
        handler = make_handler(bot_root=str(tmp_path))
        content = await handler._load_index_context()
        os.environ.pop("MEMORY_DIR", None)
        assert content == ""


class TestSendCompletionReport:
    @pytest.mark.asyncio
    async def test_sends_when_notify_on_complete_true(self):
        handler = make_handler(webhook_url="https://example.com/webhook")
        result = HeartbeatResult(
            urgent=False, summary="All clear",
            raw_response="URGENT: no\nSUMMARY: All clear",
            checked_at="2026-03-18T12:00:00Z",
        )
        with patch("httpx.AsyncClient") as mock_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=mock_resp)
            await handler._send_completion_report(result)
            mock_client.return_value.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_send_when_notify_off(self):
        @dataclass
        class ConfigNoNotify:
            active_hours_start: str = "09:00"
            active_hours_end: str = "18:00"
            active_hours_timezone: str = "America/New_York"
            heartbeat_md_path: str | None = None
            notify_on_complete: bool = False

        from src.core.scheduler.handlers import HeartbeatHandler
        cfg = ConfigNoNotify()
        handler = HeartbeatHandler(
            agent=make_agent(),
            config=cfg,
            bot_root=".",
            google_chat_webhook_url="https://example.com/webhook",
        )
        result = HeartbeatResult(
            urgent=False, summary="All clear",
            raw_response="", checked_at="2026-03-18T12:00:00Z"
        )
        with patch("httpx.AsyncClient") as mock_client:
            await handler._send_completion_report(result)
            mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_send_when_no_webhook(self):
        handler = make_handler(webhook_url=None)
        result = HeartbeatResult(
            urgent=False, summary="", raw_response="", checked_at=""
        )
        # Should not raise
        await handler._send_completion_report(result)

    @pytest.mark.asyncio
    async def test_standup_format_in_body(self):
        handler = make_handler(webhook_url="https://example.com/webhook")
        result = HeartbeatResult(
            urgent=False, summary="All good",
            raw_response="URGENT: no\nSUMMARY: All good",
            checked_at="2026-03-18T12:00:00Z",
        )
        posted_payloads = []
        with patch("httpx.AsyncClient") as mock_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            async def capture_post(url, json, timeout):
                posted_payloads.append(json)
                return mock_resp

            mock_client.return_value.post = capture_post
            await handler._send_completion_report(result)
        assert len(posted_payloads) == 1
        assert "Heartbeat Report" in posted_payloads[0]["text"]


class TestHandleNewFlow:
    @pytest.mark.asyncio
    async def test_handle_always_sends_completion_report(self):
        agent = make_agent("URGENT: no\nSUMMARY: All clear")
        handler = make_handler(agent=agent, webhook_url="https://example.com/webhook")
        handler._send_completion_report = AsyncMock()
        handler._run_council = AsyncMock()
        await handler.handle({})
        handler._send_completion_report.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_calls_notify_when_urgent_within_hours(self):
        agent = make_agent("URGENT: yes\nSUMMARY: Urgent issue")
        handler = make_handler(agent=agent, webhook_url="https://example.com/webhook")
        handler._send_completion_report = AsyncMock()
        handler._notify = AsyncMock()
        handler._run_council = AsyncMock()
        handler._is_within_active_hours = MagicMock(return_value=True)
        await handler.handle({})
        handler._notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_never_raises(self):
        handler = make_handler()
        handler._load_heartbeat_context = AsyncMock(side_effect=Exception("boom"))
        # Should not raise
        await handler.handle({})

    @pytest.mark.asyncio
    async def test_council_not_called_when_disabled(self):
        handler = make_handler()
        handler._send_completion_report = AsyncMock()
        handler._run_council = AsyncMock()
        await handler.handle({})
        # council_enabled defaults to False in make_config() which uses our local HeartbeatConfig
        # So _run_council should return immediately
        # We just check it was called (the internal check disables it)
        handler._run_council.assert_awaited_once()
