"""Simple in-memory cron scheduler for background jobs.

This module provides a lightweight scheduler for executing background jobs
at specified times. Jobs are persisted to disk and survive agent restarts.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .storage import JobStorage, create_storage

logger = logging.getLogger(__name__)


class CronScheduler:
    """Simple in-memory cron scheduler for background jobs.

    For MVP, this is a simple polling-based scheduler.
    Future: Use APScheduler or similar for production.

    Features:
        - Schedule jobs for future execution
        - Automatic retry on failure (max 3 attempts)
        - Persistent storage (survives restarts)
        - Async execution

    Example:
        >>> scheduler = CronScheduler(agent_state)
        >>> job_id = await scheduler.schedule_job(
        ...     job_type="post_content",
        ...     schedule_at=datetime.utcnow() + timedelta(hours=1),
        ...     payload={"platform": "x", "content": "Hello"}
        ... )
        >>> await scheduler.start()  # Start background processing
    """

    def __init__(
        self, 
        agent_state: Any, 
        check_interval_seconds: int = 10,
        storage: JobStorage | None = None,
    ):
        """Initialize scheduler.

        Args:
            agent_state: Agent state for accessing skills and memory
            check_interval_seconds: How often to check for due jobs (default 10)
            storage: Job storage backend (defaults to JSON file storage)
        """
        self.agent_state = agent_state
        self.check_interval = check_interval_seconds
        self.jobs: list[dict[str, Any]] = []
        self.running = False
        self._job_handlers: dict[str, Any] = {}
        
        # Initialize storage backend
        if storage is None:
            # Default to JSON file storage for backward compatibility
            memory_dir = agent_state.memory_dir if agent_state else None
            self.storage = create_storage("json", memory_dir=memory_dir)
        else:
            self.storage = storage
        
        # Load jobs from storage asynchronously is not possible in __init__
        # Will be loaded on first tick or start()
        self._jobs_loaded = False

    async def schedule_job(
        self,
        job_type: str,
        schedule_at: datetime,
        payload: dict[str, Any]
    ) -> str:
        """Schedule a job to run at specified time.

        Args:
            job_type: Type of job (e.g., "post_content")
            schedule_at: When to run the job (datetime)
            payload: Job-specific data

        Returns:
            Job ID (UUID)

        Example:
            >>> job_id = await scheduler.schedule_job(
            ...     job_type="post_content",
            ...     schedule_at=datetime.utcnow() + timedelta(hours=1),
            ...     payload={"campaign_id": "abc", "platform": "x"}
            ... )
        """
        job_id = str(uuid4())

        job = {
            "id": job_id,
            "job_type": job_type,
            "schedule_at": schedule_at.isoformat(),
            "payload": payload,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
            "max_attempts": 3
        }

        self.jobs.append(job)
        await self._save_job(job)

        # Calculate time until execution for logging
        now_utc = datetime.now(timezone.utc)
        time_until = schedule_at - now_utc
        
        logger.info(
            f"Job scheduled: id={job_id}, type={job_type}, "
            f"schedule_at={schedule_at.isoformat()}, "
            f"time_until={time_until.total_seconds():.1f}s"
        )

        return job_id

    async def run_tick(self) -> dict[str, int]:
        """Run a single scheduler tick (check and execute due jobs once).
        
        This is the primary method for Cloud Scheduler-triggered execution.
        It checks for due jobs, executes them, and returns metrics.
        
        This method is idempotent and safe to call concurrently from multiple
        scheduler instances (with proper state backend locking).
        
        Returns:
            Dict with execution metrics:
                - jobs_checked: Total jobs examined
                - jobs_due: Number of jobs that were due
                - jobs_executed: Number of jobs attempted
                - jobs_succeeded: Number of jobs that completed successfully
                - jobs_failed: Number of jobs that failed
                
        Example:
            >>> scheduler = CronScheduler(agent_state)
            >>> result = await scheduler.run_tick()
            >>> print(f"Executed {result['jobs_executed']} jobs")
        """
        now = datetime.now(timezone.utc)
        logger.info(f"Running scheduler tick at {now.isoformat()}")
        
        # Reload jobs from storage to get latest state
        await self._load_jobs()
        logger.info(f"Loaded {len(self.jobs)} total jobs from storage")
        
        now = datetime.now(timezone.utc)
        metrics = {
            "jobs_checked": len(self.jobs),
            "jobs_due": 0,
            "jobs_executed": 0,
            "jobs_succeeded": 0,
            "jobs_failed": 0,
        }
        
        # Check each job
        for job in self.jobs:
            job_id = job.get("id", "unknown")
            job_type = job.get("job_type", "unknown")
            job_status = job.get("status", "unknown")
            
            logger.info(f"Checking job {job_id} (type: {job_type}, status: {job_status})")
            
            if job["status"] != "pending":
                logger.info(f"Skipping job {job_id} - status is {job_status}, not pending")
                continue
                
            schedule_at = datetime.fromisoformat(job["schedule_at"])
            # Ensure timezone-aware for comparison
            if schedule_at.tzinfo is None:
                schedule_at = schedule_at.replace(tzinfo=timezone.utc)
                logger.warning(
                    f"Job {job['id']} had timezone-naive schedule_at, assuming UTC: {schedule_at}"
                )
            
            logger.info(
                f"Job {job_id} scheduled for {schedule_at.isoformat()}, "
                f"now is {now.isoformat()}, "
                f"due: {schedule_at <= now}"
            )
            
            if schedule_at <= now:
                metrics["jobs_due"] += 1
                
                # Try to claim the job (distributed lock)
                job_id = job["id"]
                claimed = await self.storage.claim_job(job_id)
                
                if not claimed:
                    logger.info(f"Job {job_id} already claimed by another worker")
                    continue
                
                metrics["jobs_executed"] += 1
                
                try:
                    # Execute job and track result
                    await self._execute_job(job)
                    
                    # Check final status to update metrics
                    if job.get("status") == "completed":
                        metrics["jobs_succeeded"] += 1
                    elif job.get("status") == "failed":
                        metrics["jobs_failed"] += 1
                finally:
                    # Always release the lease
                    await self.storage.release_job(job_id)
        
        logger.info(
            f"Scheduler tick completed: checked={metrics['jobs_checked']}, "
            f"due={metrics['jobs_due']}, executed={metrics['jobs_executed']}, "
            f"succeeded={metrics['jobs_succeeded']}, failed={metrics['jobs_failed']}"
        )
        
        return metrics

    async def start(self):
        """Start the scheduler background task (legacy/dev mode).

        This method runs continuously, checking for due jobs every
        check_interval seconds. Call stop() to terminate.
        
        Note: For Cloud Run production, use run_tick() called by Cloud Scheduler
        instead of this continuous loop. This method is kept for backward
        compatibility and local development.

        Example:
            >>> scheduler = CronScheduler(agent_state)
            >>> task = asyncio.create_task(scheduler.start())
            >>> # ... later ...
            >>> await scheduler.stop()
        """
        self.running = True
        logger.info("Cron scheduler started (continuous mode)")

        while self.running:
            await self.run_tick()
            await asyncio.sleep(self.check_interval)

    async def stop(self):
        """Stop the scheduler.

        This sets the running flag to False, causing the background
        task to exit on its next iteration.
        """
        self.running = False
        logger.info("Cron scheduler stopped")

    def register_handler(self, job_type: str, handler: Any):
        """Register a job handler for a specific job type.

        This allows external code to register custom handlers for different
        job types, making the scheduler extensible and framework-agnostic.

        Args:
            job_type: Type of job (e.g., "post_content", "collect_metrics")
            handler: Async callable that takes a job dict as argument

        Example:
            >>> async def my_handler(job):
            ...     print(f"Executing job: {job['id']}")
            >>> scheduler.register_handler("my_job_type", my_handler)
        """
        self._job_handlers[job_type] = handler
        logger.info(f"Registered handler for job type: {job_type}")

    async def seed_heartbeat_job(
        self,
        cron: str,
        bot_root: str,
        space_name: str | None = None,
    ) -> str | None:
        """Idempotent heartbeat job seeding.
        
        Returns existing job_id if a pending heartbeat job already exists,
        otherwise creates a new heartbeat job.
        
        Args:
            cron: Cron expression (e.g., "*/30 * * * *")
            bot_root: Bot root directory path
            space_name: Optional Google Chat space name for routing
            
        Returns:
            Job ID if created or found, None if scheduling failed
        """
        # Check for existing pending heartbeat job (idempotency)
        for job in self.jobs:
            if job.get("job_type") == "heartbeat" and job.get("status") == "pending":
                logger.info("Heartbeat job already exists: id=%s", job["id"])
                return job["id"]

        # Schedule next heartbeat 30 minutes from now
        schedule_at = datetime.now(timezone.utc) + timedelta(minutes=30)

        job_id = await self.schedule_job(
            job_type="heartbeat",
            schedule_at=schedule_at,
            payload={"bot_root": bot_root, "space_name": space_name, "cron": cron},
        )
        logger.info("Seeded heartbeat job: id=%s cron=%s", job_id, cron)
        return job_id

    async def _check_and_execute_jobs(self):
        """Check for due jobs and execute them."""
        now = datetime.now(timezone.utc)

        for job in self.jobs:
            if job["status"] != "pending":
                continue

            schedule_at = datetime.fromisoformat(job["schedule_at"])
            # Ensure timezone-aware for comparison
            if schedule_at.tzinfo is None:
                schedule_at = schedule_at.replace(tzinfo=timezone.utc)

            if schedule_at <= now:
                await self._execute_job(job)

    async def _execute_job(self, job: dict[str, Any]):
        """Execute a single job using registered handlers.

        Args:
            job: Job dict with id, job_type, payload, etc.
        """
        job_id = job["id"]
        job_type = job["job_type"]
        schedule_at = job.get("schedule_at", "unknown")

        logger.info(f"Executing job {job_id} (type: {job_type}, scheduled_for: {schedule_at})")

        try:
            job["status"] = "running"
            job["started_at"] = datetime.now(timezone.utc).isoformat()
            await self._save_job(job)

            # Look up handler from registry
            handler = self._job_handlers.get(job_type)
            if not handler:
                raise ValueError(f"No handler registered for job type: {job_type}")

            logger.info(f"Job {job_id}: invoking handler for '{job_type}'")

            # Inject agent state into job context for handlers to use
            job["_agent_state"] = self.agent_state

            try:
                # Execute the handler with JobHandler interface if available
                if hasattr(handler, 'handle'):
                    # JobHandler instance
                    await handler.handle(job)
                else:
                    # Direct callable (backward compatibility)
                    await handler(job)
            finally:
                # Always clean up agent state from job dict (even on error)
                job.pop("_agent_state", None)

            # Mark as completed
            job["status"] = "completed"
            job["completed_at"] = datetime.now(timezone.utc).isoformat()

            logger.info(f"Job {job_id} completed successfully")

        except Exception as e:
            job["attempts"] += 1
            logger.error(f"Job {job_id} failed: {e}", extra={"job": job})

            if job["attempts"] >= job["max_attempts"]:
                job["status"] = "failed"
                job["error"] = str(e)
            else:
                # Retry later (reschedule for 5 minutes from now)
                retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
                job["schedule_at"] = retry_at.isoformat()
                job["status"] = "pending"
                logger.info(f"Job {job_id} will retry at {retry_at}")

        finally:
            # Ensure agent state is cleaned up before saving
            job.pop("_agent_state", None)
            await self._save_job(job)

    async def _load_jobs(self):
        """Load jobs from storage backend."""
        self.jobs = await self.storage.load_jobs()
        self._jobs_loaded = True

    async def _save_jobs(self):
        """Save jobs to storage backend."""
        await self.storage.save_jobs(self.jobs)

    async def _save_job(self, job: dict[str, Any]):
        """Save one job without replacing unrelated storage state."""
        await self.storage.save_job(job)

    def get_pending_jobs(self) -> list[dict[str, Any]]:
        """Get all pending jobs.

        Returns:
            List of job dicts with status="pending"
        """
        return [job for job in self.jobs if job["status"] == "pending"]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get job by ID.

        Args:
            job_id: Job UUID to lookup

        Returns:
            Job dict if found, None otherwise
        """
        for job in self.jobs:
            if job["id"] == job_id:
                return job
        return None

    async def get_jobs_debug_info(self) -> list[dict[str, Any]]:
        """Get all jobs with debug information including parsed times.
        
        This is useful for debugging scheduler issues. It returns jobs with
        additional computed fields like is_due and time_until_due.
        
        Returns:
            List of job dicts with debug info
            
        Example:
            >>> debug_info = await scheduler.get_jobs_debug_info()
            >>> for job in debug_info:
            ...     print(f"{job['id']}: due={job['is_due']}, eta={job['time_until_due']}")
        """
        await self._load_jobs()
        now = datetime.now(timezone.utc)
        
        jobs_info = []
        for job in self.jobs:
            job_info = {
                "id": job.get("id"),
                "job_type": job.get("job_type"),
                "status": job.get("status"),
                "schedule_at": job.get("schedule_at"),
                "created_at": job.get("created_at"),
                "attempts": job.get("attempts", 0),
                "max_attempts": job.get("max_attempts", 3),
            }
            
            # Parse schedule_at and add computed fields
            try:
                schedule_at = datetime.fromisoformat(job["schedule_at"])
                if schedule_at.tzinfo is None:
                    schedule_at = schedule_at.replace(tzinfo=timezone.utc)
                    job_info["timezone_warning"] = "schedule_at was timezone-naive, assumed UTC"
                
                job_info["is_due"] = schedule_at <= now
                time_diff = schedule_at - now
                job_info["time_until_due_seconds"] = time_diff.total_seconds()
                job_info["time_until_due_readable"] = str(time_diff)
            except Exception as e:
                job_info["parse_error"] = str(e)
            
            jobs_info.append(job_info)
        
        return jobs_info
