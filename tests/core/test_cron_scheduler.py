"""Tests for cron scheduler."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from core.scheduler.cron import CronScheduler
from core.scheduler.storage import JobStorage


class MockAgentState:
    """Mock agent state for testing."""
    
    def __init__(self, tmp_path: Path):
        self.memory_dir = tmp_path / "data" / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)


class TrackingStorage(JobStorage):
    """Storage that records whether scheduler execution uses bulk replacement."""

    def __init__(self):
        self.jobs = []
        self.save_jobs_calls = 0
        self.save_job_calls = 0

    async def load_jobs(self):
        return self.jobs

    async def save_jobs(self, jobs):
        self.save_jobs_calls += 1
        self.jobs = list(jobs)

    async def save_job(self, job):
        self.save_job_calls += 1
        job_id = job["id"]
        for index, existing in enumerate(self.jobs):
            if existing.get("id") == job_id:
                self.jobs[index] = dict(job)
                break
        else:
            self.jobs.append(dict(job))

    async def claim_job(self, job_id, lease_duration_seconds=300):
        return True

    async def release_job(self, job_id):
        pass


@pytest.fixture
def mock_agent_state(tmp_path):
    """Create mock agent state with temp directory."""
    return MockAgentState(tmp_path)


@pytest.fixture
def scheduler(mock_agent_state):
    """Create scheduler instance for testing."""
    return CronScheduler(mock_agent_state, check_interval_seconds=1)


class TestCronScheduler:
    """Tests for CronScheduler."""

    @pytest.mark.asyncio
    async def test_schedule_job(self, scheduler):
        """Test: Schedule a job for future execution."""
        schedule_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        job_id = await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "x", "content": "Test"}
        )
        
        assert job_id is not None
        assert len(scheduler.jobs) == 1
        assert scheduler.jobs[0]["status"] == "pending"
        assert scheduler.jobs[0]["id"] == job_id
        assert scheduler.jobs[0]["job_type"] == "post_content"

    @pytest.mark.asyncio
    async def test_schedule_job_creates_file(self, scheduler, mock_agent_state):
        """Test: Scheduling a job persists to disk."""
        schedule_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"test": "data"}
        )
        
        jobs_file = mock_agent_state.memory_dir / "scheduler" / "jobs.json"
        assert jobs_file.exists()
        
        # Verify content
        saved_jobs = json.loads(jobs_file.read_text())
        assert len(saved_jobs) == 1
        assert saved_jobs[0]["job_type"] == "post_content"

    @pytest.mark.asyncio
    async def test_get_pending_jobs(self, scheduler):
        """Test: Get all pending jobs."""
        # Schedule 3 jobs
        schedule_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "x"}
        )
        await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "instagram"}
        )
        
        # Mark one as completed
        scheduler.jobs[0]["status"] = "completed"
        
        pending = scheduler.get_pending_jobs()
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_get_job_by_id(self, scheduler):
        """Test: Get job by ID."""
        schedule_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        job_id = await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "x"}
        )
        
        job = scheduler.get_job(job_id)
        assert job is not None
        assert job["id"] == job_id
        assert job["job_type"] == "post_content"

    @pytest.mark.asyncio
    async def test_get_job_returns_none_for_invalid_id(self, scheduler):
        """Test: Getting non-existent job returns None."""
        job = scheduler.get_job("invalid-id")
        assert job is None

    @pytest.mark.asyncio
    async def test_execute_due_job(self, scheduler, monkeypatch):
        """Test: Scheduler executes jobs when time arrives."""
        # Mock the post_content handler
        execute_called = []
        
        async def mock_handler(job):
            execute_called.append(job["payload"])
        
        # Register the handler
        scheduler.register_handler("post_content", mock_handler)
        
        # Schedule job for 1 second from now
        schedule_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        job_id = await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "x", "content": "test"}
        )
        
        # Start scheduler
        task = asyncio.create_task(scheduler.start())
        
        # Wait for job to execute
        await asyncio.sleep(3)
        
        # Stop scheduler
        await scheduler.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        
        # Verify job executed
        job = scheduler.get_job(job_id)
        assert job["status"] == "completed"
        assert len(execute_called) == 1

    @pytest.mark.asyncio
    async def test_job_retry_on_failure(self, scheduler):
        """Test: Failed jobs are retried."""
        schedule_at = datetime.now(timezone.utc)
        job_id = await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "invalid"}  # Will cause error
        )
        
        job = scheduler.get_job(job_id)
        
        # Mock execute to raise error
        async def mock_execute_that_fails(job_dict):
            raise Exception("Test failure")
        
        scheduler.register_handler("post_content", mock_execute_that_fails)
        
        # Execute job
        await scheduler._execute_job(job)
        
        # Check retry scheduled
        assert job["attempts"] == 1
        assert job["status"] == "pending"  # Rescheduled for retry
        
        new_schedule = datetime.fromisoformat(job["schedule_at"])
        assert new_schedule > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_job_fails_after_max_attempts(self, scheduler):
        """Test: Jobs fail permanently after max attempts."""
        schedule_at = datetime.now(timezone.utc)
        job_id = await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "invalid"}
        )
        
        job = scheduler.get_job(job_id)
        job["max_attempts"] = 2
        
        # Mock execute to always fail
        async def mock_execute_that_fails(job_dict):
            raise Exception("Test failure")
        
        scheduler.register_handler("post_content", mock_execute_that_fails)
        
        # Execute twice
        await scheduler._execute_job(job)
        await scheduler._execute_job(job)
        
        # Should be marked as failed
        assert job["status"] == "failed"
        assert job["attempts"] == 2
        assert "error" in job

    @pytest.mark.asyncio
    async def test_job_persistence_across_restarts(self, scheduler, mock_agent_state):
        """Test: Jobs are saved to disk and reloaded."""
        # Create first scheduler and schedule job
        schedule_at = datetime.now(timezone.utc) + timedelta(hours=1)
        job_id = await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=schedule_at,
            payload={"platform": "x"}
        )
        
        # Create new scheduler (simulates restart)
        scheduler2 = CronScheduler(mock_agent_state, check_interval_seconds=1)
        
        # Load jobs explicitly (async operation)
        await scheduler2._load_jobs()
        
        # Should have loaded job from disk
        assert len(scheduler2.jobs) == 1
        assert scheduler2.jobs[0]["id"] == job_id
        assert scheduler2.jobs[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_scheduler_start_stop(self, scheduler):
        """Test: Scheduler can be started and stopped."""
        assert scheduler.running is False
        
        # Start scheduler
        task = asyncio.create_task(scheduler.start())
        await asyncio.sleep(0.5)
        
        assert scheduler.running is True
        
        # Stop scheduler
        await scheduler.stop()
        assert scheduler.running is False
        
        # Clean up task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_unknown_job_type_fails_job(self, scheduler):
        """Test: Unknown job type marks job as failed."""
        schedule_at = datetime.now(timezone.utc)
        job_id = await scheduler.schedule_job(
            job_type="unknown_type",
            schedule_at=schedule_at,
            payload={}
        )
        
        job = scheduler.get_job(job_id)
        job["max_attempts"] = 1  # Fail immediately
        
        # Execute job - should fail (no handler registered)
        await scheduler._execute_job(job)
        
        # Verify job marked as failed
        assert job["status"] == "failed"
        assert "No handler registered for job type" in job["error"]

    @pytest.mark.asyncio
    async def test_run_tick_uses_single_job_saves(self, mock_agent_state):
        """Due-job execution should not bulk-replace scheduler storage."""
        storage = TrackingStorage()
        scheduler = CronScheduler(mock_agent_state, check_interval_seconds=1, storage=storage)

        async def mock_handler(job):
            job["payload"]["handled"] = True

        scheduler.register_handler("post_content", mock_handler)
        await scheduler.schedule_job(
            job_type="post_content",
            schedule_at=datetime.now(timezone.utc),
            payload={"platform": "x"},
        )
        storage.save_jobs_calls = 0

        result = await scheduler.run_tick()

        assert result["jobs_executed"] == 1
        assert result["jobs_succeeded"] == 1
        assert storage.save_jobs_calls == 0
        assert storage.save_job_calls >= 2
        assert storage.jobs[0]["status"] == "completed"
